import test from 'node:test';
import assert from 'node:assert/strict';
import { launchBrowser } from '../src/browser.js';
import { PublicFetcher } from '../src/network.js';
import { SessionManager } from '../src/sessions.js';

test('Chromium document 307/308 preserves POST, public subresource chains work and redirect loops stop', async (t) => {
  const seen = [];
  const fetcher = new PublicFetcher({
    lookup: async () => [{ address: '93.184.216.34', family: 4 }],
    transport: async (url, options) => {
      seen.push({ url: url.href, method: options.method, body: options.body?.toString() });
      const response = (body, status = 200, headers = {}) => ({
        status, body: Buffer.from(body), headers: { 'content-type': 'text/html', ...headers }, setCookies: [],
      });
      if (url.pathname === '/form') return response(`<title>Form</title>
        <form action="/post" method="post"><input name="value" value="synthetic"></form>
        <script>document.querySelector('input').focus()</script>`);
      if (url.pathname === '/post') return response('', 307, { location: '/post-again' });
      if (url.pathname === '/post-again') return response('', 308, { location: 'https://second.example/dir/final' });
      if (url.pathname === '/dir/final') return response(`<title>Final</title><article>finished</article>
        <link rel="stylesheet" href="./assets/site.css">
        <a href="./next" style="position:absolute;left:10px;top:50px;width:100px;height:30px">Next</a>`);
      if (url.pathname === '/dir/assets/site.css') return response('', 302, { location: './redirected.css' });
      if (url.pathname === '/dir/assets/redirected.css') return response('article{color:red}', 200, { 'content-type': 'text/css' });
      if (url.pathname === '/dir/next') return response('<title>Relative next</title><article>next article</article>');
      if (url.pathname.startsWith('/loop/')) return response('', 302, { location: `/loop/${Number(url.pathname.split('/').at(-1)) + 1}` });
      return response('', 404);
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
  t.after(async () => { await manager.shutdown(); await browser.close(); });
  const owner = { user: 'synthetic-user', wiki: 'synthetic-wiki' };
  const session = await manager.create(owner, 'https://example.com/form');
  await manager.run(session.id, owner, async (handle) => {
    await handle.action({ action: 'key', key: 'Enter' });
    const capture = await handle.capture();
    assert.equal(capture.url, 'https://second.example/dir/final');
    assert.equal(capture.title, 'Final');
    await handle.action({ action: 'click', x: 30, y: 60 });
    const next = await handle.capture();
    assert.equal(next.url, 'https://second.example/dir/next');
    assert.equal(next.title, 'Relative next');
  });
  for (const path of ['/post', '/post-again', '/dir/final']) {
    const call = seen.find((item) => new URL(item.url).pathname === path);
    assert.equal(call.method, 'POST');
    assert.equal(call.body, 'value=synthetic');
  }
  assert.ok(seen.some((item) => item.url === 'https://second.example/dir/assets/site.css'));
  assert.ok(seen.some((item) => item.url === 'https://second.example/dir/assets/redirected.css'));
  await manager.remove(session.id, owner);
  await assert.rejects(manager.create(owner, 'https://example.com/loop/0'), { code: 'REDIRECT_LIMIT' });
  assert.equal(manager.sessions.size, 0);
});
