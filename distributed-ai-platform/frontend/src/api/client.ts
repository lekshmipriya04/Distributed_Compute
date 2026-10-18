// ---------------------------------------------------------------------------
// Base API client.
//
// All requests go through the Vite dev-server proxy ('/api' -> gateway at
// localhost:8000, see vite.config.ts) so we always call relative paths here.
//
// Sign-in: the backend validates pre-issued JWTs (generate_token.py, same
// JWT_SECRET as config/.env); there is no /login endpoint. The account menu
// stores the token here. localStorage is per origin, so each host:port of the
// dashboard keeps its own user. Without a token, development backends treat
// requests as the local "dev-user"; production backends answer 401, which
// opens the sign-in dialog (AUTH_REQUIRED_EVENT).
// ---------------------------------------------------------------------------

const TOKEN_KEY = 'platform_jwt_token';

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage unavailable */
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

/** Fired on every 401 so the account menu can ask the user to sign in. */
export const AUTH_REQUIRED_EVENT = 'dc:auth-required';

export interface TokenInfo {
  sub: string;
  /** Expiry, ms since epoch; undefined when the token has none. */
  expiresAt?: number;
}

/**
 * Reads the subject and expiry of a JWT for display. The signature is not
 * checked here; the backend does that on every request.
 */
export function tokenInfo(token: string | null): TokenInfo | null {
  if (!token) return null;
  const part = token.split('.')[1];
  if (!part) return null;
  try {
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '='));
    const claims = JSON.parse(json) as { sub?: unknown; exp?: unknown };
    if (typeof claims.sub !== 'string' || claims.sub === '') return null;
    return { sub: claims.sub, expiresAt: typeof claims.exp === 'number' ? claims.exp * 1000 : undefined };
  } catch {
    return null;
  }
}

export function isExpired(info: TokenInfo | null, now = Date.now()): boolean {
  return info?.expiresAt !== undefined && info.expiresAt <= now;
}

/**
 * A 401 about the platform token carries `WWW-Authenticate: Bearer`. Other
 * 401s (a worker refusing an SSH password during onboarding) do not, and must
 * not send the user to the sign-in dialog.
 */
export function isSignInRequired(res: Pick<Response, 'status' | 'headers'>): boolean {
  return res.status === 401 && /bearer/i.test(res.headers.get('www-authenticate') ?? '');
}

function reportAuthRequired(res: Response) {
  if (isSignInRequired(res) && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(AUTH_REQUIRED_EVENT));
  }
}

export function getAuthHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class ApiError extends Error {
  status: number;
  /** Raw `detail` from the response body (string, object or 422 list). */
  detail?: unknown;
  /** `error_code` from shared ErrorResponse bodies, when present. */
  code?: string;
  constructor(status: number, message: string, detail?: unknown, code?: string) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.code = code;
  }

  /** True when the message, code or detail mentions `needle` (e.g. "host_key_mismatch"). */
  mentions(needle: string): boolean {
    const hay = [this.message, this.code ?? '', typeof this.detail === 'string' ? this.detail : JSON.stringify(this.detail ?? '')]
      .join(' ')
      .toLowerCase();
    return hay.includes(needle.toLowerCase());
  }
}

/** Human-readable text for one FastAPI `detail` value. */
function detailToText(detail: unknown): string | null {
  if (detail == null) return null;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // FastAPI 422: [{loc: ["body", "port"], msg: "...", type: "..."}]
    const parts = detail
      .map((d: any) => {
        if (d && typeof d === 'object' && typeof d.msg === 'string') {
          const loc = Array.isArray(d.loc) ? d.loc.filter((x: unknown) => x !== 'body').join('.') : '';
          return loc ? `${loc}: ${d.msg}` : d.msg;
        }
        return typeof d === 'string' ? d : null;
      })
      .filter(Boolean);
    return parts.length ? parts.join('; ') : null;
  }
  if (typeof detail === 'object') {
    const o = detail as Record<string, unknown>;
    for (const k of ['message', 'msg', 'detail', 'error']) {
      if (typeof o[k] === 'string') return o[k] as string;
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return null;
    }
  }
  return String(detail);
}

/**
 * Builds an ApiError from a failed Response. Understands both error shapes the
 * backend produces:
 *   - FastAPI HTTPException / validation: `{ "detail": "..." | [...] | {...} }`
 *   - shared/common/error_handlers.py ErrorResponse:
 *     `{ "error_code", "message", "detail", "timestamp" }`
 */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  let text = '';
  try {
    text = await res.text();
  } catch {
    /* body unreadable */
  }
  let parsed: any = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    /* body wasn't JSON */
  }

  const fallback = res.statusText || `HTTP ${res.status}`;
  if (!parsed || typeof parsed !== 'object') {
    const plain = text.trim().slice(0, 300);
    return new ApiError(res.status, plain || fallback);
  }

  const detailText = detailToText(parsed.detail);
  const messageText = typeof parsed.message === 'string' ? parsed.message : null;
  let message: string;
  if (messageText && detailText && !messageText.includes(detailText)) {
    message = `${messageText}: ${detailText}`;
  } else {
    message = messageText ?? detailText ?? fallback;
  }
  return new ApiError(res.status, message, parsed.detail, typeof parsed.error_code === 'string' ? parsed.error_code : undefined);
}

/** Message for any thrown value (ApiError, Error, string). */
export function errorMessage(e: unknown, fallback = 'Request failed'): string {
  if (e instanceof Error && e.message) return e.message;
  if (typeof e === 'string' && e) return e;
  return fallback;
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown; // JSON-serializable, OR a FormData/Blob sent as it is (uploads)
  isFormData?: boolean;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, isFormData = false } = options;

  const headers: Record<string, string> = { ...getAuthHeaders() };
  if (body !== undefined && !isFormData) headers['Content-Type'] = 'application/json';

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : isFormData ? (body as BodyInit) : JSON.stringify(body),
  });

  if (!res.ok) {
    reportAuthRequired(res);
    throw await errorFromResponse(res);
  }

  // 204 / empty body responses
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}

/** GET a file with auth and hand it to the browser as a download. */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const res = await fetch(path, { headers: getAuthHeaders() });
  if (!res.ok) {
    reportAuthRequired(res);
    throw await errorFromResponse(res);
  }
  const blob = await res.blob();
  // Prefer the server's name: its extension depends on what was produced.
  const served = /filename="([^"]+)"/.exec(res.headers.get('content-disposition') ?? '')?.[1];
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = served ?? filename;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
