# mobile/scripts/

Startup wrapper + helper scripts. **Populated in Phase 1**.

## start_with_mobile.sh

Zero-touch wrapper that:
1. Auto-builds `mobile/frontend/dist/` if missing (unless `CSM_SKIP_FRONTEND_BUILD=1`)
2. Launches uvicorn against a Python factory that imports `csm.main:app` then calls `mobile.backend_patch.register(app)` to attach `/m/`
3. Passes `--ws-ping-interval 30 --ws-ping-timeout 10` to mitigate SSH-tunnel WS zombie
4. Writes `csm.pid` + `csm.log` (same convention as main `scripts/start.sh`)

Usage:
```bash
./mobile/scripts/start_with_mobile.sh                 # 0.0.0.0:8000
./mobile/scripts/start_with_mobile.sh 127.0.0.1 8000  # custom bind
CSM_SKIP_FRONTEND_BUILD=1 ./mobile/scripts/start_with_mobile.sh
```

**Not enabling mobile? Just run `./scripts/start.sh` from the main repo — nothing changes.**

## ssh_tunnel_hint.md

Cheat sheet for setting up the phone-side SSH tunnel:
- iOS Blink Shell
- Android Termius / JuiceSSH / Termux
- Access URL after tunnel is up: `http://localhost:8000/m/`

## Non-goals

- No systemd unit files (single-user local console)
- No Docker (see ADR-0002)
- No CI scripts (project already has its own CI)
