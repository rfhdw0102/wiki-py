import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { chromium } from 'playwright';
import { launchBrowser } from '../src/browser.js';
import { PublicFetcher } from '../src/network.js';
import { SessionManager } from '../src/sessions.js';
import { createServer } from '../src/server.js';

test('actual Chromium HTTP sessions preserve redirect cookies, relative URLs and actions; unrouted traffic is rejected', async (t) => {
  const requests = [];
  const response = (html, options = {}) => ({
    status: 200, body: Buffer.from(html), setCookies: [],
    headers: { 'content-type': 'text/html' }, ...options,
  });
  const fetcher = new PublicFetcher({
    lookup: async () => [{ address: '93.184.216.34', family: 4 }],
    transport: async (url, options) => {
      requests.push(url.href);
      assert.equal(options.headers.authorization, undefined);
      assert.equal(options.address.address, '93.184.216.34');
      if (url.pathname === '/start') return response('', {
        status: 302, headers: { location: '/protected/landing' },
        setCookies: ['auth=synthetic; Path=/; HttpOnly', 'second=two; Path=/'],
      });
      if (url.pathname.startsWith('/protected/')) {
        assert.match(options.headers.cookie, /auth=synthetic/);
        assert.match(options.headers.cookie, /second=two/);
      }
      if (url.pathname === '/protected/landing') return response(`<!doctype html>
        <title>Interactive final</title><article>visible</article>
        <link rel="stylesheet" href="./assets/site.css">
        <a href="./next" style="position:absolute;left:10px;top:50px;width:100px;height:30px">Next</a>
        <form><input id="field" style="position:absolute;left:10px;top:100px;width:200px;height:40px"></form>
        <div id="slider" style="position:absolute;left:10px;top:200px;width:100px;height:40px;background:gray"></div>
        <script>
          setTimeout(()=>document.querySelector('article').append(' dynamic'),50);
          document.querySelector('#field').addEventListener('keydown',event=>{
            if(event.key==='Enter'){event.preventDefault();document.querySelector('article').append(' typed')}
          });
          let dragging=false;
          document.querySelector('#slider').addEventListener('mousedown',()=>dragging=true);
          addEventListener('mouseup',()=>{if(dragging)document.querySelector('article').append(' dragged');dragging=false});
        </script>`);
      if (url.pathname === '/protected/assets/site.css') return response('article{color:red}', {
        headers: { 'content-type': 'text/css' },
      });
      if (url.pathname === '/protected/next') return response('<title>Next authenticated page</title><article>next</article>');
      return response('', { status: 404 });
    },
  });
  let nativeBrowser, browser;
  try {
    browser = await launchBrowser(fetcher, {
      async launch(options) {
        nativeBrowser = await chromium.launch(options);
        return nativeBrowser;
      },
    });
  } catch (error) {
    if (!process.env.BROWSER_TEST_REQUIRED && /Executable doesn't exist/.test(error.message)) {
      t.skip('Pinned Playwright Chromium is not installed.');
      return;
    }
    throw error;
  }
  const manager = new SessionManager(browser);
  const secret = 'synthetic-http-browser-signing-key';
  const server = createServer({ manager, secret });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await manager.shutdown();
    await browser.close();
  });
  // Deliberately omit routing: Chromium must not reach any destination directly.
  const unrouted = await nativeBrowser.newContext({ serviceWorkers: 'block' });
  try {
    const page = await unrouted.newPage();
    page.setDefaultNavigationTimeout(3000);
    assert.equal((await page.goto('http://example.com/unrouted')).status(), 403);
    await assert.rejects(page.goto('https://example.com/unrouted'));
    assert.equal(requests.length, 0);
  } finally { await unrouted.close(); }

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const encoded = [
    { alg: 'HS256' },
    { sub: 'synthetic-user', wiki_id: 'synthetic-wiki', aud: 'wiki-browser-import',
      iss: 'wiki-agent', exp: Math.floor(Date.now() / 1000) + 120 },
  ].map((value) => Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
  const authorization = `Bearer ${encoded}.${createHmac('sha256', secret).update(encoded).digest('base64url')}`;
  const call = (path, method = 'GET', body) => fetch(base + path, {
    method, headers: { authorization, 'content-type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  assert.equal((await fetch(base + '/health')).status, 200);
  const created = await call('/sessions', 'POST', { url: 'https://example.com/start' });
  assert.equal(created.status, 201);
  const session = await created.json();
  assert.equal(session.url, 'https://example.com/protected/landing');
  const metadata = await (await call(`/sessions/${session.id}`)).json();
  assert.equal(typeof metadata.title, 'string');
  assert.equal(metadata.width, 1024);
  assert.equal(metadata.height, 768);

  const screenshot = await call(`/sessions/${session.id}/screenshot`);
  assert.equal(screenshot.status, 200);
  assert.equal(screenshot.headers.get('cache-control'), 'no-store');
  assert.equal(screenshot.headers.get('content-type'), 'image/png');
  const png = Buffer.from(await screenshot.arrayBuffer());
  assert.equal(png.readUInt32BE(16), 1024);
  assert.equal(png.readUInt32BE(20), 768);

  for (const action of [
    { action: 'click', x: 80, y: 120 },
    { action: 'type', text: 'synthetic-private-input' },
    { action: 'key', key: 'Enter' },
    { action: 'drag', x: 20, y: 220, to_x: 900, to_y: 220 },
  ]) assert.equal((await call(`/sessions/${session.id}/actions`, 'POST', action)).status, 200);
  const capture = await (await call(`/sessions/${session.id}/capture`)).json();
  assert.equal(capture.title, 'Interactive final');
  assert.equal(capture.url, session.url);
  assert.match(capture.html, /visible dynamic typed dragged/);
  assert.doesNotMatch(capture.html, /<script|<form|<input|synthetic-private-input/);
  const acknowledged = await call(`/sessions/${session.id}/actions`, 'POST', {
    action: 'click', x: 30, y: 60,
  });
  assert.equal(acknowledged.status, 200);
  const next = await (await call(`/sessions/${session.id}/capture`)).json();
  assert.equal(next.url, 'https://example.com/protected/next');
  assert.equal(next.title, 'Next authenticated page');
  assert.ok(requests.includes('https://example.com/protected/assets/site.css'));
  const rendered = await call('/render', 'POST', { url: 'https://example.com/start' });
  assert.equal(rendered.status, 200);
  assert.match((await rendered.json()).html, /visible dynamic/);
  assert.equal(manager.sessions.size, 1);
  assert.equal((await call(`/sessions/${session.id}`, 'DELETE')).status, 204);
  assert.equal(manager.sessions.size, 0);
});
