import http from 'node:http';
import { authenticate } from './auth.js';
import { fail, safeError } from './errors.js';
import { validateAction } from './sessions.js';
import { streamFrames } from './sse.js';

const MAX_BODY = 16384;

async function jsonBody(request) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] || '')) {
    fail(415, 'unsupported_media_type', 'An application/json request body is required.');
  }
  if (Number(request.headers['content-length']) > MAX_BODY) fail(413, 'body_limit', 'The request body is too large.');
  let bytes = 0;
  const chunks = [];
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > MAX_BODY) fail(413, 'body_limit', 'The request body is too large.');
    chunks.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error();
    return value;
  } catch {
    fail(400, 'invalid_json', 'A JSON object is required.');
  }
}

function json(response, status, data) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  response.end(JSON.stringify(data));
}

export function createServer({ manager, secret, now = Date.now }) {
  let inflight = 0;
  const server = http.createServer({ maxHeaderSize: 8192 }, async (request, response) => {
    response.setHeader('cache-control', 'no-store');
    response.setHeader('x-content-type-options', 'nosniff');
    if (++inflight > 64) {
      inflight--;
      json(response, 503, { detail: { code: 'BROWSER_CAPACITY', message: 'The browser service is busy.' } });
      return;
    }
    try {
      const pathname = new URL(request.url, 'http://browser').pathname;
      if (request.method === 'GET' && pathname === '/health') {
        json(response, manager.stopped || manager.browser.healthy?.() === false ? 503 : 200,
          { status: manager.stopped || manager.browser.healthy?.() === false ? 'unavailable' : 'ok' });
        return;
      }
      const owner = authenticate(request.headers.authorization, secret, now());
      if (owner.streamSessionId &&
          (request.method !== 'GET' || pathname !== `/sessions/${owner.streamSessionId}/frames`)) {
        fail(403, 'forbidden', 'This token can only read its bound live stream.');
      }
      const run = (id, action) => manager.run(id, owner, (handle, session) => {
        authenticate(request.headers.authorization, secret, now());
        return action(handle, session);
      });
      if (request.method === 'POST' && ['/render', '/sessions'].includes(pathname)) {
        const body = await jsonBody(request);
        if (Object.keys(body).some((key) => key !== 'url')) fail(400, 'invalid_request', 'Only a URL may be supplied.');
        const session = await manager.create(owner, body.url);
        if (pathname === '/sessions') json(response, 201, session);
        else {
          try {
            const capture = await run(session.id, (handle) => handle.capture({ settle: true }));
            json(response, 200, capture);
          } finally {
            const pending = manager.sessions.get(session.id);
            if (pending) await manager.close(pending);
          }
        }
        return;
      }
      const match = /^\/sessions\/([A-Za-z0-9_-]{32})(?:\/(screenshot|actions|capture|frames))?$/.exec(pathname);
      if (!match) fail(404, 'not_found', 'The endpoint was not found.');
      const [, id, action] = match;
      // Validate ownership even for unsupported actions/methods.
      manager.owned(id, owner);
      if (request.method === 'DELETE' && !action) {
        await run(id, (_handle, session) => manager.close(session));
        response.writeHead(204);
        response.end();
      } else if (request.method === 'GET' && !action) {
        json(response, 200, await run(id, (handle, session) => manager.metadata(session, handle)));
      } else if (request.method === 'GET' && action === 'capture') {
        json(response, 200, await run(id, (handle) => handle.capture()));
      } else if (request.method === 'GET' && action === 'screenshot') {
        const image = await run(id, (handle) => handle.screenshot());
        response.writeHead(200, { 'content-type': 'image/png', 'content-length': image.length, 'cache-control': 'no-store' });
        response.end(image);
      } else if (request.method === 'GET' && action === 'frames') {
        const claims = JSON.parse(Buffer.from(request.headers.authorization.slice(7).split('.')[1], 'base64url'));
        await run(id, (handle, session) => streamFrames({
          request, response, handle, now,
          expires: Math.min(session.expires, claims.exp * 1000),
          guard: () => {
            authenticate(request.headers.authorization, secret, now());
            manager.owned(id, owner);
          },
        }));
      } else if (request.method === 'POST' && action === 'actions') {
        const session = manager.owned(id, owner);
        const generation = session.handle.inputGeneration?.();
        response.once('close', () => {
          if (!response.writableFinished) void session.handle.resetInput?.(generation);
        });
        try {
          const input = validateAction(await jsonBody(request));
          const result = await run(id, async (handle, state) => {
            if (request.aborted || response.destroyed) fail(502, 'BROWSER_RENDER_FAILED', 'The browser action was cancelled.');
            await handle.action(input, generation);
            return manager.metadata(state, handle);
          });
          json(response, 200, result);
        } catch (error) {
          await session.handle.resetInput?.(generation);
          throw error;
        }
      } else fail(405, 'method_not_allowed', 'The method is not supported by this endpoint.');
    } catch (error) {
      const safe = safeError(error);
      if (!response.headersSent && !response.destroyed) {
        if (safe.status === 401) response.setHeader('www-authenticate', 'Bearer');
        if (safe.status === 413) response.setHeader('connection', 'close');
        json(response, safe.status, { detail: { code: safe.code, message: safe.message } });
      } else response.destroy();
    } finally { inflight--; }
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  server.timeout = 45000;
  server.keepAliveTimeout = 5000;
  server.maxRequestsPerSocket = 100;
  server.maxConnections = 128;
  server.on('clientError', (_error, socket) => {
    const body = JSON.stringify({ detail: { code: 'INVALID_REQUEST', message: 'The HTTP request is invalid.' } });
    socket.end(`HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
  });
  return server;
}
