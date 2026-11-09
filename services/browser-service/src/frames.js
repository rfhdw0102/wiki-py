import { performance } from 'node:perf_hooks';
import { fail, safeError, ServiceError } from './errors.js';

export const FRAME_LIMITS = Object.freeze({ interval: 125, maxBytes: 1024 * 1024, subscribers: 2 });

export class FrameHub {
  constructor(context, getPage, getMetadata, onIdle = () => {}) {
    this.context = context;
    this.getPage = getPage;
    this.getMetadata = getMetadata;
    this.onIdle = onIdle;
    this.subscribers = new Set();
    this.latest = null;
    this.pending = null;
    this.lastFrame = -Infinity;
    this.generation = 0;
    this.transition = Promise.resolve();
    this.closed = false;
    this.cdp = null;
  }

  async subscribe(frame, end) {
    if (this.closed) fail(404, 'SESSION_NOT_FOUND', 'The browser session was closed.');
    if (this.subscribers.size >= FRAME_LIMITS.subscribers) fail(429, 'BROWSER_CAPACITY', 'Too many live streams are connected.');
    const subscriber = { frame, end };
    this.subscribers.add(subscriber);
    if (this.latest) frame(this.latest);
    if (!this.cdp) await this.switchPage();
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.subscribers.delete(subscriber);
      if (!this.subscribers.size) {
        this.clearFrames(true);
        void this.switchPage();
        this.onIdle();
      }
    };
  }

  clearFrames(resetClock = false) {
    clearTimeout(this.flushTimer);
    clearTimeout(this.initialTimer);
    this.flushTimer = null;
    this.initialTimer = null;
    this.latest = null;
    this.pending = null;
    if (resetClock) this.lastFrame = -Infinity;
  }

  invalidate() {
    this.clearFrames();
    this.initialFrame(this.cdp, this.generation);
  }

  switchPage() {
    const generation = ++this.generation;
    this.clearFrames();
    this.transition = this.transition.catch(() => {}).then(async () => {
      await this.stopCDP();
      if (generation !== this.generation || this.closed || !this.subscribers.size) return;
      const page = this.getPage();
      if (!page || page.isClosed()) return;
      const cdp = await this.context.newCDPSession(page);
      if (generation !== this.generation || this.closed || !this.subscribers.size) {
        await cdp.detach().catch(() => {});
        return;
      }
      this.cdp = cdp;
      this.frameListener = ({ data, sessionId }) => {
        void cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
        if (generation !== this.generation || this.closed) return;
        this.accept(data);
      };
      cdp.on('Page.screencastFrame', this.frameListener);
      await cdp.send('Page.enable');
      await cdp.send('Page.startScreencast', {
        format: 'jpeg', quality: 65, maxWidth: 1024, maxHeight: 768, everyNthFrame: 2,
      });
      this.initialFrame(cdp, generation);
    }).catch((error) => {
      if (!this.closed) this.finish(safeError(error));
    });
    return this.transition;
  }

  initialFrame(cdp, generation) {
    if (!cdp || !this.subscribers.size || this.closed) return;
    clearTimeout(this.initialTimer);
    this.initialTimer = setTimeout(async () => {
      if (this.latest || generation !== this.generation || this.closed) return;
      try {
        const { data } = await cdp.send('Page.captureScreenshot', {
          format: 'jpeg', quality: 65, fromSurface: true, captureBeyondViewport: false,
        });
        if (generation === this.generation && !this.closed) this.accept(data);
      } catch {
        // A page switch/navigation can invalidate the one-shot initial-frame fallback.
      }
    }, 200);
    this.initialTimer.unref();
  }

  accept(data) {
    if (typeof data !== 'string' || data.length > FRAME_LIMITS.maxBytes) {
      this.finish(new ServiceError(413, 'BROWSER_CAPACITY', 'The live frame exceeded its size limit.'));
      return;
    }
    this.pending = data;
    const delay = this.lastFrame + FRAME_LIMITS.interval - performance.now();
    if (delay <= 0) this.flush();
    else if (!this.flushTimer) {
      this.flushTimer = setTimeout(() => { this.flushTimer = null; this.flush(); }, Math.ceil(delay));
      this.flushTimer.unref();
    }
  }

  flush() {
    if (!this.pending || !this.subscribers.size || this.closed) return;
    let metadata;
    try { metadata = this.getMetadata(); }
    catch (error) { this.finish(safeError(error)); return; }
    if (!metadata) { this.pending = null; return; }
    this.latest = { image: this.pending, ...metadata };
    this.pending = null;
    this.lastFrame = performance.now();
    for (const subscriber of [...this.subscribers]) {
      try { subscriber.frame(this.latest); }
      catch (error) { this.subscribers.delete(subscriber); subscriber.end(safeError(error)); }
    }
  }

  metadataChanged() {
    if (this.latest) this.accept(this.latest.image);
  }

  finish(error = new ServiceError(404, 'SESSION_NOT_FOUND', 'The browser session was closed.')) {
    for (const subscriber of [...this.subscribers]) {
      try { subscriber.end(error); } catch {}
    }
    this.subscribers.clear();
    this.clearFrames(true);
    void this.switchPage();
  }

  async stopCDP() {
    const cdp = this.cdp;
    if (!cdp) return;
    this.cdp = null;
    cdp.off('Page.screencastFrame', this.frameListener);
    await cdp.send('Page.stopScreencast').catch(() => {});
    await cdp.detach().catch(() => {});
  }

  async close(error) {
    this.closed = true;
    this.finish(error);
    await this.transition;
    await this.stopCDP();
  }
}
