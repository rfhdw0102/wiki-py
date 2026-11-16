import assert from "node:assert/strict";
import test from "node:test";
import {
  BrowserInputQueue, browserFrames, browserKeyAction, browserTextActions, fitBrowserViewport
} from "../src/app/sources/browser-stream.ts";

const frame = { image: "/9j/AA==", url: "https://example.com/", title: "Article", width: 1024, height: 768 };
const event = `event: frame\ndata: ${JSON.stringify(frame)}\n\n`;
const encode = (text) => new TextEncoder().encode(text);

test("live frame decoding handles split chunks, heartbeat, CRLF and stream cleanup", async () => {
  let cancelled = false;
  const source = new ReadableStream({
    start(controller) {
      const text = (": heartbeat\n\n" + event).replace(/\n/g, "\r\n");
      for (let index = 0; index < text.length; index += 7) controller.enqueue(encode(text.slice(index, index + 7)));
    },
    cancel() { cancelled = true; }
  });
  for await (const value of browserFrames(source)) {
    assert.equal(value.url, frame.url);
    assert.equal(value.image, frame.image);
    break;
  }
  assert.equal(cancelled, true);
  assert.equal(source.locked, false);
});

test("invalid/oversized live events and errors never echo private payloads", async () => {
  for (const content of [
    'event: frame\ndata: SECRET_TEST_MARKER\n\n',
    'event: error\ndata: {"code":"SESSION_EXPIRED","message":"SECRET_TEST_MARKER"}\n\n',
    "event: frame\ndata: " + "x".repeat(2 * 1024 * 1024 + 1)
  ]) {
    const stream = new ReadableStream({ start(controller) { controller.enqueue(encode(content)); controller.close(); } });
    await assert.rejects(async () => {
      for await (const value of browserFrames(stream)) assert.fail(JSON.stringify(value));
    }, (error) => error instanceof Error && !error.message.includes("SECRET_TEST_MARKER"));
  }
});

test("service terminal expiry events show a restart hint rather than reconnecting expired credentials", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encode('event: error\ndata: {"detail":{"code":"SESSION_EXPIRED","message":"private"}}\n\n'));
      controller.close();
    }
  });
  await assert.rejects(async () => {
    for await (const value of browserFrames(stream)) assert.fail(JSON.stringify(value));
  }, /会话已结束/);
});

test("viewport sizing fits both portrait and landscape without stretched click coordinates", () => {
  assert.deepEqual(fitBrowserViewport(1280, 600), { width: 800, height: 600 });
  assert.deepEqual(fitBrowserViewport(320, 600), { width: 320, height: 240 });
});

test("direct keyboard mapping and Unicode paste preserve remote input", () => {
  assert.deepEqual(browserKeyAction({ key: "a", ctrlKey: false, metaKey: true, altKey: false }),
    { action: "key", key: "Control+A" });
  assert.deepEqual(browserKeyAction({ key: "ArrowLeft", ctrlKey: false, metaKey: false, altKey: true }),
    { action: "back" });
  assert.equal(browserKeyAction({ key: "v", ctrlKey: true, metaKey: false, altKey: false }), null);
  const text = "a".repeat(999) + "😀中文" + "b".repeat(1000);
  const actions = browserTextActions(text);
  assert.equal(actions.map((action) => action.text).join(""), text);
  assert.ok(actions.every((action) => action.text.length <= 1000 && !/[\uD800-\uDBFF]$/.test(action.text)));
  assert.throws(() => browserTextActions("x".repeat(10001)), /分段输入/);
});

test("input queue keeps pointer/key ordering but coalesces obsolete moves and typing", async () => {
  const sent = [];
  const queue = new BrowserInputQueue(async (action) => { sent.push(action); }, assert.fail);
  queue.enqueue({ action: "pointer", phase: "down", x: 1, y: 1 });
  for (let index = 2; index < 100; index++) queue.enqueue({ action: "pointer", phase: "move", x: index, y: 1 });
  queue.enqueue({ action: "pointer", phase: "up", x: 100, y: 1 });
  queue.enqueue({ action: "type", text: "中" });
  queue.enqueue({ action: "type", text: "文" });
  queue.enqueue({ action: "key", key: "Enter" });
  await queue.flush();
  assert.deepEqual(sent, [
    { action: "pointer", phase: "down", x: 1, y: 1 },
    { action: "pointer", phase: "move", x: 99, y: 1 },
    { action: "pointer", phase: "up", x: 100, y: 1 },
    { action: "type", text: "中文" }, { action: "key", key: "Enter" }
  ]);
});

test("failed or cancelled input never silently proceeds to confirmation", async () => {
  let notified = false;
  const queue = new BrowserInputQueue(() => { throw new Error("输入失败"); }, () => { notified = true; });
  queue.enqueue({ action: "type", text: "temporary text" });
  await assert.rejects(queue.flush(), /输入失败/);
  assert.equal(notified, true);
  let sent = false;
  const cancelled = new BrowserInputQueue(async () => { sent = true; }, assert.fail);
  cancelled.enqueue({ action: "type", text: "temporary text" });
  cancelled.dispose();
  await cancelled.flush();
  assert.equal(sent, false);
});
