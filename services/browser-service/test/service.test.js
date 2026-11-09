import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { authenticate, configuration, DEVELOPMENT_SECRET } from '../src/auth.js';
import { SessionManager, SESSION_LIMITS, validateAction } from '../src/sessions.js';
import { createServer } from '../src/server.js';
import { ServiceError } from '../src/errors.js';

const owner = { user: 'user-1', wiki: 'wiki-1' };
const secret = 'test-only-signing-secret';
const claims = (now = Date.now()) => ({
  sub: owner.user, wiki_id: owner.wiki, iss: 'wiki-agent', aud: 'wiki-browser-import',
  exp: Math.floor(now / 1000) + 120,
});
function token(payload = claims(), header = { alg: 'HS256' }, signingSecret = secret) {
  const encoded = [header, payload].map((value) => Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
  return `Bearer ${encoded}.${createHmac('sha256', signingSecret).update(encoded).digest('base64url')}`;
}

function fixture(options = {}) {
  const handles = [];
  const browser = {
    healthy: () => true,
    async create() {
      const handle = {
        closed: false, url: '', actions: [],
        async navigate(url) { this.url = url; },
        async metadata() { return { url: this.url, title: 'synthetic', width: 1024, height: 768 }; },
        async capture() { return { url: this.url, title: 'synthetic', html: '<article>hello</article>' }; },
        async screenshot() { return Buffer.from([137, 80, 78, 71]); },
        async action(input) { this.actions.push(input); },
        async close() { this.closed = true; },
      };
      handles.push(handle);
      return handle;
    },
  };
  return { manager: new SessionManager(browser, options), handles };
}

test('JWT signature, algorithm, audience, issuer, expiry and ownership claims are mandatory', () => {
  assert.deepEqual(authenticate(token(), secret), owner);
  for (const overrides of [
    { aud: 'other' }, { aud: ['wiki-browser-import'] }, { iss: 'other' },
    { exp: 1 }, { exp: Math.floor(Date.now() / 1000) + 301 }, { exp: '123' },
    { sub: '' }, { sub: 123 }, { wiki_id: '' }, { wiki_id: null },
    { iat: Math.floor(Date.now() / 1000) + 200 }, { nbf: Math.floor(Date.now() / 1000) + 200 },
    { iat: Math.floor(Date.now() / 1000) - 1000 },
  ]) assert.throws(() => authenticate(token({ ...claims(), ...overrides }), secret), { status: 401 });
  assert.throws(() => authenticate(token(claims(), { alg: 'none' }), secret), { status: 401 });
  assert.throws(() => authenticate(token(claims(), { alg: 'HS256', crit: ['x'] }), secret), { status: 401 });
  assert.throws(() => authenticate(token(claims(), { alg: 'HS256' }, 'wrong'), secret), { status: 401 });
  for (const header of [undefined, 'Basic abc', 'Bearer a.b.c', 'Bearer ']) {
    assert.throws(() => authenticate(header, secret), { status: 401 });
  }
  assert.equal(configuration({}).secret, DEVELOPMENT_SECRET);
  assert.throws(() => configuration({ NODE_ENV: 'production' }));
  assert.throws(() => configuration({ WIKI_AGENT_ENV: 'production' }));
  assert.throws(() => configuration({ WIKI_AGENT_ENV: 'staging', NODE_ENV: 'development' }));
  assert.throws(() => configuration({ WIKI_AGENT_ENV: 'development', NODE_ENV: 'production' }));
  assert.throws(() => configuration({ NODE_ENV: 'test', WIKI_AGENT_SECRET_KEY: DEVELOPMENT_SECRET }));
  assert.equal(configuration({ NODE_ENV: 'production', WIKI_AGENT_SECRET_KEY: secret }).secret, secret);
  assert.equal(configuration({ WIKI_AGENT_ENV: 'production', WIKI_AGENT_SECRET_KEY: secret }).secret, secret);
});

test('session limits count reservations and users across wikis; IDs are cryptographic and isolated', async () => {
  const { manager, handles } = fixture();
  const first = await manager.create(owner, 'https://example.com');
  const second = await manager.create({ ...owner, wiki: 'wiki-2' }, 'https://example.com');
  assert.match(first.id, /^[A-Za-z0-9_-]{32}$/);
  assert.notEqual(first.id, second.id);
  assert.equal(new Date(first.expires_at).getTime() > Date.now(), true);
  await assert.rejects(manager.create(owner, 'https://example.com'), { status: 429, code: 'BROWSER_CAPACITY' });
  assert.throws(() => manager.owned(first.id, { ...owner, wiki: 'wiki-2' }), { status: 404, code: 'SESSION_NOT_OWNED' });
  assert.throws(() => manager.owned(first.id, { ...owner, user: 'other' }), { status: 404, code: 'SESSION_NOT_OWNED' });
  for (let i = 0; i < 6; i++) await manager.create({ user: `other-${i}`, wiki: 'wiki-1' }, 'https://example.com');
  await assert.rejects(manager.create({ user: 'over', wiki: 'wiki-1' }, 'https://example.com'), { status: 429 });
  await manager.shutdown();
  assert.equal(manager.sessions.size, 0);
  assert.ok(handles.every((handle) => handle.closed));
  await assert.rejects(manager.create(owner, 'https://example.com'), { status: 503 });
});

test('long live-stream credentials are session-bound and cannot perform any control operation', async (t) => {
  const { manager } = fixture();
  t.after(() => manager.shutdown());
  const session = await manager.create(owner, 'https://example.com');
  const now = Math.floor(Date.now() / 1000);
  const frameClaims = {
    ...claims(), scope: 'browser-frames', session_id: session.id, iat: now, exp: now + 900,
  };
  const credentials = token(frameClaims);
  assert.equal(authenticate(credentials, secret).streamSessionId, session.id);
  assert.equal(authenticate(credentials, secret, (now + 61) * 1000).streamSessionId, session.id);
  assert.throws(() => authenticate(token({ ...claims(), exp: now + 60 }), secret,
    (now + 61) * 1000), { status: 401 });
  for (const invalid of [
    { exp: now + 901 }, { session_id: 'wrong' }, { iat: undefined }, { scope: 'all' },
  ]) assert.throws(() => authenticate(token({ ...frameClaims, ...invalid }), secret), { status: 401 });
  const server = createServer({ manager, secret });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  for (const [path, method] of [
    ['/sessions', 'POST'], [`/sessions/${session.id}/actions`, 'POST'],
    [`/sessions/${session.id}`, 'DELETE'], [`/sessions/${session.id}/capture`, 'GET'],
    [`/sessions/${'a'.repeat(32)}/frames`, 'GET'],
  ]) {
    const response = await fetch(base + path, { method, headers: { authorization: credentials } });
    assert.equal(response.status, 403);
  }
});
test('expiry closes contexts and queued operations recheck TTL; delete closes immediately', async () => {
  let now = Date.now();
  const { manager, handles } = fixture({ now: () => now });
  const session = await manager.create(owner, 'https://example.com');
  let finish;
  const first = manager.run(session.id, owner, () => new Promise((resolve) => { finish = resolve; }));
  const second = manager.run(session.id, owner, () => assert.fail('Expired action ran'));
  const rejection = assert.rejects(second, { status: 404, code: 'SESSION_EXPIRED' });
  await delay(0);
  now += SESSION_LIMITS.ttl;
  finish();
  await first;
  await rejection;
  await manager.sweep();
  assert.equal(handles[0].closed, true);
  assert.throws(() => manager.owned(session.id, owner), { code: 'SESSION_EXPIRED' });
  assert.throws(() => manager.owned(session.id, { user: 'other', wiki: owner.wiki }), { code: 'SESSION_NOT_OWNED' });
  now += SESSION_LIMITS.ttl;
  await manager.sweep();
  assert.throws(() => manager.owned(session.id, owner), { code: 'SESSION_NOT_FOUND' });
  const next = await manager.create(owner, 'https://example.com');
  await manager.remove(next.id, owner);
  assert.equal(handles[1].closed, true);
  assert.throws(() => manager.owned(next.id, owner), { status: 404, code: 'SESSION_NOT_FOUND' });
});

test('session actions serialize, survive safe failures and bound queue length', async () => {
  const { manager } = fixture();
  const session = await manager.create(owner, 'https://example.com');
  let active = 0;
  const order = [];
  await Promise.all(Array.from({ length: 8 }, (_, i) => manager.run(session.id, owner, async () => {
    assert.equal(active++, 0);
    await delay(1);
    order.push(i);
    active--;
  })));
  assert.deepEqual(order, [0, 1, 2, 3, 4, 5, 6, 7]);
  await assert.rejects(manager.run(session.id, owner, async () => { throw new Error('not logged'); }));
  await manager.run(session.id, owner, () => {});
  let release;
  const pending = manager.run(session.id, owner, () => new Promise((resolve) => { release = resolve; }));
  const queue = Array.from({ length: 15 }, () => manager.run(session.id, owner, () => {}));
  assert.throws(() => manager.run(session.id, owner, () => {}), { status: 429 });
  await delay(0);
  release();
  await Promise.all([pending, ...queue]);
  await manager.shutdown();
});

test('timeouts remove and close context, including late browser context creation', async () => {
  const limits = { ...SESSION_LIMITS, operationTimeout: 5 };
  const { manager, handles } = fixture({ limits });
  const session = await manager.create(owner, 'https://example.com');
  const keepAlive = delay(30);
  await assert.rejects(manager.run(session.id, owner, () => new Promise(() => {})), { status: 504 });
  assert.equal(handles[0].closed, true);
  assert.equal(manager.sessions.size, 0);
  let closed = false;
  const slow = new SessionManager({ create: async () => { await delay(20); return { close: async () => { closed = true; } }; } }, { limits });
  await assert.rejects(slow.create(owner, 'https://example.com'), { status: 504 });
  assert.equal(slow.sessions.size, 0);
  await keepAlive;
  assert.equal(closed, true);
});

test('contexts being closed continue to count against resource slots', async () => {
  const { manager, handles } = fixture({ limits: { ...SESSION_LIMITS, total: 1 } });
  const session = await manager.create(owner, 'https://example.com');
  let finish;
  handles[0].close = () => new Promise((resolve) => { finish = resolve; });
  const closing = manager.remove(session.id, owner);
  await delay(0);
  await assert.rejects(manager.create({ user: 'other', wiki: owner.wiki }, 'https://example.com'), { status: 429 });
  finish();
  await closing;
  assert.equal(manager.live.size, 0);
});

test('action schema permits only bounded manual interaction', () => {
  for (const input of [
    { action: 'click', x: 0, y: 0 }, { action: 'type', text: 'synthetic' },
    { action: 'key', key: 'Enter' }, { action: 'scroll', delta: -1000 }, { action: 'reload' },
    { action: 'drag', x: 0, y: 0, to_x: 1023, to_y: 767 },
    { action: 'pointer', phase: 'down', x: 0, y: 0 },
    { action: 'pointer', phase: 'move', x: 1023, y: 767 },
    { action: 'pointer', phase: 'up', x: 0, y: 0 },
    { action: 'back' },
  ]) assert.equal(validateAction(input), input);
  for (const input of [
    { action: 'click', x: 1024, y: 0 }, { action: 'click', x: -1, y: 0 },
    { action: 'type', text: 'x'.repeat(4097) }, { action: 'key', key: 'F12' },
    { action: 'scroll', delta: Infinity }, { action: 'reload', url: 'file:///' },
    { action: '__proto__' }, { action: 'evaluate', text: 'script' }, null,
    { action: 'drag', x: 0.1, y: 0, to_x: 100, to_y: 100 },
    { action: 'drag', x: 0, y: 0, to_x: 1024, to_y: 767 },
    { action: 'drag', x: 0, y: 0, to_x: 1023, to_y: 768 },
    { action: 'drag', x: 0, y: 0, to_x: -1, to_y: 100 },
    { action: 'drag', x: 0, y: 0 },
    { action: 'drag', x: 0, y: 0, to_x: 100, to_y: 100, steps: 100000 },
    { action: 'pointer', phase: 'click', x: 0, y: 0 },
    { action: 'pointer', phase: 'down', x: 0.5, y: 0 },
    { action: 'pointer', phase: 'move', x: 1024, y: 0 },
    { action: 'pointer', phase: 'up', x: 0, y: 768 },
    { action: 'back', url: 'https://example.com' },
  ]) assert.throws(() => validateAction(input), { status: 400 });
});

test('HTTP API enforces every endpoint token/owner, no-store, body limits and safe errors', async (t) => {
  let now = Date.now();
  const { manager, handles } = fixture({ now: () => now });
  const server = createServer({ manager, secret, now: () => now });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await manager.shutdown(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = (path, method = 'GET', body, auth = token(claims(now))) => fetch(base + path, {
    method, headers: { authorization: auth, 'content-type': 'application/json' },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  assert.equal((await fetch(base + '/health')).status, 200);
  for (const path of ['/sessions', '/render', '/missing', '/sessions/invalid/actions']) {
    assert.equal((await fetch(base + path)).status, 401);
  }
  const created = await call('/sessions', 'POST', { url: 'https://example.com' });
  assert.equal(created.status, 201);
  const session = await created.json();
  for (const [suffix, method, body] of [
    ['', 'GET'], ['/screenshot', 'GET'], ['/capture', 'GET'], ['/actions', 'POST', { action: 'reload' }], ['', 'DELETE'],
  ]) {
    for (const foreign of [{ sub: 'foreign' }, { wiki_id: 'foreign' }]) {
      assert.equal((await call(`/sessions/${session.id}${suffix}`, method, body, token({ ...claims(now), ...foreign }))).status, 404);
    }
  }
  const screenshot = await call(`/sessions/${session.id}/screenshot`);
  assert.equal(screenshot.headers.get('content-type'), 'image/png');
  assert.equal(screenshot.headers.get('cache-control'), 'no-store');
  assert.equal((await call(`/sessions/${session.id}/actions`, 'POST', { action: 'type', text: 'synthetic' })).status, 200);
  assert.equal(handles[0].actions.length, 1);
  assert.equal((await call(`/sessions/${session.id}/actions`, 'POST', { action: 'drag', x: 1, y: 2, to_x: 300, to_y: 200 })).status, 200);
  assert.equal(handles[0].actions.at(-1).action, 'drag');
  assert.equal((await call(`/sessions/${session.id}/actions`, 'POST', { action: 'type', text: 'x'.repeat(20000) })).status, 413);
  const rendered = await call('/render', 'POST', { url: 'https://example.com' });
  assert.equal(rendered.status, 200);
  assert.match((await rendered.json()).html, /article/);
  assert.equal(handles[1].closed, true);
  handles[0].capture = async () => { throw new Error('sensitive-input-stack'); };
  const error = await call(`/sessions/${session.id}/capture`);
  assert.equal(error.status, 502);
  const detail = await error.json();
  assert.equal(detail.detail.code, 'BROWSER_RENDER_FAILED');
  assert.doesNotMatch(JSON.stringify(detail), /sensitive-input-stack/);
  now += SESSION_LIMITS.ttl;
  assert.equal((await call(`/sessions/${session.id}`)).status, 404);
  await manager.sweep();
  assert.equal(handles[0].closed, true);
  const next = await (await call('/sessions', 'POST', { url: 'https://example.com' })).json();
  assert.equal((await call(`/sessions/${next.id}`, 'DELETE')).status, 204);
  assert.equal(handles.at(-1).closed, true);
});

test('HTTP actions recheck JWT expiry after waiting in the serial action queue', async (t) => {
  let now = Date.now();
  const { manager, handles } = fixture({ now: () => now });
  const server = createServer({ manager, secret, now: () => now });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { await manager.shutdown(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { authorization: token(claims(now)), 'content-type': 'application/json' };
  const session = await (await fetch(base + '/sessions', {
    method: 'POST', headers, body: JSON.stringify({ url: 'https://example.com' }),
  })).json();
  let release;
  handles[0].action = () => new Promise((resolve) => { release = resolve; });
  const input = { method: 'POST', headers, body: JSON.stringify({ action: 'reload' }) };
  const first = fetch(base + `/sessions/${session.id}/actions`, input);
  for (let i = 0; !release && i < 100; i++) await delay(1);
  assert.ok(release);
  const second = fetch(base + `/sessions/${session.id}/actions`, input);
  for (let i = 0; manager.sessions.get(session.id).pending < 2 && i < 100; i++) await delay(1);
  assert.equal(manager.sessions.get(session.id).pending, 2);
  now += 130000;
  release();
  assert.equal((await first).status, 200);
  assert.equal((await second).status, 401);
});

test('aborted queued HTTP input is not dispatched and a fresh request preserves the owned session', async (t) => {
  const { manager, handles } = fixture();
  const session = await manager.create(owner, 'https://example.com');
  const handle = handles[0];
  let generation = 0;
  handle.inputGeneration = () => generation;
  handle.resetInput = async (expected) => { if (expected === generation) generation++; };
  handle.action = async (input, expected) => {
    if (expected !== generation) throw new ServiceError(502, 'BROWSER_RENDER_FAILED', 'The browser action was cancelled.');
    handle.actions.push(input);
  };
  const server = createServer({ manager, secret });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await manager.shutdown();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  const endpoint = `http://127.0.0.1:${server.address().port}/sessions/${session.id}/actions`;
  const headers = { authorization: token(), 'content-type': 'application/json' };
  let release;
  const blocking = manager.run(session.id, owner, () => new Promise((resolve) => { release = resolve; }));
  const controller = new AbortController();
  const abandoned = fetch(endpoint, {
    method: 'POST', headers, signal: controller.signal,
    body: JSON.stringify({ action: 'type', text: 'private-input-that-must-not-dispatch' }),
  });
  const aborted = assert.rejects(abandoned, { name: 'AbortError' });
  for (let i = 0; manager.sessions.get(session.id).pending < 2 && i < 100; i++) await delay(1);
  assert.equal(manager.sessions.get(session.id).pending, 2);
  controller.abort();
  await aborted;
  for (let i = 0; generation === 0 && i < 100; i++) await delay(1);
  assert.equal(generation, 1);
  release();
  await blocking;
  for (let i = 0; manager.sessions.get(session.id).pending > 0 && i < 100; i++) await delay(1);
  assert.equal(handle.actions.length, 0);
  assert.equal(handle.closed, false);
  const fresh = await fetch(endpoint, {
    method: 'POST', headers, body: JSON.stringify({ action: 'pointer', phase: 'move', x: 10, y: 10 }),
  });
  assert.equal(fresh.status, 200);
  assert.equal(handle.actions.length, 1);
  assert.equal(manager.sessions.has(session.id), true);
});
