// Map transport/HTTP failures to a normalized ApiError with an actionable,
// secret-free message.

import { ApiError, type ApiErrorKind } from '../models/http';

export function statusToKind(status: number): ApiErrorKind {
  if (status === 401) {
    return 'auth';
  }
  if (status === 403) {
    return 'forbidden';
  }
  if (status === 404) {
    return 'not-found';
  }
  if (status === 429) {
    return 'rate-limited';
  }
  if (status >= 500) {
    return 'server';
  }
  if (status >= 400) {
    return 'bad-response';
  }
  return 'unknown';
}

const MESSAGES: Record<ApiErrorKind, string> = {
  network: 'Cannot reach the master. Check the URL, network, and that the backend is running.',
  auth: 'Authentication required or expired. Run "Distributed Compute: Configure Authentication".',
  forbidden: 'You are not authorized to perform this action on the connected master.',
  'not-found': 'The requested resource was not found on the master.',
  'rate-limited': 'The master is rate-limiting requests. Try again shortly.',
  server: 'The master reported a server error. Check backend logs.',
  'bad-response': 'The master returned an unexpected response.',
  unknown: 'An unexpected error occurred.',
};

/**
 * Statuses whose server message describes the user's own request, (403, 502)
 * a worker the master could not reach or may not contact, or (503, 507) a
 * storage problem with its fix; the gateway's messages are curated and
 * secret-free.
 */
const USER_FACING = new Set([400, 403, 409, 413, 422, 502, 503, 507]);

export function httpStatusToError(status: number, requestId?: string, detail?: string): ApiError {
  const kind = statusToKind(status);
  const message = detail && USER_FACING.has(status) ? detail : MESSAGES[kind];
  return new ApiError(message, kind, status, requestId);
}

/** The gateway's error message from a response body ({"error": "..."}), if any. */
export function errorDetail(body: string): string | undefined {
  try {
    const parsed = JSON.parse(body) as { error?: unknown; detail?: unknown };
    const value = typeof parsed.error === 'string' ? parsed.error : parsed.detail;
    return typeof value === 'string' && value.trim() !== '' ? value.trim().slice(0, 500) : undefined;
  } catch {
    return undefined;
  }
}

/** Normalize any thrown value into an ApiError (used around fetch). */
export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) {
    return err;
  }
  const name = (err as { name?: string } | undefined)?.name;
  if (name === 'AbortError' || name === 'TimeoutError') {
    return new ApiError('Request timed out or was cancelled.', 'network');
  }
  return new ApiError(MESSAGES.network, 'network');
}
