import { createHmac, timingSafeEqual } from 'node:crypto';
import { fail } from './errors.js';

export const DEVELOPMENT_SECRET = 'development-only-secret-key-change-me';

export function configuration(env = process.env) {
  const secret = env.WIKI_AGENT_SECRET_KEY || DEVELOPMENT_SECRET;
  const development = (env.WIKI_AGENT_ENV || 'development') === 'development' &&
    (env.NODE_ENV || 'development') === 'development';
  if (!development && secret === DEVELOPMENT_SECRET) {
    throw new Error('A non-default WIKI_AGENT_SECRET_KEY is required outside development.');
  }
  return { secret };
}

export function authenticate(header, secret, now = Date.now()) {
  const invalid = () => fail(401, 'unauthorized', 'A valid browser import token is required.');
  if (typeof header !== 'string' || header.length > 4096 || !header.startsWith('Bearer ')) invalid();
  const token = header.slice(7);
  const parts = token.split('.');
  if (parts.length !== 3 || parts.some((part) => !/^[A-Za-z0-9_-]+$/.test(part))) invalid();
  const expected = createHmac('sha256', secret).update(`${parts[0]}.${parts[1]}`).digest();
  const signature = Buffer.from(parts[2], 'base64url');
  if (signature.length !== expected.length || !timingSafeEqual(signature, expected)) invalid();
  let headerData, claims;
  try {
    headerData = JSON.parse(Buffer.from(parts[0], 'base64url'));
    claims = JSON.parse(Buffer.from(parts[1], 'base64url'));
  } catch {
    invalid();
  }
  const seconds = now / 1000;
  const frames = claims?.scope === 'browser-frames';
  const maximumLifetime = frames ? 900 : 300;
  if (!headerData || headerData.alg !== 'HS256' || headerData.crit !== undefined ||
      !claims || claims.aud !== 'wiki-browser-import' || claims.iss !== 'wiki-agent' ||
      !Number.isInteger(claims.exp) || claims.exp <= seconds || claims.exp > seconds + maximumLifetime ||
      (claims.nbf !== undefined && (!Number.isInteger(claims.nbf) || claims.nbf > seconds)) ||
      (claims.iat !== undefined && (!Number.isInteger(claims.iat) || claims.iat > seconds ||
        claims.exp - claims.iat > maximumLifetime)) ||
      (claims.scope !== undefined && !frames) ||
      (frames && (!Number.isInteger(claims.iat) ||
        typeof claims.session_id !== 'string' || !/^[A-Za-z0-9_-]{32}$/.test(claims.session_id))) ||
      !validID(claims.sub) || !validID(claims.wiki_id)) invalid();
  return {
    user: claims.sub, wiki: claims.wiki_id,
    ...(frames ? { streamSessionId: claims.session_id } : {}),
  };
}

function validID(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}
