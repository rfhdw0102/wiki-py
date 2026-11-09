import dns from 'node:dns/promises';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { createBrotliDecompress, createGunzip, createInflate } from 'node:zlib';
import ipaddr from 'ipaddr.js';
import { fail, ServiceError } from './errors.js';

export const NETWORK_LIMITS = Object.freeze({
  timeout: 10000, responseBytes: 4 * 1024 * 1024, requestBytes: 1024 * 1024,
  redirects: 10, requests: 256, totalBytes: 64 * 1024 * 1024, concurrent: 8,
});

const excluded = [
  '192.0.0.0/24', '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24',
  '168.63.129.16/32',
  '2001::/23', '2002::/16', '3fff::/20',
].map((cidr) => ipaddr.parseCIDR(cidr));

export function publicAddress(address) {
  try {
    const parsed = ipaddr.parse(address);
    if (parsed.kind() === 'ipv6' && parsed.isIPv4MappedAddress()) return false;
    if (parsed.range() !== 'unicast') return false;
    if (parsed.kind() === 'ipv6' && !parsed.match(ipaddr.parseCIDR('2000::/3'))) return false;
    return !excluded.some(([network, prefix]) =>
      network.kind() === parsed.kind() && parsed.match(network, prefix));
  } catch {
    return false;
  }
}

export function publicURL(value) {
  if (typeof value !== 'string' || value.length > 8192) fail(400, 'invalid_url', 'A public HTTP(S) URL is required.');
  let url;
  try { url = new URL(value); } catch { fail(400, 'invalid_url', 'A public HTTP(S) URL is required.'); }
  if (url.href.length > 8192) fail(400, 'invalid_url', 'A public HTTP(S) URL no longer than 8192 characters is required.');
  const hostname = url.hostname.replace(/^\[|\]$/g, '');
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      !hostname || hostname.includes('%') || hostname.toLowerCase() === 'localhost' ||
      hostname.toLowerCase().endsWith('.localhost') ||
      (net.isIP(hostname) && !publicAddress(hostname))) {
    fail(400, 'blocked_url', 'Only public HTTP(S) destinations without credentials are allowed.');
  }
  return url;
}

export async function resolvePublic(url, lookup = dns.lookup) {
  const host = url.hostname.replace(/^\[|\]$/g, '');
  let addresses;
  let timer;
  try {
    addresses = net.isIP(host)
      ? [{ address: host, family: net.isIP(host) }]
      : await Promise.race([
        lookup(host, { all: true, verbatim: true }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new ServiceError(504, 'network_timeout', 'The destination timed out.')), NETWORK_LIMITS.timeout);
          timer.unref();
        }),
      ]);
  } catch (error) {
    if (error instanceof ServiceError) throw error;
    fail(502, 'dns_failure', 'The destination could not be resolved.');
  } finally { clearTimeout(timer); }
  if (!addresses.length || addresses.some(({ address, family }) => !publicAddress(address) || net.isIP(address) !== family)) {
    fail(400, 'blocked_url', 'The destination is not on the public network.');
  }
  return addresses[0];
}

const requestHeaders = new Set([
  'accept', 'accept-language', 'authorization', 'content-type', 'cookie', 'origin', 'referer',
  'user-agent', 'range', 'if-none-match', 'if-modified-since',
  'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site', 'sec-fetch-user',
  'sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform',
  'x-csrftoken', 'x-csrf-token', 'x-xsrf-token', 'x-requested-with',
  'access-control-request-method', 'access-control-request-headers',
]);

export function webpageHeaders(headers = {}) {
  const result = { 'accept-encoding': 'identity' };
  for (const [key, value] of Object.entries(headers)) {
    const lower = key.toLowerCase();
    if (lower === 'authorization' && serviceAuthorization(value)) continue;
    if (requestHeaders.has(lower)) result[lower] = value;
  }

  function serviceAuthorization(value) {
    if (typeof value !== 'string') return true;
    const match = /^Bearer\s+(\S+)$/i.exec(value);
    if (!match) return false;
    try {
      const claims = JSON.parse(Buffer.from(match[1].split('.')[1] || '', 'base64url'));
      return claims?.iss === 'wiki-agent' || claims?.aud === 'wiki-browser-import' ||
        Array.isArray(claims?.aud) && claims.aud.includes('wiki-browser-import');
    } catch {
      return false;
    }
  }
  return result;
}

export function pinnedTransport(url, options, clients = { http, https }) {
  return new Promise((resolve, reject) => {
    const client = url.protocol === 'https:' ? clients.https : clients.http;
    // Keep the URL hostname for Host and TLS verification; only the socket address is pinned.
    const request = client.request(url, {
      method: options.method, headers: options.headers, agent: false,
      lookup: (_hostname, lookupOptions, callback) => {
        const pinned = options.address;
        if (lookupOptions.all) callback(null, [pinned]);
        else callback(null, pinned.address, pinned.family);
      },
      maxHeaderSize: 32768,
      signal: options.signal,
    }, (response) => {
      const encoding = String(response.headers['content-encoding'] || '').toLowerCase();
      let stream = response;
      if (encoding === 'gzip') stream = response.pipe(createGunzip());
      else if (encoding === 'br') stream = response.pipe(createBrotliDecompress());
      else if (encoding === 'deflate') stream = response.pipe(createInflate());
      else if (encoding && encoding !== 'identity') {
        request.destroy(new ServiceError(502, 'unsupported_encoding', 'The destination returned an unsupported response.'));
        return;
      }
      let wireBytes = 0, bodyBytes = 0;
      const chunks = [];
      response.on('data', (chunk) => {
        wireBytes += chunk.length;
        if (wireBytes > options.maxBytes) request.destroy(new ServiceError(413, 'resource_limit', 'The page exceeded its resource budget.'));
      });
      stream.on('data', (chunk) => {
        bodyBytes += chunk.length;
        if (bodyBytes > options.maxBytes) {
          stream.destroy();
          request.destroy(new ServiceError(413, 'resource_limit', 'The page exceeded its resource budget.'));
        } else chunks.push(chunk);
      });
      response.on('error', reject);
      stream.on('error', reject);
      stream.on('end', () => resolve({
        status: response.statusCode, headers: response.headers,
        setCookies: response.headers['set-cookie'] || [],
        body: Buffer.concat(chunks),
      }));
    });
    request.on('error', reject);
    request.end(options.body);
  });
}

export class PublicFetcher {
  constructor({ lookup = dns.lookup, transport = pinnedTransport } = {}) {
    this.lookup = lookup;
    this.transport = transport;
  }

  async fetch(value, { method = 'GET', headers = {}, body, signal, maxBytes = NETWORK_LIMITS.responseBytes } = {}) {
    const url = publicURL(value);
    if (body && body.length > NETWORK_LIMITS.requestBytes) fail(413, 'resource_limit', 'The page exceeded its resource budget.');
    const address = await resolvePublic(url, this.lookup);
    const deadline = AbortSignal.any([AbortSignal.timeout(NETWORK_LIMITS.timeout), ...(signal ? [signal] : [])]);
    let response;
    try {
      response = await this.transport(url, {
        address, method, headers: webpageHeaders(headers), body, signal: deadline, maxBytes,
      });
    } catch (error) {
      if (error instanceof ServiceError) throw error;
      fail(deadline.aborted ? 504 : 502, deadline.aborted ? 'network_timeout' : 'network_failure', 'The destination could not be loaded.');
    }
    if (response.body.length > maxBytes) fail(413, 'resource_limit', 'The page exceeded its resource budget.');
    if (response.status >= 300 && response.status < 400 && response.headers.location) {
      // Validate and resolve *before* exposing Location. The following browser request is pinned again.
      const next = publicURL(new URL(response.headers.location, url).href);
      await resolvePublic(next, this.lookup);
    }
    return response;
  }
}
