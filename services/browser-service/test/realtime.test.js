import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { setTimeout as delay } from 'node:timers/promises';
import { launchBrowser } from '../src/browser.js';
import { PublicFetcher } from '../src/network.js';
import { SessionManager, SESSION_LIMITS } from '../src/sessions.js';
import { createServer } from '../src/server.js';
import { chromium } from 'playwright';

function liveClient(response, controller) {
  const reader = response.body.getReader();
  const queue = [], waiters = [];
  let buffer = '', done = false, count = 0, lastLocation = '';
  const pump = (async () => {
    const decoder = new TextDecoder();
    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        assert.ok(buffer.length < 2 * 1024 * 1024);
        for (let index; (index = buffer.indexOf('\n\n')) !== -1;) {
          const block = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const event = /^event: (.+)$/m.exec(block)?.[1];
          const data = /^data: (.+)$/m.exec(block)?.[1];
          if (!event || !data) continue;
          const value = JSON.parse(data);
          if (event === 'frame') { count++; lastLocation = `${value.url} (${value.title})`; }
          const waiting = waiters.find((item) => item.event === event && item.predicate(value));
          if (waiting) {
            waiters.splice(waiters.indexOf(waiting), 1);
            clearTimeout(waiting.timer);
            waiting.resolve(value);
          } else {
            queue.push({ event, value });
            if (queue.length > 4) queue.shift();
          }
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        for (const waiter of waiters.splice(0)) { clearTimeout(waiter.timer); waiter.reject(error); }
      }
    } finally {
      done = true;
      for (const waiter of waiters.splice(0)) { clearTimeout(waiter.timer); waiter.reject(new Error('Stream closed')); }
      reader.releaseLock();
    }
  })();
  return {
    get count() { return count; }, get done() { return done; },
    discardFrames() {
      for (let i = queue.length - 1; i >= 0; i--) if (queue[i].event === 'frame') queue.splice(i, 1);
    },
    next(predicate = () => true, event = 'frame', timeout = 5000, label = event) {
      const index = queue.findIndex((item) => item.event === event && predicate(item.value));
      if (index !== -1) return Promise.resolve(queue.splice(index, 1)[0].value);
      return new Promise((resolve, reject) => {
        const waiter = { event, predicate, resolve, reject };
        waiter.timer = setTimeout(() => {
          waiters.splice(waiters.indexOf(waiter), 1);
          reject(new Error(`Timed out waiting for ${label}; last fixture location: ${lastLocation}`));
        }, timeout);
        waiters.push(waiter);
      });
    },
    async stop() { controller.abort(); await pump; },
  };
}

function jpegSize(image) {
  const bytes = Buffer.from(image, 'base64');
  let position = 2;
  while (position < bytes.length) {
    while (bytes[position] === 255) position++;
    const marker = bytes[position++];
    if (marker === 217 || marker === 218) break;
    const length = bytes.readUInt16BE(position);
    if ([192, 193, 194].includes(marker)) {
      return { width: bytes.readUInt16BE(position + 5), height: bytes.readUInt16BE(position + 3) };
    }
    position += length;
  }
  throw new Error('Controlled fixture JPEG dimensions were not found.');
}

test('real SSE browser supports live input/pointer, popup/redirect/back, bounded reconnect and expiry at low latency', async (t) => {
  const secret = 'synthetic-realtime-key';
  let offset = 0;
  const now = () => Date.now() + offset;
  const requests = [];
  let delayNextDocument = 0, documentsLoaded = 0;
  const reply = (body, extra = {}) => ({
    status: 200, headers: { 'content-type': 'text/html' }, setCookies: [],
    body: Buffer.from(body), ...extra,
  });
  const fetcher = new PublicFetcher({
    lookup: async () => [{ address: '93.184.216.34', family: 4 }],
    transport: async (url, options) => {
      requests.push(url.href);
      assert.equal(options.address.address, '93.184.216.34');
      assert.equal(options.headers.authorization, undefined);
      if (url.pathname === '/start') return reply('', {
        status: 302, headers: { location: '/ui/index' },
        setCookies: ['a=one; Path=/; HttpOnly', 'b=two; Path=/'],
      });
      assert.match(options.headers.cookie, /a=one/);
      assert.match(options.headers.cookie, /b=two/);
      if (url.pathname === '/ui/index') {
        if (delayNextDocument) {
          const wait = delayNextDocument;
          delayNextDocument = 0;
          await delay(wait);
        }
        documentsLoaded++;
        return reply(`<title>Live fixture</title>
        <input id="input" style="position:fixed;left:20px;top:20px;width:300px;height:40px">
        <a href="./next" style="position:fixed;left:20px;top:80px;width:90px;height:30px">Next</a>
        <a href="./popup-start" target="_blank" style="position:fixed;left:130px;top:80px;width:90px;height:30px">Popup</a>
        <style>#submenu{display:none}#hoverable:hover #submenu{display:block}</style>
        <div id="hoverable" style="position:fixed;left:400px;top:80px;width:100px;height:40px">
          Hover menu<div id="submenu" style="position:absolute;top:40px;width:100px;height:40px">Menu option</div>
        </div>
        <div id="box" style="position:fixed;left:20px;top:140px;width:70px;height:40px;background:blue"></div>
        <article id="status" style="margin-top:220px;height:2000px">ready</article>
        <script>
          const field=document.getElementById('input'),output=document.getElementById('status'),box=document.getElementById('box');
          field.addEventListener('input',()=>output.textContent='typed:'+field.value.length);
          document.getElementById('hoverable').addEventListener('mouseenter',event=>output.textContent='hovered:'+event.buttons);
          let down=false;
          box.addEventListener('mousedown',()=>down=true);
          addEventListener('mousemove',event=>{if(down){box.style.left=event.clientX+'px';output.textContent='moved:'+event.clientX}});
          addEventListener('mouseup',event=>{if(down)output.textContent='released:'+event.clientX;down=false});
          let color=0;function paint(){box.style.background='hsl('+(color++%360)+',80%,50%)';requestAnimationFrame(paint)}paint();
        </script>`);
      }
      if (url.pathname === '/ui/next') return reply('<title>Next fixture</title><article>Next public page</article>');
      if (url.pathname === '/ui/popup-start') return reply('', { status: 302, headers: { location: './popup' } });
      if (url.pathname === '/ui/popup') return reply(`<title>Popup fixture</title><article>Popup public page</article>
        <button style="position:absolute;left:20px;top:20px;width:200px;height:40px" onclick="window.close()">Close popup</button>`);
      return reply('', { status: 404 });
    },
  });
  let browser, nativeBrowser;
  try {
    browser = await launchBrowser(fetcher, {
      async launch(options) { nativeBrowser = await chromium.launch(options); return nativeBrowser; },
    });
  }
  catch (error) {
    if (!process.env.BROWSER_TEST_REQUIRED && /Executable doesn't exist/.test(error.message)) {
      t.skip('Pinned Playwright Chromium is not installed.');
      return;
    }
    throw error;
  }
  const manager = new SessionManager(browser, { now });
  const server = createServer({ manager, secret, now });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const clients = [];
  t.after(async () => {
    await Promise.all(clients.map((client) => client.stop()));
    await manager.shutdown();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await browser.close();
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const token = (extra = {}) => {
    const encoded = [
      { alg: 'HS256' },
      { sub: 'live-user', wiki_id: 'live-wiki', aud: 'wiki-browser-import', iss: 'wiki-agent',
        exp: Math.floor(now() / 1000) + 120, ...extra },
    ].map((value) => Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
    return `Bearer ${encoded}.${createHmac('sha256', secret).update(encoded).digest('base64url')}`;
  };
  const call = (path, method = 'GET', body, authorization = token()) => fetch(base + path, {
    method, headers: { authorization, 'content-type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const started = performance.now();
  const session = await (await call('/sessions', 'POST', { url: 'https://example.com/start' })).json();
  const openMs = performance.now() - started;
  assert.equal(session.url, 'https://example.com/ui/index');
  const framesPath = `/sessions/${session.id}/frames`;
  assert.equal((await fetch(base + framesPath)).status, 401);
  const foreign = await call(framesPath, 'GET', undefined, token({ wiki_id: 'another-wiki' }));
  assert.equal(foreign.status, 404);
  assert.equal((await foreign.json()).detail.code, 'SESSION_NOT_OWNED');
  const connect = async (authorization = token()) => {
    const controller = new AbortController();
    const response = await fetch(base + framesPath, { headers: { authorization }, signal: controller.signal });
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type'), /text\/event-stream/);
    assert.equal(response.headers.get('cache-control'), 'no-store, no-transform');
    assert.equal(response.headers.get('x-accel-buffering'), 'no');
    const client = liveClient(response, controller);
    clients.push(client);
    return client;
  };
  const firstStarted = performance.now();
  let live = await connect();
  const initial = await live.next((frame) => frame.title === 'Live fixture', 'frame', 5000, 'initial frame');
  const firstFrameMs = performance.now() - firstStarted;
  assert.equal(initial.width, 1024);
  assert.equal(initial.height, 768);
  assert.equal(initial.can_go_back, false);
  assert.deepEqual([...Buffer.from(initial.image, 'base64').subarray(0, 2)], [255, 216]);
  assert.ok(initial.image.startsWith('/9j/'));
  assert.deepEqual(jpegSize(initial.image), { width: 1024, height: 768 });
  assert.ok(firstFrameMs < 1000);
  const actionTimes = [], pointerTimes = [];
  const action = async (value) => {
    const start = performance.now();
    const response = await call(`/sessions/${session.id}/actions`, 'POST', value);
    const elapsed = performance.now() - start;
    assert.equal(response.status, 200);
    actionTimes.push(elapsed);
    if (value.action === 'pointer') pointerTimes.push(elapsed);
    return response.json();
  };
  const noHistory = await call(`/sessions/${session.id}/actions`, 'POST', { action: 'back' });
  assert.equal(noHistory.status, 400);
  assert.equal((await noHistory.json()).detail.code, 'NO_HISTORY');
  await action({ action: 'pointer', phase: 'down', x: 50, y: 40 });
  await action({ action: 'pointer', phase: 'up', x: 50, y: 40 });
  for (let i = 0; i < 12; i++) await action({ action: 'type', text: 'a' });
  const changed = await live.next((frame) => frame.image !== initial.image, 'frame', 5000, 'updated input frame');
  assert.ok(changed.image.length <= 1024 * 1024);
  assert.match((await (await call(`/sessions/${session.id}/capture`)).json()).html, /typed:12/);
  await action({ action: 'key', key: 'Backspace' });
  await action({ action: 'scroll', delta: 120 });
  await action({ action: 'pointer', phase: 'move', x: 430, y: 90 });
  assert.match((await (await call(`/sessions/${session.id}/capture`)).json()).html, /hovered:0/);
  assert.equal(await nativeBrowser.contexts()[0].pages()[0].locator('#submenu').isVisible(), true);
  await action({ action: 'pointer', phase: 'down', x: 40, y: 160 });
  for (let i = 0; i < 18; i++) await action({ action: 'pointer', phase: 'move', x: 80 + i * 10, y: 160 });
  await action({ action: 'pointer', phase: 'up', x: 350, y: 160 });
  assert.match((await (await call(`/sessions/${session.id}/capture`)).json()).html, /released:350/);
  await action({ action: 'pointer', phase: 'down', x: 360, y: 160 });
  const invalid = await call(`/sessions/${session.id}/actions`, 'POST', {
    action: 'pointer', phase: 'move', x: 1024, y: 160,
  });
  assert.equal(invalid.status, 400);
  assert.equal((await invalid.json()).detail.code, 'INVALID_ACTION');
  await action({ action: 'pointer', phase: 'move', x: 430, y: 90 });
  assert.match((await (await call(`/sessions/${session.id}/capture`)).json()).html, /hovered:0/);
  const countBefore = live.count;
  await delay(1000);
  assert.ok(live.count - countBefore <= 9);
  await action({ action: 'click', x: 30, y: 90 });
  await live.next((frame) => frame.url.endsWith('/ui/next') && frame.title === 'Next fixture' && frame.can_go_back === true,
    'frame', 5000, 'relative next navigation with history metadata');
  live.discardFrames();
  await action({ action: 'back' });
  await live.next((frame) => frame.url.endsWith('/ui/index') && frame.title === 'Live fixture', 'frame', 5000, 'back navigation');
  const hit = await nativeBrowser.contexts()[0].pages()[0].evaluate(() => {
    const element = document.elementFromPoint(150, 90);
    return { tag: element?.tagName, target: element?.getAttribute('target'), href: element?.getAttribute('href') };
  });
  assert.deepEqual(hit, { tag: 'A', target: '_blank', href: './popup-start' });
  await action({ action: 'click', x: 150, y: 90 });
  for (let i = 0; i < 100 && !requests.includes('https://example.com/ui/popup-start'); i++) await delay(10);
  assert.ok(requests.includes('https://example.com/ui/popup-start'), 'Pointer click must initiate the popup request.');
  await live.next((frame) => frame.url.endsWith('/ui/popup') && frame.title === 'Popup fixture', 'frame', 5000, 'popup navigation');
  const selected = await (await call(`/sessions/${session.id}`)).json();
  assert.ok(selected.url.includes('/ui/popup'), 'Popup must become the active owned page.');
  assert.ok(requests.includes('https://example.com/ui/popup-start'));
  assert.ok(requests.includes('https://example.com/ui/popup'));
  live.discardFrames();
  await action({ action: 'click', x: 50, y: 40 });
  await live.next((frame) => frame.url.endsWith('/ui/index') && frame.title === 'Live fixture', 'frame', 5000, 'popup close restores parent');
  const reloadDocument = documentsLoaded + 1;
  delayNextDocument = 700;
  live.discardFrames();
  const reloadStart = performance.now();
  await action({ action: 'reload' });
  assert.ok(performance.now() - reloadStart < 250, 'Reload acknowledgement must not wait for the 700ms fixture response.');
  await live.next((frame) => documentsLoaded >= reloadDocument && frame.url.endsWith('/ui/index') && frame.title === 'Live fixture',
    'frame', 5000, 'asynchronous reload');
  await action({ action: 'pointer', phase: 'down', x: 40, y: 160 });
  await action({ action: 'pointer', phase: 'move', x: 280, y: 160 });
  await live.stop();
  await delay(80);
  assert.equal(manager.sessions.size, 1);
  assert.match((await (await call(`/sessions/${session.id}/capture`)).json()).html, /released:280/);
  const reconnectStarted = performance.now();
  live = await connect();
  await live.next((frame) => frame.title === 'Live fixture');
  const reconnectMs = performance.now() - reconnectStarted;
  assert.ok(reconnectMs < 1000);
  await action({ action: 'pointer', phase: 'move', x: 430, y: 90 });
  assert.match((await (await call(`/sessions/${session.id}/capture`)).json()).html, /hovered:0/);
  const short = await connect(token({ exp: Math.floor(now() / 1000) + 2 }));
  const overflow = await call(framesPath);
  assert.equal(overflow.status, 429);
  assert.equal((await overflow.json()).detail.code, 'BROWSER_CAPACITY');
  assert.equal((await short.next(() => true, 'error', 4000)).code, 'UNAUTHORIZED');
  await short.stop();
  offset += SESSION_LIMITS.ttl;
  await manager.sweep();
  assert.equal((await live.next(() => true, 'error')).code, 'SESSION_EXPIRED');
  assert.equal(manager.sessions.size, 0);
  assert.equal(manager.live.size, 0);
  assert.equal((await call(framesPath)).status, 404);
  const p95 = (values) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * 0.95) - 1];
  console.log(JSON.stringify({
    fixture: 'realtime', openMs: Math.round(openMs), firstFrameMs: Math.round(firstFrameMs),
    reconnectMs: Math.round(reconnectMs), actionSamples: actionTimes.length,
    actionP95Ms: Math.round(p95(actionTimes)), pointerP95Ms: Math.round(p95(pointerTimes)),
  }));
  assert.ok(p95(actionTimes) < 250, 'Controlled fixture action p95 must remain below 250ms.');
  assert.ok(p95(pointerTimes) < 250, 'Controlled fixture pointer p95 must remain below 250ms.');
});

test('open and first public-URL frame do not wait for a heavy document or destroy the session on disconnect', async (t) => {
  let slowReady = false;
  const fetcher = new PublicFetcher({
    lookup: async () => [{ address: '93.184.216.34', family: 4 }],
    transport: async (url) => {
      if (url.pathname === '/slow.js') {
        await delay(1500);
        slowReady = true;
        return { status: 200, headers: { 'content-type': 'application/javascript' }, setCookies: [], body: Buffer.from('') };
      }
      return {
        status: 200, headers: { 'content-type': 'text/html' }, setCookies: [],
        body: Buffer.from('<title>Heavy fixture</title><article>Visible while loading</article><script src="/slow.js"></script>'),
      };
    },
  });
  let browser;
  try { browser = await launchBrowser(fetcher); }
  catch (error) {
    if (!process.env.BROWSER_TEST_REQUIRED && /Executable doesn't exist/.test(error.message)) {
      t.skip('Pinned Playwright Chromium is not installed.');
      return;
    }
    throw error;
  }
  const manager = new SessionManager(browser);
  const secret = 'synthetic-heavy-fixture-key';
  const server = createServer({ manager, secret });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  let client;
  t.after(async () => {
    await client?.stop();
    await manager.shutdown();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await browser.close();
  });
  const encoded = [
    { alg: 'HS256' },
    { sub: 'heavy-user', wiki_id: 'heavy-wiki', iss: 'wiki-agent', aud: 'wiki-browser-import', exp: Math.floor(Date.now() / 1000) + 120 },
  ].map((value) => Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
  const authorization = `Bearer ${encoded}.${createHmac('sha256', secret).update(encoded).digest('base64url')}`;
  const headers = { authorization, 'content-type': 'application/json' };
  const base = `http://127.0.0.1:${server.address().port}`;
  const start = performance.now();
  const created = await fetch(base + '/sessions', {
    method: 'POST', headers, body: JSON.stringify({ url: 'https://example.com/heavy' }),
  });
  assert.equal(created.status, 201);
  const session = await created.json();
  const openMs = performance.now() - start;
  assert.ok(openMs < 700);
  assert.equal(slowReady, false);
  assert.equal(session.url, 'https://example.com/heavy');
  const controller = new AbortController();
  const streamed = await fetch(base + `/sessions/${session.id}/frames`, { headers, signal: controller.signal });
  assert.equal(streamed.status, 200);
  assert.equal(streamed.headers.get('content-type'), 'text/event-stream');
  client = liveClient(streamed, controller);
  const frame = await client.next();
  assert.equal(frame.url, 'https://example.com/heavy');
  assert.equal(slowReady, false);
  assert.equal(typeof frame.can_go_back, 'boolean');
  assert.deepEqual(jpegSize(frame.image), { width: 1024, height: 768 });
  assert.ok(frame.image.startsWith('/9j/'));
  await client.stop();
  assert.equal(manager.sessions.has(session.id), true);
  for (let i = 0; i < 200 && !slowReady; i++) await delay(10);
  assert.equal(slowReady, true);
  assert.equal((await fetch(base + `/sessions/${session.id}`, { headers })).status, 200);
  assert.equal((await fetch(base + `/sessions/${session.id}`, { method: 'DELETE', headers })).status, 204);
  assert.equal(manager.sessions.size, 0);
  console.log(JSON.stringify({ fixture: 'heavy-document', openMs: Math.round(openMs), delayedScriptMs: 1500 }));
});
