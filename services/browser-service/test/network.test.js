import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { gzipSync } from 'node:zlib';
import { publicAddress, publicURL, resolvePublic, PublicFetcher, pinnedTransport, webpageHeaders } from '../src/network.js';

const publicDNS = async () => [{ address: '93.184.216.34', family: 4 }];
const response = (headers = {}, body = 'hello', status = 200) => ({
  status, headers, setCookies: [], body: Buffer.from(body),
});

test('public network URL and address validation rejects special-purpose ranges and URL tricks', () => {
  for (const address of [
    '127.0.0.1', '10.1.1.1', '172.16.0.1', '192.168.0.1', '169.254.169.254',
    '0.0.0.0', '100.100.100.200', '168.63.129.16', '198.18.0.1', '192.0.2.1', '198.51.100.1',
    '203.0.113.1', '224.0.0.1', '240.0.0.1', '255.255.255.255', '::1',
    '::', 'fe80::1', 'fc00::1', 'ff02::1', '::ffff:8.8.8.8', '2001:db8::1',
    '2002:7f00:1::', '64:ff9b::7f00:1', '3fff::1',
  ]) assert.equal(publicAddress(address), false, address);
  for (const address of ['8.8.8.8', '93.184.216.34', '2606:4700:4700::1111']) assert.equal(publicAddress(address), true);
  for (const url of [
    'file:///etc/passwd', 'ftp://example.com', 'data:text/html,hello', 'http://localhost',
    'http://x.localhost', 'http://user:pass@example.com', 'http://2130706433',
    'http://0x7f000001', 'http://0177.0.0.1', 'http://[::ffff:127.0.0.1]', 'http://[::1]',
  ]) assert.throws(() => publicURL(url), { status: 400 });
  assert.equal(publicURL('https://example.com/article').hostname, 'example.com');
});

test('all DNS answers must be public and correct address families', async () => {
  await assert.rejects(resolvePublic(new URL('https://example.com'), async () => [
    { address: '8.8.8.8', family: 4 }, { address: '10.0.0.1', family: 4 },
  ]), { code: 'UNSAFE_URL' });
  await assert.rejects(resolvePublic(new URL('https://example.com'), async () => [
    { address: '8.8.8.8', family: 6 },
  ]), { code: 'UNSAFE_URL' });
  await assert.rejects(resolvePublic(new URL('https://example.com'), async () => []), { code: 'UNSAFE_URL' });
  await assert.rejects(resolvePublic(new URL('https://example.com'), async () => { throw new Error('secret'); }), { code: 'BROWSER_RENDER_FAILED' });
});

test('public fetch pins resolved address and strips authorization and owner headers', async () => {
  let calls = 0;
  const fetcher = new PublicFetcher({
    lookup: publicDNS,
    transport: async (url, options) => {
      calls++;
      assert.equal(url.hostname, 'example.com');
      assert.deepEqual(options.address, { address: '93.184.216.34', family: 4 });
      assert.equal(options.headers.authorization, undefined);
      assert.equal(options.headers['x-owner-id'], undefined);
      assert.equal(options.headers.host, undefined);
      assert.equal(options.headers.cookie, 'session=synthetic');
      return response();
    },
  });
  await fetcher.fetch('https://example.com', { headers: {
    Authorization: `Bearer e30.${Buffer.from(JSON.stringify({ iss: 'wiki-agent', aud: 'wiki-browser-import' })).toString('base64url')}.synthetic`,
    'X-Owner-ID': 'user', host: 'internal', cookie: 'session=synthetic',
  } });
  assert.equal(calls, 1);
  assert.equal(webpageHeaders({ authorization: 'Bearer website-issued-opaque-token' }).authorization, 'Bearer website-issued-opaque-token');
  assert.equal(webpageHeaders({ authorization: 'Basic synthetic-webpage-login' }).authorization, 'Basic synthetic-webpage-login');
  assert.equal(webpageHeaders({ 'X-CSRFToken': 'synthetic-csrf' })['x-csrftoken'], 'synthetic-csrf');
});

test('redirects to private URLs or private DNS are rejected before Location reaches Chromium', async () => {
  for (const location of ['http://127.0.0.1/private', 'http://metadata.example/private', 'file:///etc/passwd', 'https://u:p@example.com']) {
    let calls = 0;
    const fetcher = new PublicFetcher({
      lookup: async (host) => host === 'metadata.example' ? [{ address: '169.254.169.254', family: 4 }] : publicDNS(),
      transport: async () => { calls++; return response({ location }, '', 302); },
    });
    await assert.rejects(fetcher.fetch('https://example.com'), { status: 400 });
    assert.equal(calls, 1);
  }
});

test('DNS rebinding between validated redirect and follow-up is rejected; public redirects are resolved individually', async () => {
  let count = 0, connections = 0;
  const fetcher = new PublicFetcher({
    lookup: async () => ++count <= 2 ? publicDNS() : [{ address: '127.0.0.1', family: 4 }],
    transport: async () => { connections++; return response({ location: '/next' }, '', 302); },
  });
  await fetcher.fetch('https://example.com/start');
  await assert.rejects(fetcher.fetch('https://example.com/next'), { code: 'UNSAFE_URL' });
  assert.equal(connections, 1);
});

function clientFixture(body, headers = {}) {
  let options, destination;
  const client = {
    request(url, opts, callback) {
      destination = url;
      options = opts;
      const request = new EventEmitter();
      request.destroy = (error) => { if (error) request.emit('error', error); };
      request.end = () => {
        const stream = Readable.from([body]);
        stream.headers = headers;
        stream.statusCode = 200;
        callback(stream);
      };
      return request;
    },
  };
  return { clients: { http: client, https: client }, get options() { return options; }, get destination() { return destination; } };
}

test('real Node transport socket lookup is pinned, while Host/TLS URL stays original', async () => {
  const fixture = clientFixture(Buffer.from('hello'), { 'set-cookie': ['a=1', 'b=2'] });
  const options = { address: { address: '8.8.8.8', family: 4 }, maxBytes: 1024, method: 'GET', headers: {} };
  const result = await pinnedTransport(new URL('https://example.com'), options, fixture.clients);
  assert.equal(fixture.destination.hostname, 'example.com');
  assert.equal(fixture.options.agent, false);
  fixture.options.lookup('example.com', {}, (_error, address, family) => {
    assert.equal(address, '8.8.8.8');
    assert.equal(family, 4);
  });
  fixture.options.lookup('example.com', { all: true }, (_error, addresses) => assert.deepEqual(addresses, [options.address]));
  assert.deepEqual(result.setCookies, ['a=1', 'b=2']);
});

test('transport decoding enforces compressed and decoded response limits', async () => {
  const normal = clientFixture(gzipSync('hello'), { 'content-encoding': 'gzip' });
  const options = { address: { address: '8.8.8.8', family: 4 }, maxBytes: 1024, method: 'GET', headers: {} };
  assert.equal((await pinnedTransport(new URL('https://example.com'), options, normal.clients)).body.toString(), 'hello');
  const bomb = clientFixture(gzipSync('a'.repeat(10000)), { 'content-encoding': 'gzip' });
  await assert.rejects(pinnedTransport(new URL('https://example.com'), options, bomb.clients), { code: 'BROWSER_CAPACITY' });
  const fetcher = new PublicFetcher({ lookup: publicDNS, transport: async () => response({}, 'oversized') });
  await assert.rejects(fetcher.fetch('https://example.com', { maxBytes: 1 }), { code: 'BROWSER_CAPACITY' });
});

test('URL input and canonical browser URL are bounded to 8192 characters', () => {
  assert.throws(() => publicURL(undefined), { code: 'PUBLIC_URL_REQUIRED' });
  assert.throws(() => publicURL('https://example.com/' + 'a'.repeat(8192)), { code: 'PUBLIC_URL_REQUIRED' });
  assert.throws(() => publicURL('https://example.com/' + '中'.repeat(3000)), { code: 'PUBLIC_URL_REQUIRED' });
  assert.throws(() => publicURL('file:///private'), { code: 'UNSAFE_URL' });
});
