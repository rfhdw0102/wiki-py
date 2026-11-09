import test from 'node:test';
import assert from 'node:assert/strict';
import { launchBrowser, applyCookies, createHandle, chromiumEnvironment } from '../src/browser.js';
import { safeError, SAFE_FALLBACK_CODE, ServiceError } from '../src/errors.js';
import { PublicFetcher } from '../src/network.js';
import { SessionManager } from '../src/sessions.js';

const dns = async () => [{ address: '93.184.216.34', family: 4 }];
const owner = { user: 'synthetic-user', wiki: 'synthetic-wiki' };
const reply = (body, extras = {}) => ({
  status: 200, headers: { 'content-type': 'text/html', ...extras.headers },
  setCookies: extras.cookies || [], body: Buffer.from(body), ...extras,
});

test('Chromium environment only contains explicit runtime keys, never signing or infrastructure secrets', () => {
  const env = chromiumEnvironment('/app/.runtime', {
    PATH: '/usr/bin', HOME: '/sensitive-home', LANG: 'sensitive-lang',
    WIKI_AGENT_SECRET_KEY: 'synthetic-signing-key',
    WIKI_AGENT_ENV: 'production', DATABASE_URL: 'synthetic-database-secret',
    SMTP_PASSWORD: 'synthetic-mail-secret', ENCRYPTION_KEY: 'synthetic-encryption-key',
    NODE_OPTIONS: '--inspect=0.0.0.0', LD_PRELOAD: '/synthetic-library',
  });
  assert.deepEqual(env, {
    PATH: '/usr/bin', HOME: '/app/.runtime', TMPDIR: '/app/.runtime', LANG: 'C.UTF-8',
  });
  assert.equal(Object.values(env).some((value) => value.includes('synthetic')), false);
});

async function browserFor(t, transport) {
  let browser;
  try { browser = await launchBrowser(new PublicFetcher({ lookup: dns, transport })); }
  catch (error) {
    if (!process.env.BROWSER_TEST_REQUIRED && /Executable doesn't exist/.test(error.message)) {
      t.skip('Pinned Playwright Chromium is not installed.');
      return null;
    }
    throw error;
  }
  t.after(() => browser.close());
  return browser;
}

test('cookies are separate, domain-validated and deletions preserved without header flattening', async () => {
  const added = [];
  await applyCookies({ addCookies: async (cookies) => added.push(...cookies) }, [
    'a=one; Path=/; HttpOnly', 'b=two; Path=/; Secure; SameSite=None',
    'evil=ignored; Domain=unrelated.example', 'suffix=ignored; Domain=com',
    'a=; Path=/; Max-Age=0',
  ], 'https://example.com/login');
  assert.deepEqual(added.map((cookie) => cookie.name), ['a', 'b', 'a']);
  assert.equal(added[0].httpOnly, true);
  assert.equal(added[1].sameSite, 'None');
  assert.equal(added[2].expires, 1);
});

test('asset bursts stay bounded and unavailable or private subresources do not terminate the article', async (t) => {
  let active = 0, maximum = 0, fetched = 0;
  const browser = await browserFor(t, async (url) => {
    if (url.pathname === '/article') return reply(`<title>Busy article</title>
      <article>A complete article remains readable despite a failed optional resource.</article>
      ${Array.from({ length: 24 }, (_, i) => `<link rel="stylesheet" href="https://cdn${i}.example.com/style.css">`).join('')}
      <img src="http://127.0.0.1/blocked">`);
    active++;
    maximum = Math.max(maximum, active);
    try {
      await new Promise((resolve) => setTimeout(resolve, 40));
      fetched++;
      if (url.hostname === 'cdn7.example.com') throw new ServiceError(502, 'network_failure', 'Synthetic optional asset failure');
      return reply('body{color:black}', { headers: { 'content-type': 'text/css' } });
    } finally { active--; }
  });
  if (!browser) return;
  const manager = new SessionManager(browser);
  t.after(() => manager.shutdown());
  const session = await manager.create(owner, 'https://example.com/article');
  const capture = await manager.run(session.id, owner, (handle) => handle.capture());
  assert.match(capture.html, /complete article remains readable/);
  assert.equal(fetched, 24);
  assert.ok(maximum <= 8);
  let first;
  const arrived = new Promise((resolve) => { first = resolve; });
  const stop = await manager.run(session.id, owner, (handle) => handle.frames(first, assert.fail));
  const frame = await Promise.race([
    arrived, new Promise((_, reject) => setTimeout(() => reject(new Error('Missing live frame')), 3000)),
  ]);
  assert.ok(frame.resource_errors >= 1);
  assert.ok(frame.image.startsWith('/9j/'));
  stop();
  await manager.remove(session.id, owner);
});

test('real Chromium renders dynamic content, preserves redirect/login cookies and supports manual actions/capture', async (t) => {
  let loginBody = '', seenCookies = '';
  const browser = await browserFor(t, async (url, options) => {
    assert.equal(options.address.address, '93.184.216.34');
    assert.equal(options.headers.authorization, undefined);
    if (url.pathname === '/start') return reply('', {
      status: 302, headers: { location: '/login' },
      cookies: ['a=one; Path=/; HttpOnly', 'b=two; Path=/'],
    });
    if (url.pathname === '/login' && options.method === 'POST') {
      loginBody = options.body.toString();
      seenCookies = options.headers.cookie;
      return reply('', { status: 303, headers: { location: '/article' }, cookies: ['logged=yes; Path=/; HttpOnly'] });
    }
    if (url.pathname === '/login') return reply(`<!doctype html><title>Manual login</title>
      <style>input {position:absolute;left:10px;top:10px;width:200px;height:40px}button{position:absolute;left:10px;top:80px}</style>
      <form method="post" action="/login"><input name="password" type="password"><button>Login</button></form>`);
    if (url.pathname === '/article') {
      assert.match(options.headers.cookie, /logged=yes/);
      return reply(`<!doctype html><html onclick="synthetic()"><head><title>Rendered article</title></head><body>
        <article id="content">Article</article>
        <div id="slider" style="position:absolute;left:10px;top:200px;width:100px;height:40px;background:gray"></div>
        <script>
          setTimeout(()=>document.querySelector('article').append(' dynamic content'),50);
          let dragging=false,moves=0;
          document.querySelector('#slider').addEventListener('mousedown',()=>dragging=true);
          addEventListener('mousemove',()=>{if(dragging)moves++});
          addEventListener('mouseup',()=>{if(dragging)document.querySelector('article').append(' drag moves:'+moves);dragging=false});
        </script>
        <form><input value="synthetic-login-field"><article>Form-wrapped visible article</article></form>
        <template><script>synthetic()</script><input value="synthetic-template"></template></body></html>`);
    }
    return reply('', { status: 404 });
  });
  if (!browser) return;
  const manager = new SessionManager(browser);
  t.after(() => manager.shutdown());
  const session = await manager.create(owner, 'https://example.com/start');
  assert.equal(session.url, 'https://example.com/login');
  assert.equal((await manager.run(session.id, owner, (handle) => handle.capture())).title, 'Manual login');
  await manager.run(session.id, owner, async (handle) => {
    await handle.action({ action: 'click', x: 50, y: 30 });
    await handle.action({ action: 'type', text: 'synthetic-password' });
    const before = await handle.capture();
    assert.doesNotMatch(before.html, /<form|<input|synthetic-password/);
    await handle.action({ action: 'key', key: 'Enter' });
    await handle.capture({ settle: true });
    await handle.action({ action: 'drag', x: 20, y: 220, to_x: 900, to_y: 220 });
    const capture = await handle.capture();
    assert.equal(capture.url, 'https://example.com/article');
    assert.equal(capture.title, 'Rendered article');
    assert.match(capture.html, /Article dynamic content/);
    assert.match(capture.html, /drag moves:32/);
    assert.match(capture.html, /Form-wrapped visible article/);
    assert.doesNotMatch(capture.html, /<script|<form|<input|onclick|synthetic-login-field|synthetic-template/);
    const png = await handle.screenshot();
    assert.equal(png.readUInt32BE(16), 1024);
    assert.equal(png.readUInt32BE(20), 768);
  });
  assert.equal(loginBody, 'password=synthetic-password');
  assert.match(seenCookies, /a=one/);
  assert.match(seenCookies, /b=two/);
  await manager.remove(session.id, owner);
  assert.equal(manager.sessions.size, 0);
});

test('real Chromium blocks private subresources/redirects, WebSockets and shared cookies', async (t) => {
  const paths = [];
  const browser = await browserFor(t, async (url, options) => {
    paths.push(url.href);
    if (url.pathname === '/private') return reply('<title>Blocked</title><img src="http://127.0.0.1/private">');
    if (url.pathname === '/redirect') return reply('', { status: 302, headers: { location: 'http://169.254.169.254/latest' } });
    if (url.pathname === '/cookie') return reply('<title>Cookie</title>', { cookies: ['unique=yes; Path=/'] });
    if (url.pathname === '/api') {
      assert.equal(options.headers.authorization, 'Bearer website-only-token');
      return reply('ok');
    }
    if (url.pathname === '/service-auth') {
      assert.equal(options.headers.authorization, undefined);
      return reply('blocked');
    }
    if (url.pathname === '/isolated') {
      assert.equal(options.headers.cookie, undefined);
      return reply(`<title>Isolated</title><article>content</article><script>
        document.querySelector('article').append(' RTC:'+typeof RTCPeerConnection);
        fetch('/api',{headers:{Authorization:'Bearer website-only-token'}}).then(r=>r.text()).then(v=>document.querySelector('article').append(' SPA:'+v));
        fetch('/service-auth',{headers:{Authorization:'Bearer e30.eyJpc3MiOiJ3aWtpLWFnZW50In0.synthetic'}});
        navigator.serviceWorker.register('/worker.js').catch(()=>{});
        new WebSocket("ws://127.0.0.1/private")</script>`);
    }
    return reply('', { status: 404 });
  });
  if (!browser) return;
  const manager = new SessionManager(browser);
  t.after(() => manager.shutdown());
  let privateSession;
  try {
    privateSession = await manager.create(owner, 'https://example.com/private');
    // Chromium may reject an unsafe image before routing, or routing rejects it asynchronously after commit.
    const captured = await manager.run(privateSession.id, owner, (handle) => handle.capture());
    assert.equal(captured.url, 'https://example.com/private');
  } catch (error) { assert.equal(error.code, 'UNSAFE_URL'); }
  finally {
    const state = privateSession && manager.sessions.get(privateSession.id);
    if (state) await manager.close(state);
  }
  await assert.rejects(manager.create(owner, 'https://example.com/redirect'), { code: 'UNSAFE_URL' });
  const cookie = await manager.create(owner, 'https://example.com/cookie');
  const isolated = await manager.create({ user: 'another-user', wiki: owner.wiki }, 'https://example.com/isolated');
  const capture = await manager.run(isolated.id, { user: 'another-user', wiki: owner.wiki }, (handle) => handle.capture());
  assert.equal(capture.title, 'Isolated');
  assert.match(capture.html, /RTC:undefined/);
  assert.match(capture.html, /SPA:ok/);
  assert.equal(paths.some((url) => url.endsWith('/worker.js')), false);
  assert.equal(paths.some((url) => /127\.0\.0\.1|169\.254/.test(url)), false);
  await manager.remove(cookie.id, owner);
});

test('drag releases mouse on failure, bounds steps, clips titles and hides raw action failures', async () => {
  let moves = 0, released = false;
  const page = {
    url: () => 'https://example.com/',
    isClosed: () => false,
    title: async () => 't'.repeat(600),
    on() {},
    async waitForLoadState() {},
    async waitForTimeout() {},
    evaluate: async () => '<article>visible content</article>',
    mouse: {
      async move(_x, _y, options) {
        if (++moves === 2) {
          assert.equal(options.steps, 32);
          throw new Error('sensitive-typed-action-state');
        }
      },
      async down() {},
      async up() { released = true; },
    },
  };
  const context = {
    setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, on() {},
    async addInitScript() {}, async exposeBinding() {}, async routeWebSocket() {}, async route() {},
    async newPage() { return page; }, async close() {},
  };
  const handle = await createHandle({ newContext: async () => context }, {});
  assert.equal((await handle.metadata()).title.length, 512);
  assert.equal((await handle.capture()).title.length, 512);
  try {
    await handle.action({ action: 'drag', x: 0, y: 0, to_x: 1023, to_y: 767 });
    assert.fail('Expected drag failure');
  } catch (error) {
    const safe = safeError(error);
    assert.equal(safe.code, SAFE_FALLBACK_CODE);
    assert.doesNotMatch(safe.message, /sensitive-typed-action-state/);
  } finally { await handle.close(); }
  assert.equal(released, true);
});

test('popup initial requests and redirect destinations remain DNS-pinned and private-network failures are safe', async (t) => {
  const paths = [];
  const browser = await browserFor(t, async (url) => {
    paths.push(url.href);
    if (url.pathname === '/root') return reply(`<title>Popup isolation</title>
      <a target="_blank" href="/redirect" style="position:absolute;left:10px;top:20px;width:150px;height:40px">Open</a>`);
    if (url.pathname === '/redirect') return reply('', { status: 302, headers: { location: 'http://127.0.0.1/private' } });
    return reply('', { status: 404 });
  });
  if (!browser) return;
  const manager = new SessionManager(browser);
  t.after(() => manager.shutdown());
  const session = await manager.create(owner, 'https://example.com/root');
  let error;
  let finish;
  const failure = new Promise((resolve) => { finish = resolve; });
  await manager.run(session.id, owner, (handle) => handle.frames(() => {}, (value) => { error = value; finish(); }));
  try {
    await manager.run(session.id, owner, (handle) => handle.action({ action: 'click', x: 30, y: 40 }));
  } catch (value) { error = value; finish(); }
  let timer;
  try {
    await Promise.race([failure, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Popup isolation check timed out')), 5000); })]);
  } finally { clearTimeout(timer); }
  assert.equal(error.code, 'UNSAFE_URL');
  assert.deepEqual(paths, ['https://example.com/root', 'https://example.com/redirect']);
  assert.doesNotMatch(error.message, /127\.0\.0\.1|private/);
});

test('pointer reset covers an in-flight down, rejects stale queued input and preserves context after action failure', async () => {
  let held = false, finishDown, delayDown = true, closed = false, typed = 0;
  const page = {
    url: () => 'https://example.com/', isClosed: () => false, title: async () => 'Input fixture',
    on() {},
    mouse: {
      async move() {},
      async down() {
        held = true;
        if (delayDown) await new Promise((resolve) => { finishDown = resolve; });
      },
      async up() { held = false; },
    },
    keyboard: {
      async insertText() { typed++; },
      async press() { throw new Error('sensitive-private-action-text'); },
    },
  };
  const context = {
    setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, on() {},
    async addInitScript() {}, async exposeBinding() {}, async routeWebSocket() {}, async route() {},
    async newPage() { return page; }, async close() { closed = true; },
  };
  const handle = await createHandle({ newContext: async () => context }, {});
  const oldGeneration = handle.inputGeneration();
  const down = handle.action({ action: 'pointer', phase: 'down', x: 20, y: 20 }, oldGeneration);
  for (let i = 0; !finishDown && i < 100; i++) await new Promise((resolve) => setImmediate(resolve));
  assert.ok(finishDown);
  const rejected = assert.rejects(down, { code: 'BROWSER_RENDER_FAILED' });
  await handle.resetInput(oldGeneration);
  finishDown();
  await rejected;
  assert.equal(held, false);
  assert.equal(closed, false);
  await assert.rejects(handle.action({ action: 'type', text: 'must-not-be-typed' }, oldGeneration), { code: 'BROWSER_RENDER_FAILED' });
  assert.equal(typed, 0);
  delayDown = false;
  await handle.action({ action: 'pointer', phase: 'down', x: 20, y: 20 });
  assert.equal(held, true);
  try {
    await handle.action({ action: 'key', key: 'Enter' });
    assert.fail('Expected action failure');
  } catch (error) {
    assert.equal(safeError(error).code, 'BROWSER_RENDER_FAILED');
    assert.doesNotMatch(safeError(error).message, /sensitive-private-action-text/);
  }
  assert.equal(held, false);
  assert.equal(closed, false);
  await handle.action({ action: 'pointer', phase: 'move', x: 30, y: 20 });
  assert.equal(held, false);
  await handle.close();
});
