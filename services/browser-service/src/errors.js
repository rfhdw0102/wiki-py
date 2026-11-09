export const SAFE_FALLBACK_CODE = 'BROWSER_RENDER_FAILED';
export const SAFE_FALLBACK_MESSAGE = 'The browser operation could not be completed.';

const aliases = Object.freeze({
  invalid_url: 'PUBLIC_URL_REQUIRED',
  blocked_url: 'UNSAFE_URL',
  session_limit: 'BROWSER_CAPACITY',
  action_limit: 'BROWSER_CAPACITY',
  resource_limit: 'BROWSER_CAPACITY',
  browser_failure: SAFE_FALLBACK_CODE,
  unavailable: SAFE_FALLBACK_CODE,
  operation_timeout: SAFE_FALLBACK_CODE,
  network_timeout: SAFE_FALLBACK_CODE,
  network_failure: SAFE_FALLBACK_CODE,
  dns_failure: SAFE_FALLBACK_CODE,
  unsupported_encoding: SAFE_FALLBACK_CODE,
});

export class ServiceError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = aliases[code] || code.toUpperCase();
  }
}

export function fail(status, code, message) {
  throw new ServiceError(status, code, message);
}

export function safeError(error) {
  return error instanceof ServiceError
    ? error
    : new ServiceError(502, SAFE_FALLBACK_CODE, SAFE_FALLBACK_MESSAGE);
}
