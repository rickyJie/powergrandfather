import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebglAddon } from '@xterm/addon-webgl'
import { CanvasAddon } from '@xterm/addon-canvas'
import '@xterm/xterm/css/xterm.css'
import { attachXtermFileLinks } from './useXtermFileLinks'
import { TickRecorder, reportWsClose, visState } from '../lib/wsDiag'
import { isConnectionDead } from '../lib/wsLiveness'

/**
 * useTerminalManager — encapsulates xterm.js terminal lifecycle for a single
 * PTY-backed session pane.
 *
 * Extracted from `views/Sessions.vue` (H1 P1). What lives here:
 *   - Terminal instance + FitAddon + WebGL/Canvas/DOM renderer selection
 *   - WebSocket wiring (binary frames → term.write, term.onData → ws.send)
 *   - ResizeObserver + scheduleFit (120ms debounce) + safety-net refit
 *   - attachXtermFileLinks integration (clickable file paths / s3:// URIs)
 *   - Handler-clearing on detach so a dying socket's `onclose` cannot write
 *     "[disconnected]" into the next attached terminal
 *   - Monotonic attachToken guard so overlapping attach() calls collapse
 *     to a single live terminal instance
 *
 * What stays in the caller (Sessions.vue) — reason:
 *   - Right-click context-menu clipboard: needs project-level toast + paste
 *     mode state (secure context detection, permission denied warnings)
 *   - Closed-edge watch (session ended → detach): needs Session domain state
 *   - Fullscreen toggle, splitter drag, focus management
 *   - Skeleton spinner state: caller-owned UX flag
 *
 * The composable is intentionally NOT a Vue-lifecycle-bound thing (no
 * onUnmounted hook inside). Caller decides when attach/detach run — needed
 * because Sessions.vue re-attaches on activeSid changes without unmounting.
 */

const MIN_COLS = 80
const NARROW_MIN_COLS = 28
const MIN_ROWS = 24
const NARROW_MIN_ROWS = 8

export type Renderer = 'webgl' | 'canvas' | 'dom'
export type TerminalConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'disconnected'
// Why a 'disconnected' state is terminal (no auto-retry). Lets the UI show a
// specific message and hide the useless "Reconnect now" button (F3):
//   'ended'        — server closed 4404 (session no longer live). Reconnecting
//                    just gets another 4404 forever.
//   'unauthorized' — server closed 4401/4403.
export type TerminalDisconnectReason = 'ended' | 'unauthorized'

export interface TerminalManagerOptions {
  /** Called whenever the user types / pastes into the terminal. */
  onData?: (data: string) => void
  /** Called when the WebSocket transitions to OPEN. */
  onOpen?: () => void
  /** Called when the WebSocket closes (any reason). */
  onClose?: () => void
  /** Browser↔PTY connection state, separate from the agent/process state. */
  onConnectionState?: (state: TerminalConnectionState, attempt: number, reason?: TerminalDisconnectReason) => void
  /** Called for text WS frames that couldn't be parsed as JSON errors. */
  onTextFrame?: (data: string) => void
  /**
   * Called for text WS frames that parse to `{ error: string, ... }` shape.
   * Return `true` if handled (default handler will not run). Default handler
   * writes a red banner into the terminal.
   */
  onErrorFrame?: (err: { error: string; [k: string]: unknown }) => boolean | void
  fontFamily?: string
  fontSize?: number
  /** Xterm scrollback lines. Default 5000. */
  scrollback?: number
  /** Emit debug lines to console under `import.meta.env.DEV`. Default true. */
  debug?: boolean
}

export interface TerminalManager {
  /**
   * Mount an xterm terminal into `mountEl`, open `wsUrl`, and start streaming.
   * Awaits `document.fonts.ready` + 2 RAF so Geist Mono metrics are stable
   * before `term.open()` — see comment in `attach()`.
   *
   * Safe to call while a previous attach is in-flight or already attached:
   * the previous instance is detached first, and any earlier in-flight
   * attach is invalidated via the internal attachToken.
   */
  attach: (mountEl: HTMLElement, wsUrl: string, sid?: string | null) => Promise<void>
  /** Tear down: dispose xterm, close ws, disconnect observers, clear timers. */
  detach: () => void
  /** Force an immediate FitAddon.proposeDimensions → term.resize + WS resize. */
  fit: () => void
  /** Debounced fit (120ms). Cheap to call from ResizeObserver / rAF. */
  scheduleFit: () => void
  /** Force xterm to repaint all rows without changing dimensions. Used to
   *  recover from renderer/state drift (WebGL frame drops on tab switch,
   *  partial repaints after fullscreen toggle) that a fit() wouldn't
   *  catch because the container size didn't change. */
  refresh: () => void
  /** Unconditional PTY winsize resync + repaint. Unlike fit() (which
   *  short-circuits when xterm dimensions haven't changed), forceSync
   *  always re-sends the current size to the PTY. Use at drift-prone
   *  moments: post-fullscreen toggle, post-splitter drag, after any
   *  layout change that the internal ResizeObserver may have missed.
   *  Rationale: codex / vim / any full-screen TUI on the other end
   *  caches SIGWINCH; any missed resize leaves them drawing at a stale
   *  size and content past the (new) visible bounds gets clipped. */
  forceSync: () => void
  /** Server → terminal write (bytes or string). */
  writeTo: (data: string | Uint8Array) => void
  /** Which renderer is currently active. null before attach / after detach. */
  getRenderer: () => Renderer | null
  /** True iff the WebSocket is OPEN. */
  isConnected: () => boolean
  /** Xterm .focus() convenience passthrough. */
  focus: () => void
  /** Read-only access to the underlying Terminal (for selection, paste, ...). */
  getTerminal: () => Terminal | null
  /** The mount element passed to the most recent attach(), if still attached. */
  getMountElement: () => HTMLElement | null
  /** Retry immediately after a failed/disconnected socket. */
  reconnect: () => void
}

export function useTerminalManager(opts: TerminalManagerOptions = {}): TerminalManager {
  const debug = opts.debug !== false
  const scrollback = opts.scrollback ?? 5000
  const fontSize = opts.fontSize ?? 14

  let term: Terminal | null = null
  let fitAddon: FitAddon | null = null
  let ws: WebSocket | null = null
  let resizeObserver: ResizeObserver | null = null
  let resizeDebounce: number | null = null
  let fileLinksDisposable: { dispose(): void } | null = null
  let mountEl: HTMLElement | null = null
  let renderer: Renderer | null = null
  let reconnectTimer: number | null = null
  let reconnectAttempt = 0
  let activeWsUrl: string | null = null
  let activeSocketToken = 0

  // Application-level heartbeat (A1). The PTY WS has no protocol-level
  // keepalive we can observe: a NAT / reverse-proxy / SSH tunnel that
  // silently drops an idle TCP connection (no RST/FIN) leaves ws.readyState
  // OPEN forever, so typed input is swallowed and no reconnect ever fires —
  // the terminal looks alive but is dead. We send `{type:'ping'}` while idle
  // and the backend replies `{type:'pong'}`; ANY inbound frame (data or pong)
  // refreshes liveness. The socket is declared dead only when a ping we
  // actually sent goes unanswered for PONG_GRACE_MS — see startPing for why
  // this is a pairing check and not an elapsed-time deadline. Closing drops
  // into the normal onclose→reconnect path.
  let pingTimer: number | null = null
  // Monotonic (performance.now) throughout — Date.now() is wall-clock and
  // jumps on an NTP correction or a suspend/resume, which on its own is
  // enough to make a liveness check fire against a healthy socket.
  let lastInboundTs = 0
  let lastPingSentTs = 0
  const PING_INTERVAL_MS = 20_000
  // How long a ping may go unanswered before we call the socket dead. This is
  // NOT a staleness deadline: it is only ever consulted once a ping we
  // actually sent has gone unanswered, so a late tick cannot turn it into a
  // false positive. Measured pong RTT through the SSH tunnel is 15-105ms.
  const PONG_GRACE_MS = 10_000
  // wsDiag (temporary, 2026-08-24): observation only — nothing below reads
  // these to decide anything. See lib/wsDiag.ts.
  let closedByWatchdog = false
  const tickRec = new TickRecorder()

  // Monotonic guard — every attach captures its value on entry and re-checks
  // after each await. detach() bumps it, forcing any in-flight attach to bail
  // before it creates a second Terminal/WebSocket/WebGL context. Without this
  // the async gap in attach (fonts.ready + 2 RAF) lets concurrent invocations
  // pile up when the caller re-attaches during a mount/remount.
  let attachToken = 0

  function log(...args: unknown[]) {
    if (debug && import.meta.env.DEV) console.info('[terminal]', ...args)
  }
  function warn(...args: unknown[]) {
    if (debug && import.meta.env.DEV) console.warn('[terminal]', ...args)
  }

  function computeFitAndSend(opts: { force?: boolean } = {}) {
    if (!fitAddon || !term || !ws || ws.readyState !== WebSocket.OPEN) return
    try {
      const dim = fitAddon.proposeDimensions()
      if (!dim) return
      const narrow = (mountEl?.clientWidth ?? window.innerWidth) <= 640
      const minCols = narrow ? NARROW_MIN_COLS : MIN_COLS
      const minRows = narrow ? NARROW_MIN_ROWS : MIN_ROWS
      const cols = Math.max(minCols, dim.cols)
      const rows = Math.max(minRows, dim.rows)
      const dimensionsChanged = cols !== term.cols || rows !== term.rows
      // Fast path: xterm-side is already at target size AND caller isn't
      // requesting a forced PTY resync → skip. The `opts.force=true` mode
      // is used by forceSync() below to defend against xterm↔PTY winsize
      // drift (codex is especially sensitive: ratatui redraws use the
      // cached SIGWINCH size, so any missed resize compounds).
      if (!dimensionsChanged && !opts.force) return
      if (dimensionsChanged) term.resize(cols, rows)
      ws.send(JSON.stringify({ type: 'resize', cols, rows }))
    } catch (_) {
      /* transient measure errors — next fit will catch up */
    }
  }

  function scheduleFit() {
    if (resizeDebounce != null) window.clearTimeout(resizeDebounce)
    resizeDebounce = window.setTimeout(() => {
      resizeDebounce = null
      computeFitAndSend()
    }, 120)
  }

  function fit() {
    computeFitAndSend()
  }

  // Unconditional PTY winsize resync + repaint. Use at moments where we
  // suspect xterm and the PTY may have drifted (WS reconnect, visibility
  // restore, explicit user "reconnect", post-attach settle) — the plain
  // fit() short-circuits when xterm's cell dimensions haven't changed,
  // which misses the failure mode where the PTY side lost / never got
  // the resize message and codex/vim/etc. are drawing at a stale size.
  // Repro: drag CSM splitter or toggle fullscreen while codex is
  // "working" — codex ends up drawing rows past the visible bottom.
  function forceSync() {
    computeFitAndSend({ force: true })
    refresh()
  }

  function refresh() {
    if (!term) return
    try {
      term.refresh(0, Math.max(0, term.rows - 1))
    } catch (_) { /* transient — nothing worth logging */ }
  }

  // iOS/Android resize the visual viewport when the software keyboard opens,
  // sometimes without resizing the layout viewport immediately. ResizeObserver
  // alone therefore misses the most important mobile terminal resize.
  const onVisualViewportResize = () => scheduleFit()

  // When the tab is backgrounded then re-shown, the WebGL renderer's
  // frame buffer can be discarded by the browser; xterm may not repaint
  // until something (keystroke, resize) forces it. Result: user comes
  // back to a partially-rendered terminal ("lost input bar" bug).
  // Force a repaint AND resync PTY winsize on visibility restore —
  // while the tab was hidden the container may have re-flowed
  // (splitter drag, sidebar collapse) and the resize event never
  // reached the PTY, so pair the repaint with a forceSync.
  const onVisibilityChange = () => {
    if (document.visibilityState === 'visible') forceSync()
  }

  function detach() {
    // Bump first so any in-flight attach bails at its next guard check
    // instead of racing us to create a second terminal.
    attachToken++
    if (resizeDebounce != null) {
      window.clearTimeout(resizeDebounce)
      resizeDebounce = null
    }
    if (reconnectTimer != null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    stopPing()
    if (resizeObserver) {
      try { resizeObserver.disconnect() } catch (_) { /* ignore */ }
      resizeObserver = null
    }
    window.visualViewport?.removeEventListener('resize', onVisualViewportResize)
    window.visualViewport?.removeEventListener('scroll', onVisualViewportResize)
    document.removeEventListener('visibilitychange', onVisibilityChange)
    if (ws) {
      // Detach handlers BEFORE ws.close(). WebSocket close is asynchronous —
      // `onclose` fires on a later tick, by which point `term` already points
      // at the NEXT terminal. Without clearing handlers first, the old ws's
      // onclose writes "[disconnected]" into the freshly-attached new terminal.
      ws.onopen = null
      ws.onmessage = null
      ws.onclose = null
      ws.onerror = null
      try { ws.close() } catch (_) { /* ignore */ }
      ws = null
    }
    if (fileLinksDisposable) {
      try { fileLinksDisposable.dispose() } catch (_) { /* ignore */ }
      fileLinksDisposable = null
    }
    if (term) {
      try { term.dispose() } catch (_) { /* ignore */ }
      term = null
    }
    fitAddon = null
    mountEl = null
    renderer = null
    activeWsUrl = null
    reconnectAttempt = 0
    activeSocketToken = 0
  }

  function scheduleReconnect(myToken: number) {
    if (attachToken !== myToken || !activeWsUrl || reconnectTimer != null) return
    reconnectAttempt += 1
    opts.onConnectionState?.('reconnecting', reconnectAttempt)
    const delay = Math.min(750 * 2 ** (reconnectAttempt - 1), 15_000)
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      if (attachToken === myToken && activeWsUrl) {
        connectSocket(activeWsUrl, myToken, true)
      }
    }, delay)
  }

  function stopPing() {
    if (pingTimer != null) {
      window.clearInterval(pingTimer)
      pingTimer = null
    }
  }

  function startPing(socket: WebSocket, myToken: number, socketToken: number) {
    stopPing()
    lastInboundTs = performance.now()
    lastPingSentTs = 0
    tickRec.start()
    pingTimer = window.setInterval(() => {
      tickRec.tick()
      // Stop if this socket is no longer the active one.
      if (attachToken !== myToken || socketToken !== activeSocketToken || ws !== socket) {
        stopPing()
        return
      }
      if (socket.readyState !== WebSocket.OPEN) return
      // Liveness by PAIRING, not by deadline: dead means "a ping we actually
      // sent went unanswered", which compares two event stamps and is
      // therefore immune to this callback running late.
      //
      // The previous rule — "no inbound frame in the last 45s" — measured
      // elapsed wall time with a clock the browser is free to stall. A hidden
      // tab gets throttled to ~1 tick/min, so the first late tick saw ~60s of
      // silence and closed the socket, and because the verdict ran BEFORE the
      // send it never probed to check. Measured in the field: the pong to the
      // preceding ping had come back in 15-105ms — the socket was healthy and
      // we killed it anyway (18/18 samples, 2026-08-24).
      //
      // Under the same throttling this rule holds: the pong still landed
      // milliseconds after the ping, so lastInboundTs > lastPingSentTs and no
      // verdict is reached, however late the tick runs.
      if (isConnectionDead({
        lastInboundTs,
        lastPingSentTs,
        now: performance.now(),
        graceMs: PONG_GRACE_MS,
      })) {
        closedByWatchdog = true
        stopPing()
        try { socket.close() } catch (_) { /* onclose owns retry */ }
        return
      }
      try {
        socket.send(JSON.stringify({ type: 'ping' }))
        lastPingSentTs = performance.now()
      } catch (_) { /* ignore */ }
    }, PING_INTERVAL_MS)
  }

  function connectSocket(wsUrl: string, myToken: number, isReconnect: boolean) {
    if (attachToken !== myToken || !term) return
    const socketToken = ++activeSocketToken
    opts.onConnectionState?.(isReconnect ? 'reconnecting' : 'connecting', reconnectAttempt)
    const socket = new WebSocket(wsUrl)
    ws = socket
    socket.binaryType = 'arraybuffer'
    socket.onopen = () => {
      if (attachToken !== myToken || socketToken !== activeSocketToken || ws !== socket) {
        socket.close()
        return
      }
      if (isReconnect) {
        // reset() clears the screen; the server then replays its ring buffer
        // (default ~1MiB). Any PTY output produced while we were disconnected
        // beyond that window is gone — tell the user so a truncated scrollback
        // doesn't read as "the terminal rolled back / lost my work" (F2).
        term?.reset()
        term?.write('\x1b[90m[reconnected — restored recent history; '
          + 'output from a long outage may be truncated]\x1b[0m\r\n')
      }
      reconnectAttempt = 0
      // Force-resync the PTY on every socket open (not just resize-if-
      // changed). A reconnect can land on a PTY whose winsize was set by
      // a prior socket's fit but has since drifted from xterm's current
      // dimensions; the plain `computeFitAndSend()` would skip the resend
      // because xterm's `term.cols/rows` are unchanged. forceSync bypasses
      // that guard so codex / vim / any TUI on the other end always sees
      // an accurate SIGWINCH after we reattach.
      forceSync()
      setTimeout(() => {
        if (ws === socket && socket.readyState === WebSocket.OPEN) {
          // Post-replay resync: the server's ring-buffer replay finishes
          // ~200-300ms after onopen. Force another sync so codex re-
          // paints against the correct size AFTER the replay bytes
          // have landed (repro: `local:25495b5c` — input bar stayed
          // blank until the user dragged the splitter).
          forceSync()
        }
      }, 300)
      opts.onConnectionState?.('connected', 0)
      opts.onOpen?.()
      startPing(socket, myToken, socketToken)
    }
    socket.onmessage = (ev) => {
      if (attachToken !== myToken || socketToken !== activeSocketToken) return
      // Any inbound frame proves the connection is alive (A1 heartbeat).
      lastInboundTs = performance.now()
      if (ev.data instanceof ArrayBuffer) {
        term?.write(new Uint8Array(ev.data))
      } else if (typeof ev.data === 'string') {
        const trimmed = ev.data.trim()
        if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
          try {
            const obj = JSON.parse(trimmed) as { error?: string, type?: string }
            // Heartbeat pong — liveness already refreshed above; swallow it.
            if (obj && obj.type === 'pong') return
            if (obj && typeof obj.error === 'string') {
              const handled = opts.onErrorFrame?.(obj as { error: string })
              if (handled !== true) {
                term?.write(`\r\n\x1b[31m● ${obj.error}\x1b[0m\r\n`)
              }
              return
            }
          } catch (_) {
            // fall through to raw write
          }
        }
        if (opts.onTextFrame) opts.onTextFrame(ev.data)
        else term?.write(ev.data)
      }
    }
    socket.onclose = (ev) => {
      if (attachToken !== myToken || socketToken !== activeSocketToken || ws !== socket) return
      // wsDiag (temporary): record who closed this and how late our own
      // watchdog ticks were running. Deliberately after the staleness guard
      // above, so intentional detaches (navigation, remount) don't pollute
      // the sample set.
      reportWsClose({
        ch: 'term',
        by: closedByWatchdog ? 'watchdog' : 'other',
        code: ev.code,
        clean: ev.wasClean,
        sinceInbound: lastInboundTs ? Math.round(performance.now() - lastInboundTs) : -1,
        sincePing: lastPingSentTs ? Math.round(performance.now() - lastPingSentTs) : -1,
        gaps: tickRec.recent(),
        vis: visState(),
      })
      closedByWatchdog = false
      stopPing()
      ws = null
      opts.onClose?.()
      // Auth/not-live closes are terminal until the surrounding Session row
      // changes; retrying them creates an infinite error loop.
      if ([4401, 4403, 4404].includes(ev.code)) {
        const reason: TerminalDisconnectReason = ev.code === 4404 ? 'ended' : 'unauthorized'
        opts.onConnectionState?.('disconnected', reconnectAttempt, reason)
        return
      }
      term?.write('\r\n\x1b[33m[connection lost — retrying]\x1b[0m\r\n')
      scheduleReconnect(myToken)
    }
    socket.onerror = () => {
      // Browsers provide no useful error detail here; onclose owns retry.
    }
  }

  function reconnect() {
    if (!activeWsUrl || !term) return
    if (reconnectTimer != null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      ws.onclose = null
      try { ws.close() } catch (_) { /* ignore */ }
      ws = null
    }
    reconnectAttempt = Math.max(1, reconnectAttempt)
    connectSocket(activeWsUrl, attachToken, true)
  }

  function loadRenderer(myToken: number) {
    if (!term) return
    // WebGL (fastest) → Canvas (stable, ~5× DOM) → DOM (fallback).
    try {
      const webgl = new WebglAddon()
      webgl.onContextLoss(() => {
        try { webgl.dispose() } catch (_) { /* ignore */ }
        if (term) {
          try {
            term.loadAddon(new CanvasAddon())
            renderer = 'canvas'
            // Repaint immediately: the WebGL canvas went blank on context
            // loss and nothing redraws until the next PTY write, so without
            // this the screen can sit empty for seconds on an idle terminal.
            try { term.refresh(0, term.rows - 1) } catch (_) { /* ignore */ }
            log('webgl context lost → canvas')
          } catch (_) { /* ignore */ }
        }
      })
      term.loadAddon(webgl)
      renderer = 'webgl'
      log(`#${myToken} renderer: webgl`)
      return
    } catch (e) {
      warn(`#${myToken} webgl init failed, trying canvas:`, e)
    }
    try {
      term.loadAddon(new CanvasAddon())
      renderer = 'canvas'
      log(`#${myToken} renderer: canvas`)
      return
    } catch (e) {
      warn(`#${myToken} canvas init failed, falling back to DOM renderer:`, e)
    }
    // No addon loaded → xterm's built-in DOM renderer is active by default.
    renderer = 'dom'
  }

  async function attach(el: HTMLElement, wsUrl: string, sid?: string | null): Promise<void> {
    // Detach previous instance (bumps attachToken invalidating any in-flight).
    detach()
    const myToken = ++attachToken
    log(`#${myToken} attach begin`)

    // Flip state to 'connecting' immediately so the caller's banner
    // doesn't flash "Terminal disconnected. Reconnect now" during the
    // ~30ms preflight below (fonts.ready + 2 RAFs). Prior default was
    // whatever the previous session left behind — typically 'disconnected'
    // (set by detachTerm on the caller side when the previous session was
    // ended). The user-visible symptom: after clicking Resume on a closed
    // session, the freshly-mounted term-body pane briefly showed the
    // disconnected banner, and clicking Reconnect did nothing because
    // `term` / `activeWsUrl` were still null.
    opts.onConnectionState?.('connecting', 0)

    // Wait for the monospace font to load — xterm measures the font ONCE on
    // open() to compute cell dimensions; if the desired face isn't ready it
    // falls back and never re-measures, so characters end up clipped.
    //
    // Self-hosted @font-face files are lazy — the browser doesn't download
    // them until something on the page uses the family. `document.fonts.load()`
    // both kicks off the download AND resolves when the face is installed,
    // which `fonts.ready` alone does NOT (ready resolves trivially when there
    // are no *pending* loads). Fallback: an unregistered face still resolves
    // load() successfully (empty result set), so we won't hang.
    try {
      if ('fonts' in document && document.fonts?.load) {
        await Promise.race([
          Promise.all([
            document.fonts.load('14px "Geist Mono"'),
            document.fonts.load('bold 14px "Geist Mono"'),
          ]),
          new Promise((r) => setTimeout(r, 1500)),
        ])
      }
    } catch (_) { /* ignore */ }
    if (attachToken !== myToken) { log(`#${myToken} superseded after fonts`); return }

    // Two rAFs so SPA layout (modals, sidebars, splitter) is fully painted
    // and font metrics have settled before xterm measures cell height.
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
    if (attachToken !== myToken) { log(`#${myToken} superseded after RAF`); return }

    // Guard against caller-side unmount during the await gap.
    if (!el.isConnected) {
      log(`#${myToken} mount el detached before open()`)
      // Undo the optimistic 'connecting' so the banner reflects reality.
      opts.onConnectionState?.('disconnected', 0)
      return
    }

    mountEl = el

    // Belt-and-suspenders: `check()` trivially returns true when NO faces are
    // registered (MDN spec quirk: "no fonts that would need to be loaded"),
    // which historically fooled us into picking "Geist Mono" while the
    // browser had nothing to render with. Require at least one FontFace to
    // be present so a failed @font-face load correctly triggers fallback.
    const fontReady =
      'fonts' in document &&
      document.fonts.size > 0 &&
      document.fonts.check('14px "Geist Mono"')
    const defaultFontFamily = fontReady
      ? '"Geist Mono", "SF Mono", Menlo, monospace'
      : '"SF Mono", Menlo, Consolas, "Courier New", monospace'
    const fontFamily = opts.fontFamily ?? defaultFontFamily

    const narrow = el.clientWidth <= 640
    term = new Terminal({
      // Seed only; FitAddon.fit() below re-computes from actual container.
      cols: narrow ? NARROW_MIN_COLS : MIN_COLS,
      rows: narrow ? NARROW_MIN_ROWS : MIN_ROWS,
      fontFamily,
      fontSize,
      lineHeight: 1.2,
      letterSpacing: 0,
      theme: {
        background: '#1A1A1A',
        foreground: '#DCDCDC',
        cursor: '#DCDCDC',
        cursorAccent: '#1A1A1A',
      },
      cursorBlink: true,
      convertEol: true,
      scrollback,
      allowProposedApi: true,
      // stdin ENABLED — xterm handles all keystrokes (arrows, Tab, Ctrl-*,
      // function keys, bracketed paste, IME). They flow to PTY via onData.
      disableStdin: false,
    })
    fitAddon = new FitAddon()
    term.loadAddon(fitAddon)
    term.open(el)

    // Swallow Ctrl/Meta+Z when xterm has focus. PTY-side apps treat \x1a
    // as SIGTSTP / suspend, which lands claude / codex in an unrecoverable
    // half-state (repro: `local:2c222595` — Cmd+Z froze the chat and the
    // only way out was to kill the session; a subsequent resume then
    // reattached to the wrong claude_session_id chain).
    // attachCustomKeyEventHandler is xterm-focus-scoped, so keychords in
    // form inputs / editable divs elsewhere on the page are unaffected.
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== 'keydown') return true
      if (!(ev.metaKey || ev.ctrlKey) || ev.altKey) return true
      if (ev.key.toLowerCase() === 'z') return false
      return true
    })

    loadRenderer(myToken)

    // Attach xterm file / s3 link providers. Absolute paths
    // and `s3://` URIs in the terminal buffer become clickable and open
    // in a fresh tab. Disposed in detach().
    try {
      fileLinksDisposable = attachXtermFileLinks(term, sid)
    } catch (e) {
      warn('file link providers failed to register:', e)
    }

    activeWsUrl = wsUrl
    reconnectAttempt = 0
    connectSocket(wsUrl, myToken, false)

    // Primary input path: whatever xterm captures (keystrokes, paste including
    // bracketed-paste \x1b[200~..\x1b[201~, IME committed characters) → PTY.
    term.onData((data) => {
      if (opts.onData) {
        opts.onData(data)
        return
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(new TextEncoder().encode(data))
    })

    // Watch container for size changes (window resize, panel collapse) → refit.
    const container = el.parentElement
    if (container && 'ResizeObserver' in window) {
      resizeObserver = new ResizeObserver(() => scheduleFit())
      resizeObserver.observe(container)
    }
    window.visualViewport?.addEventListener('resize', onVisualViewportResize)
    window.visualViewport?.addEventListener('scroll', onVisualViewportResize)
    document.addEventListener('visibilitychange', onVisibilityChange)

    // Post-attach safety-net: 2 rAF after mount so any final layout shift
    // (splitter animation, sidebar collapse, fullscreen toggle happening
    // during attach) has settled before we re-measure. forceSync (not
    // plain fit) so the PTY winsize is unconditionally re-sent even if
    // the xterm-side cell dimensions haven't visibly changed since the
    // previous seed — otherwise codex-style TUIs that cache SIGWINCH
    // can end up drawing against the seed 80×24 instead of the actual
    // container size.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (attachToken !== myToken) return
      forceSync()
    }))
  }

  function writeTo(data: string | Uint8Array) {
    term?.write(data as string) // xterm accepts either signature at runtime
  }

  function getRenderer(): Renderer | null {
    return renderer
  }

  function isConnected(): boolean {
    return !!ws && ws.readyState === WebSocket.OPEN
  }

  function focus() {
    term?.focus()
  }

  function getTerminal(): Terminal | null {
    return term
  }

  function getMountElement(): HTMLElement | null {
    return mountEl
  }

  return {
    attach,
    detach,
    fit,
    scheduleFit,
    refresh,
    forceSync,
    writeTo,
    getRenderer,
    isConnected,
    focus,
    getTerminal,
    getMountElement,
    reconnect,
  }
}
