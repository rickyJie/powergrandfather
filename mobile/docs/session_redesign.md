# Mobile redesign — session-only companion (2026-08-18)

Supersedes the "mirror the whole desktop console" shape. The phone app is now a
**session companion**: watch a running Claude Code session, reply, and interrupt
it — nothing else. Web and mobile stay in sync because they are the **same
backend**, not because mobile re-implements anything.

## 1. Connectivity — keep the APK, fix it (do NOT chase Doze)

The reported failure is **active-use** ("screen on, normal use, can't complete
one full interaction"), so it is NOT the Doze / OEM-background-kill problem.
Root cause, verified against **sshj 0.38.0 bytecode**, lived in
`android/.../SshTunnelService.kt`:

- **P0 — busy-spin hang (the cause).** On a transient SSH blip the watcher did a
  bare `ss.close()`. sshj's `LocalPortForwarder.listen()` reacts to a *closed*
  ServerSocket by looping `accept()` forever (SocketException every iteration,
  thread never interrupted) — 100% CPU, local port left unbound, WS reconnect
  gets connection-refused forever. Only `forwarder.close()` interrupts the
  listen thread. **Fix:** tear down via `forwarder.close()`, tracked in
  `activeForwarder`; `shutdown()` closes it too.
- **P1 — 2s unbound gap.** A single forwarded channel-open failure
  (`ConnectionException`, not caught) exited `listen()`; the old code then
  `ss.close()` + `delay(2000)` + rebind, so the SPA's parallel cold-load
  connections hit a listener-less port. **Fix:** bind ONE ServerSocket per SSH
  session, re-listen on the same socket, no gap.
- **P2 — Nagle.** No `TCP_NODELAY`; streamed tokens coalesced 40–200 ms and
  looked hung. **Fix:** `client.socket.tcpNoDelay = true` on the transport.

Ruled out (don't touch): the 10s SO_TIMEOUT (sshj retries), keepalive 30s×8
(≈4 min, not aggressive), StreamCopier flush, per-connection concurrency, WS
URL, ping/pong.

> Tailscale / Cloudflare Tunnel / terminal-app SSH were investigated and
> **rejected** by the user — the APK is the connectivity layer, optimised.

## 2. Why the old app had so many "deferred" features

It was scoped as a **full mirror of the desktop web** (19 routes). Most desktop
features (xterm, workflow YAML editing, CRUD forms, calendars, file pickers)
have no home on a 375 px touch screen, so they were deferred or degraded to
read-only. "Lots of deferred features" was the structural consequence of the
mirror goal, and it buried the one thing a phone is good at — glancing at and
poking a live session (the old root even redirected to `/missions`).

## 3. The redesign

- **Scope:** Sessions list (interactive + auto only; `chat_agent` dropped) +
  chat-style SessionDetail + a thin Notifications surface that deep-links back
  into a session. 16 other views + their api/stores/components deleted. Sessions
  is now the home route.
- **Sync = one backend.** Mobile is just the `/m/` SPA on the **same uvicorn
  process, same `csm.db`, same EventStream, same JSONL** as the desktop `/`.
  Sending / renaming / interrupting from the phone writes the one PTID/DB, so the
  desktop sees it live, and vice-versa. **Never** run a second backend / DB for
  mobile (that races EventStream and breaks tab sync).
- **Interaction robustness.** The chat WS (`/api/sessions/{sid}/messages`) used
  to close 4500 the instant a fresh session had no `claude_session_id` yet;
  it now **waits (bounded, answering pings)** for reconciliation before it
  gives up. Sends are idempotent via `client_msg_id` (safe tunnel retries).
- **UI — ChatGPT-web-style rendered chat.** `lib/markdown.ts`: markdown-it
  (`html:false`, XSS-safe) + Shiki dual-theme syntax highlighting + DOMPurify.
  Full-width document layout (not bubbles), code blocks with a language label +
  copy button, `--shiki-dark` CSS-var theme switching. Fine-grained Shiki bundle
  (only ~23 grammars) keeps the PWA precache to ~3.5 MB. Vant tokens + the raw
  design tokens in `styles/global.css` are remapped to the desktop's warm
  Notion palette (canvas/card/ink/pastel/accent, dark mode).

## Verification

- APK Kotlin: `gradle :app:compileDebugKotlin --rerun-tasks` → BUILD SUCCESSFUL.
- Backend: `mobile/tests/backend/` incl. new `test_session_ws_waiting.py`.
- Frontend: `vitest` incl. `lib/markdown.spec.ts` (render + XSS + dual-theme).
- Desktop zero-regression: `pytest tests/`.
- Runtime APK check (one full interaction on a real device) is the user's — the
  fix is bytecode-verified but not device-tested in the build env.
