import { performance } from 'node:perf_hooks';
import { launchBrowser } from '../src/browser.js';
import { PublicFetcher } from '../src/network.js';
import { SessionManager } from '../src/sessions.js';

const fetcher = new PublicFetcher({
  lookup: async () => [{ address: '93.184.216.34', family: 4 }],
  transport: async () => ({
    status: 200, headers: { 'content-type': 'text/html' }, setCookies: [],
    body: Buffer.from(`<title>Controlled latency fixture</title>
      <input style="position:fixed;left:20px;top:20px;width:300px;height:40px">
      <article style="margin-top:100px;height:3000px">Controlled content</article>`),
  }),
});
const percentile = (values, percentile) =>
  [...values].sort((a, b) => a - b)[Math.ceil(values.length * percentile) - 1];
const browser = await launchBrowser(fetcher);
const manager = new SessionManager(browser);
const owner = { user: 'benchmark-user', wiki: 'benchmark-wiki' };
try {
  const start = performance.now();
  const session = await manager.create(owner, 'https://example.com/controlled');
  const openMs = performance.now() - start;
  const actions = [], screenshots = [], combined = [];
  const samples = [
    { action: 'click', x: 50, y: 40 }, { action: 'type', text: 'synthetic' },
    { action: 'key', key: 'Backspace' }, { action: 'scroll', delta: 10 },
  ];
  for (let i = 0; i < 24; i++) {
    const actionStart = performance.now();
    await manager.run(session.id, owner, (handle) => handle.action(samples[i % samples.length]));
    actions.push(performance.now() - actionStart);
    const shotStart = performance.now();
    await manager.run(session.id, owner, (handle) => handle.screenshot());
    screenshots.push(performance.now() - shotStart);
    combined.push(performance.now() - actionStart);
  }
  console.log(JSON.stringify({
    label: process.env.BENCH_LABEL || 'current', samples: actions.length,
    openMs: Math.round(openMs),
    actionP50Ms: Math.round(percentile(actions, 0.5)),
    actionP95Ms: Math.round(percentile(actions, 0.95)),
    screenshotP95Ms: Math.round(percentile(screenshots, 0.95)),
    actionAndScreenshotP95Ms: Math.round(percentile(combined, 0.95)),
  }));
} finally { await manager.shutdown(); await browser.close(); }
