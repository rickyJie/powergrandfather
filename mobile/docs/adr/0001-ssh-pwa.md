# ADR-0001: SSH port forwarding + PWA in mobile browser

- **Status**: Accepted (2026-08-14)
- **Deciders**: llj (single-user product)
- **Supersedes**: —

## Context

CSM (PowerGrandFather) is a **single-user local developer console** for
managing Claude Code sessions, workflow missions, tokens, ports, etc.
Users have asked for a way to check on and interact with CSM from their
phone. Four candidate approaches were considered:

1. **Public HTTPS + JWT/OAuth** — expose CSM on the public internet with
   proper authentication.
2. **SSH port forwarding + browser** — user SSHes from phone to
   workstation, forwards port 8000, opens `http://localhost:8000/m/` on
   phone browser.
3. **Native Android APK** — package a WebView + Foreground SSH tunnel
   service into an installable Android app.
4. **Capacitor / Tauri Mobile** — wrap web code as a native shell.

## Decision

**SSH port forwarding (approach 2) + a Progressive Web App at `/m/` for
the mobile UI.**

## Rationale

- **Zero attack surface**: Single-user product has no reason to be on the
  public internet. SSH already provides transport encryption and
  identity — no need to re-invent auth in the app.
- **Zero backend auth work**: The existing `X-CSM-Client` middleware +
  optional access-token cookie flow is sufficient because the trust
  boundary is the SSH tunnel itself, not the HTTP layer.
- **PWA over native**: Approximately 1/10th the engineering effort of a
  full native app. iOS and Android both support "add to home screen"
  giving a near-native experience without app store hurdles.
- **Avoids MIUI/ColorOS Foreground Service kill**: Product-manager and
  QA reviewers flagged this as a hard blocker for a native APK path —
  Chinese Android ROMs aggressively kill background SSH tunnels. Punting
  to SSH-in-terminal-app sidesteps the issue.

## Consequences

- Users must install an SSH client app on their phone (**iOS**: Blink
  Shell; **Android**: Termius, JuiceSSH, or Termux).
- No native push notifications on iOS. Deferred to Phase N; existing
  Lark push channel remains the escape hatch.
- Native APK is deferred, not permanently ruled out — if the SSH tunnel
  UX proves too clunky in practice, a follow-up ADR can revisit.
- Public HTTPS remains explicitly off-limits until an auth layer exists.

## Alternatives considered

| Option | Why not |
|---|---|
| Public HTTPS + JWT | Requires implementing auth; enlarges attack surface for zero product benefit in a single-user tool. |
| Native Android APK | 4-8 weeks of work; Chinese OEM battery optimizers kill background services; iOS not covered. |
| Capacitor / Tauri Mobile | Buys us little over PWA; still requires app-store distribution or side-load. |
