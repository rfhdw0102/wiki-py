import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { FrameHub, FRAME_LIMITS } from '../src/frames.js';
import { streamFrames, HEARTBEAT_INTERVAL } from '../src/sse.js';

const metadata = () => ({ url: 'https://example.com/', title: 'Fixture', width: 1024, height: 768 });

function fakeContext() {
  const sessions = [];
  return {
    sessions,
    async newCDPSession() {
      const cdp = new EventEmitter();
      cdp.commands = [];
      cdp.send = async (command) => {
        cdp.commands.push(command);
        return command === 'Page.captureScreenshot' ? { data: '/9j/initial' } : {};
      };
      cdp.detach = async () => { cdp.detached = true; };
      sessions.push(cdp);
      return cdp;
    },
  };
}

test('screencast acknowledges all frames, coalesces to 8fps, bounds subscribers and clears on disconnect/reconnect', async () => {
  const context = fakeContext();
  const page = { isClosed: () => false };
  const hub = new FrameHub(context, () => page, metadata);
  const received = [];
  const stop = await hub.subscribe((frame) => received.push(frame), () => {});
  const cdp = context.sessions[0];
  for (let i = 0; i < 1000; i++) cdp.emit('Page.screencastFrame', { data: `/9j/${i}`, sessionId: i });
  assert.equal(received.length, 1);
  assert.equal(hub.pending, '/9j/999');
  await delay(150);
  assert.equal(received.length, 2);
  assert.equal(received[1].image, '/9j/999');
  assert.equal(cdp.commands.filter((command) => command === 'Page.screencastFrameAck').length, 1000);
  const second = await hub.subscribe(() => {}, () => {});
  await assert.rejects(hub.subscribe(() => {}, () => {}), { code: 'BROWSER_CAPACITY' });
  stop();
  second();
  await hub.transition;
  assert.ok(cdp.commands.includes('Page.stopScreencast'));
  assert.equal(cdp.detached, true);
  assert.equal(cdp.listenerCount('Page.screencastFrame'), 0);
  assert.equal(hub.latest, null);
  assert.equal(hub.pending, null);
  const reconnected = [];
  const stopAgain = await hub.subscribe((frame) => reconnected.push(frame), () => {});
  await delay(250);
  assert.equal(reconnected[0].image, '/9j/initial');
  assert.equal(context.sessions.length, 2);
  stopAgain();
  await hub.close();
});

test('oversized frames produce safe capacity errors and switching pages detaches prior screencast', async () => {
  const context = fakeContext();
  let page = { isClosed: () => false };
  const errors = [];
  const hub = new FrameHub(context, () => page, metadata);
  await hub.subscribe(() => {}, (error) => errors.push(error));
  const first = context.sessions[0];
  page = { isClosed: () => false };
  await hub.switchPage();
  assert.equal(first.detached, true);
  context.sessions[1].emit('Page.screencastFrame', {
    data: 'x'.repeat(FRAME_LIMITS.maxBytes + 1), sessionId: 1,
  });
  assert.equal(errors[0].code, 'BROWSER_CAPACITY');
  assert.equal(hub.subscribers.size, 0);
  await hub.close();
});

test('SSE backpressure keeps only the newest pending frame and disconnect unsubscribes without replay', async () => {
  const response = new EventEmitter();
  const writes = [];
  let callback, stopped = 0;
  response.headersSent = false;
  response.destroyed = false;
  response.writeHead = () => { response.headersSent = true; };
  response.flushHeaders = () => {};
  response.setTimeout = () => {};
  response.write = (data) => { writes.push(data); return false; };
  response.end = () => { response.destroyed = true; response.emit('close'); };
  await streamFrames({
    request: { socket: { setTimeout() {} } }, response,
    handle: { async frames(frame) { callback = frame; return () => { stopped++; }; } },
    guard: () => {}, expires: Date.now() + 10000, now: Date.now,
  });
  for (let i = 0; i < 1000; i++) callback({ image: `${i}`, ...metadata() });
  assert.equal(writes.length, 1);
  response.emit('drain');
  assert.equal(writes.length, 2);
  assert.match(writes[1], /"image":"999"/);
  response.emit('close');
  callback({ image: 'after-close', ...metadata() });
  assert.equal(stopped, 1);
  assert.equal(writes.length, 2);
});

test('SSE sends idle heartbeats at a 10-second production interval and flat safe terminal errors', async () => {
  assert.equal(HEARTBEAT_INTERVAL, 10000);
  const response = new EventEmitter();
  const writes = [];
  let endStream, unsubscribed = 0;
  response.headersSent = false;
  response.destroyed = false;
  response.writeHead = (_status, headers) => {
    assert.equal(headers['content-type'], 'text/event-stream');
    response.headersSent = true;
  };
  response.flushHeaders = () => {};
  response.setTimeout = () => {};
  response.write = (data) => { writes.push(data); return true; };
  response.end = () => { response.destroyed = true; response.emit('close'); };
  await streamFrames({
    request: { socket: { setTimeout() {} } }, response,
    handle: { async frames(_frame, end) { endStream = end; return () => { unsubscribed++; }; } },
    guard: () => {}, expires: Date.now() + 10000, now: Date.now, heartbeatInterval: 10,
  });
  await delay(35);
  assert.ok(writes.includes(': heartbeat\n\n'));
  endStream(new Error('sensitive-action-input'));
  const block = writes.find((value) => value.startsWith('event: error'));
  const error = JSON.parse(/^data: (.+)$/m.exec(block)[1]);
  assert.deepEqual(Object.keys(error).sort(), ['code', 'message']);
  assert.equal(error.code, 'BROWSER_RENDER_FAILED');
  assert.doesNotMatch(error.message, /sensitive-action-input/);
  assert.equal(unsubscribed, 1);
});
