// Mobile HTTP client. Wraps axios with two mandatory concerns:
//   1. Inject `X-CSM-Client: 1` on every request — the desktop backend's
//      RequireClientHeaderMiddleware (backend/csm/main.py) 400s any
//      /api/* write without it.
//   2. Access-token handling — if the backend has `settings.access_token`
//      set, requests must carry it via cookie / x-csm-token header.
//      We support first-visit ?token=... query bootstrap by storing the
//      token in localStorage; the backend also sets an httpOnly cookie
//      after the first successful call.
import axios, {
  type AxiosInstance,
  type InternalAxiosRequestConfig,
  type AxiosResponse,
} from "axios";
import { showToast } from "vant";
import { installPerfLog } from "../lib/perfLog";

const ACCESS_TOKEN_KEY = "csm_access_token";

function readTokenFromLocation(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("token");
}

function persistToken(token: string) {
  try {
    localStorage.setItem(ACCESS_TOKEN_KEY, token);
  } catch {
    // localStorage disabled (e.g. Safari private mode) — cookie fallback
    document.cookie = `${ACCESS_TOKEN_KEY}=${token}; path=/; SameSite=Strict`;
  }
}

function loadToken(): string | null {
  try {
    return localStorage.getItem(ACCESS_TOKEN_KEY);
  } catch {
    const m = document.cookie.match(new RegExp(`${ACCESS_TOKEN_KEY}=([^;]+)`));
    return m ? m[1] : null;
  }
}

const bootstrap = readTokenFromLocation();
if (bootstrap) persistToken(bootstrap);

export const http: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  // Generous default so a request survives a laggy SSH-tunnel stall instead of
  // failing at 10s (mobile tunnels periodically stall for 10-30s).
  timeout: 20_000,
  withCredentials: true,
});

http.interceptors.request.use((cfg: InternalAxiosRequestConfig) => {
  cfg.headers = cfg.headers || {};
  cfg.headers["X-CSM-Client"] = "1";
  const token = loadToken();
  if (token) cfg.headers["x-csm-token"] = token;
  return cfg;
});

// Latency instrumentation (surface='mobile'), same mechanism as the desktop
// build — records failures always, everything in verbose mode, correlated with
// the backend via X-Request-Id. Inspect via window.__csmPerf. See lib/perfLog.ts.
installPerfLog(http, "mobile");

const MAX_RETRIES = 2;

http.interceptors.response.use(
  (r: AxiosResponse) => r,
  async (err) => {
    const status = err?.response?.status;
    const cfg = err?.config as
      | (InternalAxiosRequestConfig & { __retryCount?: number })
      | undefined;

    // Retry idempotent GETs on transient tunnel failures (timeout / no
    // response). A laggy SSH-over-mobile tunnel stalls for seconds; the same
    // request usually succeeds on a short-delayed retry, which keeps the UI
    // from flashing errors. Non-GET writes are NOT auto-retried UNLESS they
    // carry an idempotency key (`__idempotent`) — e.g. session chat sends,
    // where the backend dedups by client_msg_id so a lost response is safe to
    // resend without double-typing into the PTY.
    const transient = !err.response || err.code === "ECONNABORTED";
    const isGet = (cfg?.method || "get").toLowerCase() === "get";
    const idempotent =
      (cfg as { __idempotent?: boolean } | undefined)?.__idempotent === true;
    // The health probe opts out: it is its own debounce (5 consecutive misses),
    // so letting the interceptor retry each probe ×3 would stack to ~9 attempts
    // and lag the offline banner by 60-90s.
    const noRetry = (cfg as { __noRetry?: boolean } | undefined)?.__noRetry === true;
    if (cfg && transient && (isGet || idempotent) && !noRetry) {
      cfg.__retryCount = (cfg.__retryCount ?? 0) + 1;
      if (cfg.__retryCount <= MAX_RETRIES) {
        await new Promise((r) => setTimeout(r, 400 * cfg.__retryCount!));
        return http(cfg);
      }
    }

    if (status === 401) {
      try {
        localStorage.removeItem(ACCESS_TOKEN_KEY);
      } catch {
        /* noop */
      }
      showToast({
        message: "Access token invalid; append ?token=... to /m/ URL",
        type: "fail",
      });
    }
    // Note: no per-error "network unreachable" toast — it flapped on every
    // tunnel stall. The OfflineBanner (5 consecutive health-probe misses) owns
    // that.
    return Promise.reject(err);
  }
);

// DELETE /api/sessions/{sid} blocks up to 15s server-side — need bigger
// timeout when calling that endpoint. Helper wrapper exposes it.
export function httpLongDelete(url: string) {
  return http.delete(url, { timeout: 25_000 });
}
