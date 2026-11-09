import { configuration } from './auth.js';
import { launchBrowser } from './browser.js';
import { PublicFetcher } from './network.js';
import { SessionManager } from './sessions.js';
import { createServer } from './server.js';

async function main() {
  const { secret } = configuration();
  const browser = await launchBrowser(new PublicFetcher());
  const manager = new SessionManager(browser);
  const server = createServer({ manager, secret });
  const sweep = setInterval(() => { void manager.sweep(); }, 5000);
  sweep.unref();
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(8080, '0.0.0.0', resolve);
    });
  } catch (error) {
    clearInterval(sweep);
    await browser.close();
    throw error;
  }
  console.info('Browser import service listening on port 8080.');
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    stopping = true;
    clearInterval(sweep);
    const forced = setTimeout(() => process.exit(1), 10000);
    forced.unref();
    server.close();
    server.closeIdleConnections();
    await manager.shutdown();
    await browser.close();
    server.closeAllConnections();
    clearTimeout(forced);
  };
  process.on('SIGTERM', () => { void stop(); });
  process.on('SIGINT', () => { void stop(); });
}

main().catch(() => {
  console.error('Browser import service failed to start. Check configuration and browser availability.');
  process.exit(1);
});
