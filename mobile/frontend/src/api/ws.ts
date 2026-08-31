// WebSocket client for the mobile SPA.
//
// Features:
//   - Exponential backoff reconnect (1s → 2s → 4s → 8s → capped 30s). The
//     backoff is only reset AFTER the socket has stayed open for a stability
//     window — a half-open tunnel that opens then instantly drops must not
//     keep zeroing the counter (that produced a fixed 1s reconnect storm).
//   - App-level heartbeat: client sends "ping" every 25s; the backend replies
//     {type:"pong"}. A staleness watchdog force-closes (→ reconnect) when no
//     inbound frame arrives for 45s, so a silently-dead socket (readyState
//     stuck OPEN, no FIN/RST through a stalled SSH tunnel) is detected instead
//     of the chat silently freezing.
//   - visibilitychange handler: on returning to foreground, reconnect after a
//     small delay while PRESERVING the backoff counter (no thrash).
//   - Access-token propagation via ?token=... query (backend WS bootstrap
//     reads access_token from URL — browsers don't let JS attach headers
//     to `new WebSocket`).
//   - Typed dispatch: `onEvent` receives the discriminated `WSEvent` union
//     from ws-events.d.ts; a JSON parse fallback still delivers raw
//     unknown to `onRaw` for logging / debugging.

import type { WSEvent } from "./ws-events";
import { isConnectionDead } from "../lib/wsLiveness";

const ACCESS_TOKEN_KEY = "csm_access_token";

function currentToken(): string | null {
  try {
    return localStorage.getItem(ACCESS_TOKEN_KEY);
  } catch {
    return null;
  }
}

function buildWsUrl(path: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  const token = currentToken();
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${scheme}://${host}${path}${qs}`;
}

export interface ChatWebSocketOptions {
  /** Called with each typed event (session_status / history / transcript / error). */
  onEvent?: (ev: WSEvent) => void;
  /** Called with the raw parsed payload — useful for logging unknown types. */
  onRaw?: (data: unknown) => void;
  onOpen?: () => void;
  onClose?: (ev: CloseEvent) => void;
  /** Called before each reconnect attempt; the `attempt` counter starts at 1. */
  onReconnect?: (attempt: number, delayMs: number) => void;
  /** Called once when auto-reconnect gives up (attempt cap hit) so the UI can
   *  stop the spinner and offer a manual retry. */
  onGaveUp?: () => void;
}

/**
 * App-level close codes the backend uses to signal a PERMANENT rejection —
 * reconnecting against these just loops forever. 4401 = auth, 4404 = not
 * found / not live, 4500 = agent has no chat surface (its transcript isn't
 * one route_record() can parse — claude and codex both are). NOTE: 4503
 * ("not ready yet — no external_session_id, or transcript not on disk") is
 * deliberately NOT here: it is retryable (the session may still become
 * chattable), handled by bounded auto-reconnect below.
 */
const TERMINAL_CLOSE_CODES = new Set([4401, 4404, 4500]);

/** Cap auto-reconnect so a never-ready session (repeated 4503) or a dead tunnel
 *  can't loop a fresh handshake every 30s forever, draining cellular battery /
 *  data. On hitting the cap we surface onGaveUp() and stop; the user can
 *  manually retry. A healthy connection resets `attempts` after the stability
 *  window, so this only trips on sustained failure. */
const MAX_RECONNECT_ATTEMPTS = 8;

/** Heartbeat / staleness tuning. Server pings at 30s (uvicorn --ws-ping-interval),
 *  our app-level ping is faster so the watchdog window can be tight without
 *  false-tripping on a healthy-but-idle stream. */
const PING_INTERVAL_MS = 25_000;
/** How long an UNANSWERED ping may stand before the link is called dead.
 *  Replaces the old `STALE_AFTER_MS = 45_000` deadline — see `lib/wsLiveness`
 *  for why an elapsed-time rule cannot work on a throttled timer. */
const PONG_GRACE_MS = 10_000;
const WATCHDOG_INTERVAL_MS = 15_000;
/** How long the socket must stay OPEN before we trust it and reset backoff. */
const STABILITY_WINDOW_MS = 8_000;

export class ChatWebSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private permanentlyClosed = false;
  /** True only for a PERMANENT server rejection (4401/4404/4500) — those must
   *  never auto-retry. A backoff-exhausted give-up is NOT terminal: returning
   *  to the foreground gets a fresh chance (see visibilityHandler). */
  private terminal = false;
  private attempts = 0;
  private reconnectTimer: number | null = null;
  private stabilityTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private watchdogTimer: number | null = null;
  /** Monotonic (`performance.now`) throughout. A wall clock jumps on NTP
   *  correction and on suspend/resume — which a phone does constantly — and
   *  that alone was enough to fire a false "connection dead" verdict. */
  private lastInboundTs = 0;
  private lastPingSentTs = 0;
  private visibilityHandler: () => void;

  constructor(
    private readonly path: string,
    private readonly opts: ChatWebSocketOptions = {}
  ) {
    this.visibilityHandler = () => {
      if (this.closed || this.terminal) return;
      if (document.visibilityState !== "visible") return;
      // A backoff-exhausted give-up is not permanent: coming back to the
      // foreground is a fresh chance, so clear the flag and reset the counter
      // and try again (instead of stranding the user on "Disconnected" until a
      // manual tap — the previous behavior).
      if (this.permanentlyClosed) {
        this.permanentlyClosed = false;
        this.attempts = 0;
      }
      const rs = this.ws?.readyState;
      // Only reconnect when the socket is actually dead — NOT while it is
      // still CONNECTING (0) or already OPEN (1); doing so would orphan the
      // in-flight socket and create a duplicate stream.
      if (rs == null || rs === WebSocket.CLOSING || rs === WebSocket.CLOSED) {
        if (this.reconnectTimer !== null) {
          clearTimeout(this.reconnectTimer);
          this.reconnectTimer = null;
        }
        // Nudge a prompt reconnect on foreground, but PRESERVE `attempts` — a
        // user idly toggling the app on a flaky tunnel must not wipe the
        // backoff and drive back-to-back reconnects.
        this.scheduleReconnect(500);
      }
    };
    document.addEventListener("visibilitychange", this.visibilityHandler);
    this.connect();
  }

  private connect() {
    if (this.closed) return;
    const url = buildWsUrl(this.path);
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this.lastInboundTs = performance.now();
      this.lastPingSentTs = 0; // fresh socket — nothing outstanding yet
      this.startHealthTimers();
      this.opts.onOpen?.();
    };
    this.ws.onmessage = (ev) => {
      this.lastInboundTs = performance.now();
      // Any inbound frame (data OR pong) proves the link actually works — reset
      // the backoff counter. The stability-timer reset only fires after the
      // socket stays OPEN for 8s, which a half-open tunnel that flaps every few
      // seconds never reaches, so it would climb straight to give-up. A received
      // frame is the stronger, immediate proof.
      if (this.attempts !== 0) this.attempts = 0;
      let parsed: unknown = ev.data;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        /* leave as string */
      }
      // Heartbeat ack — keeps the watchdog fed but carries no app payload.
      if (
        parsed &&
        typeof parsed === "object" &&
        (parsed as { type?: unknown }).type === "pong"
      ) {
        return;
      }
      this.opts.onRaw?.(parsed);
      if (parsed && typeof parsed === "object" && "type" in parsed) {
        this.opts.onEvent?.(parsed as WSEvent);
      }
    };
    this.ws.onclose = (ev) => {
      this.stopHealthTimers();
      this.opts.onClose?.(ev);
      if (this.closed) return;
      if (TERMINAL_CLOSE_CODES.has(ev.code)) {
        // Permanent rejection — stop retrying and let the consumer surface it.
        this.permanentlyClosed = true;
        this.terminal = true;
        return;
      }
      this.scheduleReconnect();
    };
    this.ws.onerror = () => {
      // Let onclose handle reconnection; onerror often fires just before close.
    };
  }

  /** Start the stability, heartbeat, and staleness timers for a fresh socket. */
  private startHealthTimers() {
    this.stopHealthTimers();
    // Reset backoff ONLY after the socket has proven stable for a window.
    this.stabilityTimer = window.setTimeout(() => {
      this.attempts = 0;
      this.stabilityTimer = null;
    }, STABILITY_WINDOW_MS);
    // App-level ping; the backend answers {type:"pong"}, feeding the watchdog.
    this.heartbeatTimer = window.setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        try {
          this.ws.send("ping");
          // Stamp only on a SUCCESSFUL send: a ping that never left must not
          // start a grace period whose expiry would then be blamed on the peer.
          this.lastPingSentTs = performance.now();
        } catch {
          /* send on a dying socket — watchdog/onclose will handle it */
        }
      }
    }, PING_INTERVAL_MS);
    // Liveness watchdog. Dead means "a ping we actually sent went unanswered",
    // NOT "no frame arrived in the last 45s". The old deadline measured elapsed
    // time on a timer the OS is free to stall: Android Doze and a backgrounded
    // WebView throttle this interval to roughly one tick a minute, so the first
    // throttled tick saw ~60s of apparent silence and closed a perfectly
    // healthy socket — before ever probing it, because the verdict ran ahead of
    // the ping. That is what the "phone keeps disconnecting" reports were.
    this.watchdogTimer = window.setInterval(() => {
      if (this.ws?.readyState !== WebSocket.OPEN) return;
      if (
        isConnectionDead({
          lastInboundTs: this.lastInboundTs,
          lastPingSentTs: this.lastPingSentTs,
          now: performance.now(),
          graceMs: PONG_GRACE_MS,
        })
      ) {
        try {
          this.ws.close();
        } catch {
          /* ignore */
        }
      }
    }, WATCHDOG_INTERVAL_MS);
  }

  private stopHealthTimers() {
    if (this.stabilityTimer !== null) {
      clearTimeout(this.stabilityTimer);
      this.stabilityTimer = null;
    }
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.watchdogTimer !== null) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
  }

  private scheduleReconnect(explicitDelay?: number) {
    if (this.reconnectTimer !== null) return;
    if (this.attempts >= MAX_RECONNECT_ATTEMPTS) {
      this.permanentlyClosed = true;
      this.opts.onGaveUp?.();
      return;
    }
    this.attempts++;
    const backoff =
      explicitDelay ?? Math.min(1000 * 2 ** (this.attempts - 1), 30_000);
    this.opts.onReconnect?.(this.attempts, backoff);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, backoff);
  }

  send(payload: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(
        typeof payload === "string" ? payload : JSON.stringify(payload)
      );
    }
  }

  /** Send raw bytes — used by session PTY writes (stdin, Ctrl-C 0x03).
   * Agent conversation WS is server-push only; this is only meaningful
   * for /api/sessions/{sid}/ws. */
  sendBytes(bytes: Uint8Array | ArrayBuffer): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(bytes);
    return true;
  }

  get isOpen(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  close() {
    this.closed = true;
    document.removeEventListener("visibilitychange", this.visibilityHandler);
    this.stopHealthTimers();
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }
}

/** Composable helpers for Vue components. */

export function useAgentConversationSocket(
  cid: string,
  opts: ChatWebSocketOptions
): ChatWebSocket {
  // Backend agents router has prefix="/api", so WS path lives under /api/ws/*.
  // Do not "simplify" this to /ws/... — it will 404.
  return new ChatWebSocket(`/api/ws/agents/conversations/${cid}`, opts);
}

/** Structured message stream for a regular claude session (mobile chat).
 *  Backend: WS /api/sessions/{sid}/messages (JSONL tail → same event shape). */
export function useSessionMessageSocket(
  sid: string,
  opts: ChatWebSocketOptions
): ChatWebSocket {
  return new ChatWebSocket(`/api/sessions/${sid}/messages`, opts);
}
