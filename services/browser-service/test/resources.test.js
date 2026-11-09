import test from 'node:test';
import assert from 'node:assert/strict';
import { ResourceSlots } from '../src/resources.js';

test('resource concurrency is queued and bounded instead of rejecting ordinary page bursts', async () => {
  const controller = new AbortController();
  const slots = new ResourceSlots(2, controller.signal);
  const first = await slots.acquire();
  const second = await slots.acquire();
  let acquired = false;
  const queued = slots.acquire().then((release) => { acquired = true; return release; });
  await Promise.resolve();
  assert.equal(acquired, false);
  assert.equal(slots.active, 2);
  first();
  const third = await queued;
  assert.equal(slots.active, 2);
  first();
  assert.equal(slots.active, 2);
  second();
  third();
  assert.equal(slots.active, 0);
});

test('closing a context cancels pending resource leases without leaking active slots', async () => {
  const controller = new AbortController();
  const slots = new ResourceSlots(1, controller.signal);
  const release = await slots.acquire();
  const pending = assert.rejects(slots.acquire(), { code: 'BROWSER_RENDER_FAILED' });
  controller.abort();
  await pending;
  release();
  assert.equal(slots.active, 0);
  assert.equal(slots.waiters.length, 0);
  await assert.rejects(slots.acquire(), { code: 'BROWSER_RENDER_FAILED' });
});
