# CSM Mobile — Setup & Troubleshooting

**Target audience**: single-user product (you). You already run CSM on
a workstation and want to reach it from your phone.

## Prerequisites

- CSM checkout at some path (e.g. `~/data/PowerGrandFather`)
- SSH server running on the workstation (usually already the case)
- Phone with an SSH client app installed:
  - **iOS**: [Blink Shell](https://blink.sh/) (paid, industry-standard)
  - **Android**: [Termius](https://termius.com/) (free tier ok),
    [JuiceSSH](https://juicessh.com/), or Termux (`pkg install openssh`)

## Step 1 — Start CSM Mobile on the workstation

As of 2026-08-18 the **main** `scripts/start.sh` auto-mounts the mobile UI
under `/m/` whenever `mobile/frontend/dist` exists, and now defaults to
plain HTTP (the SSH tunnel is the trust boundary — a TLS backend breaks the
WebView's `http://localhost` load). So one process serves both `/` (desktop)
and `/m/` (mobile) off the same `csm.db`:

```bash
./scripts/start.sh 0.0.0.0 8000
# → plain HTTP, desktop at / and mobile at /m/, single csm.db.
# TLS (desktop-only LAN box): CSM_ENABLE_TLS=1 ./scripts/start.sh
```

The standalone wrapper still works if you want mobile on a separate
process/DB, but it is no longer required:

```bash
./mobile/scripts/start_with_mobile.sh
# → binds 0.0.0.0:8000, builds mobile SPA on first run, wires /m/ mount
```

Or with custom bind:

```bash
./mobile/scripts/start_with_mobile.sh 127.0.0.1 8000
```

Runtime files land under `mobile/`:
- `mobile/csm-mobile.pid` — pid for `stop_mobile.sh`
- `mobile/csm-mobile.log` — uvicorn output

Stop with:

```bash
./mobile/scripts/stop_mobile.sh
```

## Step 2 — Establish the SSH tunnel from your phone

### iOS · Blink Shell

1. `config` → **Hosts** → **+** → set alias `ws`, hostname/user, upload
   your private key.
2. Open a shell tab and run:
   ```
   ssh -N -L 8000:localhost:8000 ws
   ```
   The `-N` flag means "no remote command"; the tab must stay open.
3. Blink keeps the tunnel alive as long as the tab is visible. If you
   background Blink, the tunnel dies — swipe back to it to reconnect.

### Android · Termius

1. Add a host with your SSH credentials.
2. In the host detail: **Port Forwarding** → **Local** → local port
   `8000`, remote host `localhost`, remote port `8000`.
3. Connect. Termius keeps the tunnel in the background.

### Android · Termux

```bash
pkg install openssh
ssh -N -L 8000:localhost:8000 user@workstation-ip-or-alias
```

Optional: add to `~/.ssh/config`:

```
Host ws
  HostName workstation.local
  User owner
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
```

## Step 3 — Open in phone browser

```
http://localhost:8000/m/
```

- If backend has `settings.access_token` set, first visit needs
  `?token=YOUR_TOKEN`. The cookie is written after the first
  successful load, so subsequent visits work without the query.
- Add to home screen for a full-screen "app-like" experience:
  - **iOS Safari**: Share → **Add to Home Screen**
  - **Android Chrome**: menu → **Install app** (or Add to Home Screen)

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| WebView shows nothing / spinner forever | SSH tunnel not established or died | Re-run the `ssh -N -L …` command; check `ServerAliveInterval` in `~/.ssh/config` |
| Opening `/m/` returns the desktop app (not mobile) or 404 | Backend started before the auto-mount change, or from a dir where `mobile/` isn't importable | Restart `scripts/start.sh` from the repo root; confirm `curl -s localhost:8000/m/ \| grep title` shows `CSM Mobile` |
| WebView blank / handshake garbage | Backend running with TLS; WebView loads plain `http://` | Restart without TLS (default now) — do NOT set `CSM_ENABLE_TLS=1` when using the phone |
| Opening `/m/` returns 404 even with wrapper | Mobile SPA not built | `cd mobile/frontend && npm install && npm run build` (or delete `mobile/frontend/dist/index.html` and let wrapper rebuild) |
| POST / DELETE requests return 400 | `X-CSM-Client` header stripped somewhere | Hard-refresh the page (axios interceptor should re-inject) |
| Toast: `Access token invalid` | `settings.access_token` set, cookie expired | Append `?token=YOUR_TOKEN` once to the URL |
| WebSocket keeps disconnecting | SSH keepalive too aggressive on tunnel | Add `ServerAliveInterval 30` to `~/.ssh/config`; also check that phone's WiFi is stable |
| Codex TUI session shows a "please use desktop" card | By design — TUI cannot be rendered on mobile | Open the desktop client for that session |
| Chat/session message flow doesn't scroll | Rare jsdom fallback used in prod | Report as a bug in Feedback → attach browser + version |
| "Port 8000 already in use" from wrapper | Another process holds 8000 | `lsof -iTCP:8000 -sTCP:LISTEN` → kill it or use a different port |

## Uninstall / rollback

Everything mobile lives in `mobile/`. Delete the directory and pull main
repo — no residue.

```bash
rm -rf mobile/frontend/dist mobile/frontend/node_modules mobile/csm-mobile.*
```

The main repo `backend/` / `frontend/` / `scripts/` / `tests/` are
untouched — running `scripts/start.sh` returns you to the pure desktop
experience.
