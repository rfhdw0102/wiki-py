import { ServiceError } from './errors.js';

export class ResourceSlots {
  constructor(limit, signal) {
    this.limit = limit;
    this.active = 0;
    this.waiters = [];
    this.closed = signal.aborted;
    signal.addEventListener('abort', () => {
      this.closed = true;
      for (const waiter of this.waiters.splice(0)) waiter.reject(this.cancelled());
    }, { once: true });
  }

  cancelled() {
    return new ServiceError(503, 'BROWSER_RENDER_FAILED', 'The browser session was closed.');
  }

  acquire() {
    if (this.closed) return Promise.reject(this.cancelled());
    if (this.active < this.limit) {
      this.active++;
      return Promise.resolve(this.lease());
    }
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }

  lease() {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const next = this.waiters.shift();
      if (next && !this.closed) next.resolve(this.lease());
      else this.active--;
    };
  }
}
