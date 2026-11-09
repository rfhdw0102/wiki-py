import http from 'node:http';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { CookieJar } from 'tough-cookie';
import { chromium } from 'playwright';
import { NETWORK_LIMITS, publicURL } from './network.js';
import { fail, safeError, ServiceError } from './errors.js';
import { FrameHub } from './frames.js';
import { ResourceSlots } from './resources.js';

export const VIEWPORT = Object.freeze({ width: 1024, height: 768 });

export function chromiumEnvironment(runtime, env = process.env) {
  return {
    PATH: env.PATH || '/usr/local/bin:/usr/bin:/bin',
    HOME: runtime, TMPDIR: runtime, LANG: 'C.UTF-8',
  };
}

export async function launchBrowser(fetcher, browserType = chromium) {
  const runtime = fileURLToPath(new URL('../.runtime/', import.meta.url));
  await mkdir(runtime, { recursive: true, mode: 0o700 });
  process.env.TMPDIR = runtime;
  const blocker = http.createServer((_request, response) => {
    response.writeHead(403);
    response.end();
  });
  blocker.on('connect', (_request, socket) => socket.destroy());
  blocker.on('upgrade', (_request, socket) => socket.destroy());
  blocker.requestTimeout = 1000;
  blocker.headersTimeout = 1000;
  await new Promise((resolve) => blocker.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await browserType.launch({
      headless: true,
      env: chromiumEnvironment(runtime),
      proxy: { server: `http://127.0.0.1:${blocker.address().port}`, bypass: '<-loopback>' },
      args: [
        '--disable-quic', '--disable-background-networking', '--dns-prefetch-disable',
        '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
      ],
    });
  } catch (error) {
    blocker.close();
    throw error;
  }
  return {
    create: () => createHandle(browser, fetcher),
    healthy: () => browser.isConnected(),
    close: async () => {
      await browser.close();
      blocker.closeAllConnections();
      await new Promise((resolve) => blocker.close(resolve));
    },
  };
}

export async function applyCookies(context, rawCookies, value) {
  const jar = new CookieJar();
  for (const raw of rawCookies) {
    let cookie;
    try {
      cookie = await jar.setCookie(raw, value);
    } catch {
      continue; // Malformed and unrelated-domain cookies are ignored, as in a browser.
    }
    if (!cookie) continue;
    if (cookie.secure && new URL(value).protocol !== 'https:') continue;
    const expires = cookie.expiryTime();
    await context.addCookies([{
      name: cookie.key, value: cookie.value,
      domain: cookie.hostOnly ? cookie.domain : `.${cookie.domain}`,
      path: cookie.path || '/', secure: cookie.secure, httpOnly: cookie.httpOnly,
      sameSite: cookie.sameSite === 'strict' ? 'Strict' : cookie.sameSite === 'none' ? 'None' : 'Lax',
      ...(expires !== Infinity ? { expires: Math.max(1, Math.floor(expires / 1000)) } : {}),
    }]);
  }
}

export async function createHandle(browser, fetcher) {
  const context = await browser.newContext({
    viewport: VIEWPORT, serviceWorkers: 'block', acceptDownloads: false,
    javaScriptEnabled: true, ignoreHTTPSErrors: false,
  });
  context.setDefaultTimeout(10000);
  context.setDefaultNavigationTimeout(25000);
  const controller = new AbortController();
  const slots = new ResourceSlots(NETWORK_LIMITS.concurrent, controller.signal);
  let requests = 0, bytes = 0, resourceErrors = 0, networkError = null, page, closed = false;
  const reportedResources = new Set();
  let inputGeneration = 0, releasing = Promise.resolve();
  const pages = new Map();
  const pendingNavigation = new Map();
  const pendingPopupNavigation = new Map();
  const cached = () => {
    if (networkError) throw networkError;
    return {
      url: publicURL(page.url()).href, title: pages.get(page)?.title || '',
      resource_errors: resourceErrors, ...VIEWPORT,
    };
  };
  const releasePointer = (invalidate = true) => {
    if (invalidate) inputGeneration++;
    const releases = [];
    for (const [target, state] of pages) {
      if (state.pointerDown) {
        state.pointerDown = false;
        releases.push(target.mouse.up().catch(() => {}));
      }
    }
    releasing = Promise.all([releasing, ...releases]).then(() => {});
    return releasing;
  };
  const hub = new FrameHub(context, () => page, () => {
    return { ...cached(), can_go_back: pages.get(page)?.canGoBack || false };
  }, releasePointer);
  const recordFailure = (error) => {
    if (error instanceof ServiceError) {
      networkError ??= error;
      for (const state of pages.values()) {
        state.loading = false;
        state.resolveLoad?.();
        state.resolveLoad = null;
        state.resolveCommit?.();
        state.resolveCommit = null;
      }
      hub.finish(networkError);
    }
  };
  const refreshTitle = async (target) => {
    const state = pages.get(target);
    if (!state || target.isClosed()) return;
    const url = target.url();
    try {
      const title = (await target.title()).slice(0, 512);
      if (target.url() === url && pages.has(target)) {
        state.title = title;
        if (target === page) hub.metadataChanged();
      }
    } catch {}
  };
  const refreshHistory = async (target) => {
    const state = pages.get(target);
    if (!state || target.isClosed()) return;
    const url = target.url();
    let cdp;
    try {
      cdp = await context.newCDPSession(target);
      const history = await cdp.send('Page.getNavigationHistory');
      const previous = history.entries[history.currentIndex - 1];
      let canGoBack = false;
      if (previous) {
        try { publicURL(previous.url); canGoBack = true; } catch {}
      }
      if (target.url() === url && pages.has(target)) {
        state.canGoBack = canGoBack;
        if (target === page) hub.metadataChanged();
      }
    } catch {}
    finally { await cdp?.detach().catch(() => {}); }
  };
  const activate = (target) => {
    if (target === page || target.isClosed()) return;
    void releasePointer(false);
    page = target;
    void target.bringToFront().catch(() => {});
    void hub.switchPage();
  };
  const adopt = (target) => {
    if (pages.has(target)) return pages.get(target);
    if (pages.size >= 4) {
      void target.close().catch(() => {});
      fail(429, 'BROWSER_CAPACITY', 'The browser tab limit has been reached.');
    }
    const state = {
      title: '', canGoBack: false, pointerDown: false, loading: false, resolveLoad: null,
      resolveCommit: null, command: null, promote: target !== page,
    };
    pages.set(target, state);
    if (target.opener) void target.opener().then((opener) => { state.opener = opener; }).catch(() => {});
    target.on('download', (download) => { void download.cancel().catch(() => {}); });
    target.on('dialog', (dialog) => { void dialog.dismiss().catch(() => {}); });
    target.on('framenavigated', (frame) => {
      if (frame !== target.mainFrame()) return;
      try {
        publicURL(target.url());
        if (state.promote) { activate(target); state.promote = false; }
        if (target === page) hub.invalidate();
        if (!pendingNavigation.has(target) &&
            (!state.documentURL || target.url().split('#')[0] === state.documentURL.split('#')[0])) {
          state.resolveCommit?.();
          state.resolveCommit = null;
        }
      } catch (error) {
        if (target.url() !== 'about:blank') recordFailure(error);
      }
      void refreshTitle(target);
      void refreshHistory(target);
    });
    target.on('domcontentloaded', () => {
      if (!pendingNavigation.has(target) &&
          (!state.documentURL || target.url().split('#')[0] === state.documentURL.split('#')[0])) {
        state.loading = false;
        state.resolveLoad?.();
        state.resolveCommit?.();
        state.resolveLoad = null;
        if (target === page) hub.invalidate();
      }
      void refreshTitle(target);
    });
    target.on('close', () => {
      state.resolveLoad?.();
      pages.delete(target);
      pendingNavigation.delete(target);
      if (target === page && !closed) {
        const parent = state.opener && pages.has(state.opener) && !state.opener.isClosed()
          ? state.opener
          : [...pages.keys()].filter((candidate) => !candidate.isClosed()).at(-1);
        if (parent) activate(parent);
        else hub.finish(new ServiceError(404, 'SESSION_NOT_FOUND', 'The browser page was closed.'));
      }
    });
    if (target.url() !== 'about:blank') {
      try { publicURL(target.url()); activate(target); state.promote = false; } catch (error) { recordFailure(error); }
    }
    return state;
  };
  const documentLoading = (target, url) => {
    const state = adopt(target);
    state.documentURL = url;
    if (!state.resolveCommit) state.committed = new Promise((resolve) => { state.resolveCommit = resolve; });
    if (!state.loading) {
      state.loading = true;
      state.loaded = new Promise((resolve) => { state.resolveLoad = resolve; });
    }
    return state;
  };
  try {
    await context.exposeBinding('__wikiBrowserTitle', ({ page: target, frame }, title) => {
      if (frame !== target.mainFrame() || typeof title !== 'string' || !pages.has(target)) return;
      pages.get(target).title = title.slice(0, 512);
      if (target === page) hub.metadataChanged();
    });
    await context.addInitScript(() => {
      for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection']) {
        Object.defineProperty(globalThis, name, { value: undefined, configurable: false, writable: false });
      }
      addEventListener('DOMContentLoaded', () => {
        let previous = null;
        const notify = () => {
          if (document.title === previous) return;
          previous = document.title;
          globalThis.__wikiBrowserTitle(previous).catch(() => {});
        };
        notify();
        if (document.head) new MutationObserver(notify).observe(document.head, { childList: true, characterData: true, subtree: true });
      });
    });
    await context.routeWebSocket('**/*', (socket) => socket.close());
    await context.route('**/*', async (route) => {
      const request = route.request();
      let frame = null;
      try { frame = request.frame(); } catch {}
      const documentRequest = request.isNavigationRequest() && (!frame || !frame.parentFrame());
      let release;
      try {
        publicURL(request.url());
        if (++requests > NETWORK_LIMITS.requests ||
            bytes >= NETWORK_LIMITS.totalBytes) {
          fail(413, 'resource_limit', 'The page exceeded its resource budget.');
        }
        let redirects = 0;
        for (let previous = request.redirectedFrom(); previous; previous = previous.redirectedFrom()) {
          if (++redirects > NETWORK_LIMITS.redirects) fail(400, 'redirect_limit', 'The page redirected too many times.');
        }
        release = await slots.acquire();
        let response;
        // The first popup request can precede creation of its Playwright Frame.
        const target = documentRequest && frame ? frame.page() : null;
        if (target) documentLoading(target, request.url());
        let effectiveURL = request.url();
        let method = request.method();
        let body = request.postDataBuffer();
        let headers = await request.allHeaders();
        let redirectCount = 0;
        const redirect = (target && pendingNavigation.get(target)) || (documentRequest && pendingPopupNavigation.get(effectiveURL));
        if (redirect?.url === effectiveURL) {
          ({ method, body, redirectCount } = redirect);
          if (redirect.contentType) headers['content-type'] = redirect.contentType;
          if (redirect.origin) headers.origin = redirect.origin;
          if (redirect.referer) headers.referer = redirect.referer;
        }
        if (target) pendingNavigation.delete(target);
        if (documentRequest) pendingPopupNavigation.delete(effectiveURL);
        for (;;) {
          response = await fetcher.fetch(effectiveURL, {
            method, headers, body, signal: controller.signal,
            maxBytes: Math.min(NETWORK_LIMITS.responseBytes, NETWORK_LIMITS.totalBytes - bytes),
          });
          bytes += response.body.length;
          if (bytes > NETWORK_LIMITS.totalBytes) fail(413, 'resource_limit', 'The page exceeded its resource budget.');
          await applyCookies(context, response.setCookies, effectiveURL);
          if (![301, 302, 303, 307, 308].includes(response.status) || !response.headers.location) break;
          if (++redirectCount > NETWORK_LIMITS.redirects) fail(400, 'redirect_limit', 'The page redirected too many times.');
          const next = publicURL(new URL(response.headers.location, effectiveURL).href).href;
          if (response.status === 303 && method !== 'HEAD' ||
              [301, 302].includes(response.status) && method === 'POST') {
            method = 'GET';
            body = null;
            delete headers['content-type'];
          }
          if (documentRequest) {
            // Chromium skips Playwright routing for HTTP redirect follow-ups. A new
            // document navigation keeps every hop intercepted and the address accurate.
            const redirect = {
              url: next, method, body, redirectCount, contentType: headers['content-type'],
              origin: headers.origin, referer: headers.referer,
            };
            if (target) pendingNavigation.set(target, redirect);
            else {
              if (pendingPopupNavigation.size >= 4) fail(429, 'BROWSER_CAPACITY', 'The browser tab limit has been reached.');
              pendingPopupNavigation.set(next, redirect);
            }
            const destination = JSON.stringify(next).replace(/</g, '\\u003c');
            await route.fulfill({
              status: 200, contentType: 'text/html',
              body: `<!doctype html><script>location.replace(${destination})</script>`,
            });
            return;
          }
          if (++requests > NETWORK_LIMITS.requests) fail(413, 'resource_limit', 'The page exceeded its resource budget.');
          // Subresource redirects are fetched entirely in Node, never in Chromium.
          if (new URL(next).origin !== new URL(effectiveURL).origin) {
            delete headers.cookie;
            delete headers.authorization;
          }
          else {
            headers.cookie = (await context.cookies(next)).map((cookie) => `${cookie.name}=${cookie.value}`).join('; ');
          }
          effectiveURL = next;
        }
        const responseHeaders = {};
        for (const [name, value] of Object.entries(response.headers)) {
          if (!['set-cookie', 'content-encoding', 'content-length', 'transfer-encoding',
            'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
            'upgrade', 'alt-svc', 'refresh'].includes(name.toLowerCase()) && value !== undefined) {
            responseHeaders[name] = Array.isArray(value) ? value.join(', ') : String(value);
          }
        }
        await route.fulfill({ status: response.status, headers: responseHeaders, body: response.body });
      } catch (error) {
        if (!closed && (!frame || !frame.page().isClosed())) {
          const safe = safeError(error);
          if (documentRequest) recordFailure(safe);
          else {
            resourceErrors = Math.min(NETWORK_LIMITS.requests, resourceErrors + 1);
            const kind = request.resourceType();
            const key = `${kind}:${safe.code}`;
            if (!reportedResources.has(key)) {
              reportedResources.add(key);
              console.warn(JSON.stringify({
                event: 'browser.resource_failed', resource_type: kind, code: safe.code,
              }));
            }
            hub.metadataChanged();
          }
        }
        await route.abort('blockedbyclient').catch(() => {});
      } finally {
        release?.();
      }
    });
    page = await context.newPage();
    adopt(page);
    await refreshTitle(page);
    context.on('page', (other) => {
      try { adopt(other); } catch (error) { recordFailure(error); }
    });
  } catch (error) {
    controller.abort();
    await context.close();
    throw error;
  }
  const check = () => {
    if (networkError) throw networkError;
    publicURL(page.url());
  };
  const loaded = async () => {
    if (networkError) throw networkError;
    for (let i = 0; i < 12; i++) {
      const target = page;
      const state = pages.get(target);
      if (state?.command) await state.command;
      if (state?.loading) await state.loaded;
      if (target === page && !pages.get(page)?.loading) break;
    }
    await page.waitForLoadState('domcontentloaded');
    check();
  };
  const settle = async () => {
    await loaded();
    await page.waitForLoadState('networkidle', { timeout: 1500 }).catch(() => {});
    await page.waitForTimeout(300);
    check();
  };
  const backgroundNavigation = (operation, target = page) => {
    const state = pages.get(target);
    const command = operation.catch((error) => {
      if (!closed && !/ERR_ABORTED|interrupted by another navigation|Target.*closed/.test(error.message || '')) {
        recordFailure(safeError(error));
      }
    }).finally(() => {
      if (state.command === command) {
        state.command = null;
        if (target === page) hub.invalidate();
      }
    });
    state.command = command;
  };
  return {
    async navigate(url) {
      try {
        await page.goto(publicURL(url).href, { waitUntil: 'commit' });
        const state = pages.get(page);
        if (state?.resolveCommit) await state.committed;
      }
      catch (error) { if (networkError) throw networkError; throw error; }
      check();
    },
    async metadata() {
      check();
      return cached();
    },
    async screenshot() {
      check();
      const result = await page.screenshot({ type: 'png', fullPage: false, timeout: 10000 });
      if (result.length > 4 * 1024 * 1024) fail(413, 'resource_limit', 'The screenshot exceeded its size limit.');
      return result;
    },
    async capture({ settle: wait = false } = {}) {
      if (wait) await settle();
      else {
        // Explicit confirmation snapshots can wait one paint; live input never does.
        await page.waitForTimeout(32);
        await loaded();
      }
      check();
      let html, target;
      for (let attempt = 0; attempt < 4; attempt++) {
        target = page;
        try {
          html = await target.evaluate(() => {
        const root = document.documentElement.cloneNode(true);
        const sanitize = (node) => {
          node.querySelectorAll('form').forEach((form) => {
            const content = form.ownerDocument.createDocumentFragment();
            while (form.firstChild) content.appendChild(form.firstChild);
            form.replaceWith(content);
          });
          node.querySelectorAll('script,input,textarea,select,button,iframe,object,embed,noscript').forEach((element) => element.remove());
          const elements = [...node.querySelectorAll('*')];
          if (node.nodeType === 1) elements.unshift(node);
          elements.forEach((element) => {
            for (const attribute of [...element.attributes]) {
              if (/^on/i.test(attribute.name) || ['srcdoc', 'value', 'nonce', 'integrity'].includes(attribute.name)) {
                element.removeAttribute(attribute.name);
              }
            }
            if (element.tagName === 'TEMPLATE') sanitize(element.content);
          });
        };
        sanitize(root);
        return '<!DOCTYPE html>\n' + root.outerHTML;
          });
          if (target === page) break;
        } catch (error) {
          if (!/Execution context was destroyed|Cannot find context/.test(error.message || '') || attempt === 3) throw error;
        }
        await page.waitForTimeout(32);
        await loaded();
      }
      if (Buffer.byteLength(html) > 4 * 1024 * 1024) fail(413, 'resource_limit', 'The captured page exceeded its size limit.');
      return { url: target.url(), title: (await target.title()).slice(0, 512), html };
    },
    async action(action, expectedGeneration = inputGeneration) {
      const currentGeneration = expectedGeneration ?? inputGeneration;
      const current = () => {
        if (closed || currentGeneration !== inputGeneration) {
          fail(502, 'BROWSER_RENDER_FAILED', 'The browser action was cancelled.');
        }
      };
      try {
        await releasing;
        current();
        check();
        const target = page;
        const state = pages.get(target);
        switch (action.action) {
        case 'click':
          state.pointerDown = true;
          await target.mouse.click(action.x, action.y);
          state.pointerDown = false;
          break;
        case 'pointer': {
          await target.mouse.move(action.x, action.y);
          current();
          if (action.phase === 'down') {
            state.pointerDown = true;
            await target.mouse.down();
          }
          else if (action.phase === 'up') {
            try { await target.mouse.up(); } finally { state.pointerDown = false; }
          }
          break;
        }
        case 'drag': {
          await target.mouse.move(action.x, action.y);
          current();
          state.pointerDown = true;
          await target.mouse.down();
          try {
            const distance = Math.hypot(action.to_x - action.x, action.to_y - action.y);
            const steps = Math.min(32, Math.max(1, Math.ceil(distance / 20)));
            current();
            await target.mouse.move(action.to_x, action.to_y, { steps });
          } finally {
            try { await target.mouse.up(); } finally { state.pointerDown = false; }
          }
          break;
        }
        case 'type': await target.keyboard.insertText(action.text); break;
        case 'key': await target.keyboard.press(action.key); break;
        case 'scroll': await target.mouse.wheel(0, action.delta); break;
        case 'reload': backgroundNavigation(target.reload({ waitUntil: 'domcontentloaded' }), target); break;
        case 'back': {
          const cdp = await context.newCDPSession(target);
          let history;
          try { history = await cdp.send('Page.getNavigationHistory'); }
          finally { await cdp.detach().catch(() => {}); }
          if (history.currentIndex <= 0 || !history.entries[history.currentIndex - 1] ||
              !/^https?:\/\//i.test(history.entries[history.currentIndex - 1].url)) {
            fail(400, 'NO_HISTORY', 'There is no previous browser page.');
          }
          publicURL(history.entries[history.currentIndex - 1].url);
          current();
          backgroundNavigation(target.goBack({ waitUntil: 'domcontentloaded' }), target);
          break;
        }
      }
        current();
      } catch (error) {
        if (currentGeneration === inputGeneration) await releasePointer();
        else await releasing;
        throw error;
      }
    },
    inputGeneration: () => inputGeneration,
    resetInput: (expectedGeneration) => expectedGeneration === undefined || expectedGeneration === inputGeneration
      ? releasePointer() : releasing,
    frames: (frame, end) => hub.subscribe(frame, end),
    async close(reason) {
      closed = true;
      const released = releasePointer();
      pendingNavigation.clear();
      pendingPopupNavigation.clear();
      controller.abort();
      await Promise.all([released, hub.close(reason), context.close()]);
    },
  };
}
