import { randomBytes } from 'node:crypto';
import { fail, ServiceError } from './errors.js';
import { publicURL } from './network.js';

export const SESSION_LIMITS = Object.freeze({ ttl: 15 * 60 * 1000, total: 8, perUser: 2, operationTimeout: 35000 });

export class SessionManager {
  constructor(browser, { now = Date.now, limits = SESSION_LIMITS } = {}) {
    this.browser = browser;
    this.now = now;
    this.limits = limits;
    this.sessions = new Map();
    this.live = new Set();
    this.expired = new Map();
    this.stopped = false;
  }

  async create(owner, url) {
    publicURL(url);
    await this.sweep();
    if (this.stopped) fail(503, 'unavailable', 'The browser service is shutting down.');
    if (this.live.size >= this.limits.total ||
        [...this.live].filter((session) => session.owner.user === owner.user).length >= this.limits.perUser) {
      fail(429, 'session_limit', 'The browser session limit has been reached.');
    }
    const session = {
      id: randomBytes(24).toString('base64url'), owner,
      expires: this.now() + this.limits.ttl, queue: Promise.resolve(), handle: null, pending: 0,
    };
    this.sessions.set(session.id, session);
    this.live.add(session);
    try {
      session.opening = this.browser.create();
      session.handle = await this.deadline(session, session.opening);
      await this.run(session.id, owner, async (handle) => handle.navigate(url));
      return await this.run(session.id, owner, (handle) => this.metadata(session, handle));
    } catch (error) {
      await this.close(session);
      throw error;
    }
  }

  owned(id, owner) {
    const session = this.sessions.get(id);
    const expired = this.expired.get(id);
    const known = session || (expired && expired.until > this.now() ? expired : null);
    if (!known) fail(404, 'SESSION_NOT_FOUND', 'The browser session was not found.');
    if (known.owner.user !== owner.user || known.owner.wiki !== owner.wiki) {
      fail(404, 'SESSION_NOT_OWNED', 'The browser session does not belong to this user and wiki.');
    }
    if (!session || session.expires <= this.now()) fail(404, 'SESSION_EXPIRED', 'The browser session has expired.');
    if (session.closing) fail(404, 'SESSION_NOT_FOUND', 'The browser session was not found.');
    return session;
  }

  async deadline(session, operation) {
    let timer;
    try {
      return await Promise.race([
        operation,
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            void this.close(session);
            reject(new ServiceError(504, 'operation_timeout', 'The browser operation timed out.'));
          }, this.limits.operationTimeout);
          timer.unref();
        }),
      ]);
    } finally { clearTimeout(timer); }
  }

  run(id, owner, action) {
    const session = this.owned(id, owner);
    if (session.pending >= 16) fail(429, 'action_limit', 'Too many browser actions are queued.');
    session.pending++;
    const result = session.queue.then(async () => {
      this.owned(id, owner);
      return this.deadline(session, Promise.resolve().then(() => action(session.handle, session)));
    }).finally(() => { session.pending--; });
    session.queue = result.catch(() => {});
    return result;
  }

  async metadata(session, handle = session.handle) {
    return { id: session.id, ...await handle.metadata(), expires_at: new Date(session.expires).toISOString() };
  }

  async remove(id, owner) {
    return this.run(id, owner, (_handle, session) => this.close(session));
  }

  async close(session) {
    if (session.closing) return session.closing;
    this.sessions.delete(session.id);
    if (session.expires <= this.now()) {
      this.expired.set(session.id, { owner: session.owner, until: this.now() + this.limits.ttl });
      if (this.expired.size > 128) this.expired.delete(this.expired.keys().next().value);
    }
    session.closing = (async () => {
      if (session.handle) {
        try {
          await session.handle.close(new ServiceError(404,
            session.expires <= this.now() ? 'SESSION_EXPIRED' : 'SESSION_NOT_FOUND',
            session.expires <= this.now() ? 'The browser session has expired.' : 'The browser session was closed.'));
        } catch {}
        finally { this.live.delete(session); }
      } else {
        void session.opening?.then((handle) => handle.close()).catch(() => {})
          .finally(() => { this.live.delete(session); });
        if (!session.opening) this.live.delete(session);
      }
    })();
    return session.closing;
  }

  async sweep() {
    for (const [id, entry] of this.expired) {
      if (entry.until <= this.now()) this.expired.delete(id);
    }
    await Promise.all([...this.sessions.values()]
      .filter((session) => session.expires <= this.now()).map((session) => this.close(session)));
  }

  async shutdown() {
    this.stopped = true;
    await Promise.all([...this.live].map((session) => this.close(session)));
    this.expired.clear();
  }
}

const keys = new Set([
  'Enter', 'Tab', 'Shift+Tab', 'Escape', 'Backspace', 'Delete', 'ArrowLeft', 'ArrowRight',
  'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'Space',
  'Control+A', 'Meta+A',
]);

export function validateAction(value) {
  if (!value || Array.isArray(value) || typeof value !== 'object') fail(400, 'invalid_action', 'A valid browser action is required.');
  const valid = {
    click: Number.isFinite(value.x) && Number.isFinite(value.y) &&
      value.x >= 0 && value.x < 1024 && value.y >= 0 && value.y < 768,
    drag: Number.isInteger(value.x) && Number.isInteger(value.y) &&
      Number.isInteger(value.to_x) && Number.isInteger(value.to_y) &&
      value.x >= 0 && value.x < 1024 && value.y >= 0 && value.y < 768 &&
      value.to_x >= 0 && value.to_x < 1024 && value.to_y >= 0 && value.to_y < 768,
    type: typeof value.text === 'string' && value.text.length > 0 && value.text.length <= 4096,
    key: keys.has(value.key),
    scroll: Number.isInteger(value.delta) && Math.abs(value.delta) <= 3000,
    reload: true,
    back: true,
    pointer: ['down', 'move', 'up'].includes(value.phase) &&
      Number.isInteger(value.x) && Number.isInteger(value.y) &&
      value.x >= 0 && value.x < 1024 && value.y >= 0 && value.y < 768,
  };
  const fields = {
    click: ['action', 'x', 'y'], type: ['action', 'text'], key: ['action', 'key'],
    drag: ['action', 'x', 'y', 'to_x', 'to_y'],
    scroll: ['action', 'delta'], reload: ['action'],
    back: ['action'], pointer: ['action', 'phase', 'x', 'y'],
  };
  if (!Object.hasOwn(valid, value.action) || !valid[value.action] ||
      Object.keys(value).some((key) => !fields[value.action].includes(key))) {
    fail(400, 'invalid_action', 'The browser action parameters are invalid.');
  }
  return value;
}
