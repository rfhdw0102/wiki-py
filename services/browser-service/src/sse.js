import { safeError } from './errors.js';

export const HEARTBEAT_INTERVAL = 10000;

export async function streamFrames({ request, response, handle, guard, expires, now, heartbeatInterval = HEARTBEAT_INTERVAL }) {
  let stopped = false, blocked = false, pending = null, unsubscribe;
  let expiryTimer, heartbeat;
  const cleanup = () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(expiryTimer);
    clearInterval(heartbeat);
    pending = null;
    unsubscribe?.();
    response.off('drain', drain);
  };
  const finish = (error) => {
    if (stopped) return;
    const safe = safeError(error);
    if (!response.headersSent && !response.destroyed) {
      response.writeHead(safe.status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      cleanup();
      response.end(JSON.stringify({ detail: { code: safe.code, message: safe.message } }));
      return;
    }
    // Do not grow the writable buffer with an error if a slow client is already blocked.
    if (!response.destroyed && !blocked) {
      response.write(`event: error\ndata: ${JSON.stringify({ code: safe.code, message: safe.message })}\n\n`);
    }
    cleanup();
    response.end();
  };
  const send = (frame) => {
    if (stopped || response.destroyed) return;
    try { guard(); } catch (error) { finish(error); return; }
    if (blocked) { pending = frame; return; }
    blocked = !response.write(`event: frame\ndata: ${JSON.stringify(frame)}\n\n`);
  };
  const drain = () => {
    blocked = false;
    const frame = pending;
    pending = null;
    if (frame) send(frame);
  };
  response.once('close', cleanup);
  response.once('error', cleanup);
  response.on('drain', drain);
  try {
    guard();
    // Reserve capacity before committing SSE headers, so unauthorized/capacity errors stay JSON.
    unsubscribe = await handle.frames((frame) => {
      if (!response.headersSent) {
        response.writeHead(200, {
          'content-type': 'text/event-stream', 'cache-control': 'no-store, no-transform',
          'x-accel-buffering': 'no', connection: 'keep-alive',
        });
        response.flushHeaders();
      }
      send(frame);
    }, finish);
    if (stopped || response.destroyed) { unsubscribe(); return; }
    if (!response.headersSent) {
      response.writeHead(200, {
        'content-type': 'text/event-stream', 'cache-control': 'no-store, no-transform',
        'x-accel-buffering': 'no', connection: 'keep-alive',
      });
      response.flushHeaders();
    }
    response.setTimeout(0);
    request.socket.setTimeout(0);
    expiryTimer = setTimeout(() => {
      try { guard(); } catch (error) { finish(error); return; }
      cleanup();
      response.end();
    }, Math.max(1, expires - now() + 1));
    expiryTimer.unref();
    heartbeat = setInterval(() => {
      if (stopped || blocked || response.destroyed) return;
      try { guard(); } catch (error) { finish(error); return; }
      blocked = !response.write(': heartbeat\n\n');
    }, heartbeatInterval);
    heartbeat.unref();
  } catch (error) {
    cleanup();
    if (!response.headersSent) throw error;
    response.end();
  }
}
