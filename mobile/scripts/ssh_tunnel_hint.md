# SSH tunnel cheat sheet (mobile access)

Full setup guide will land in `mobile/docs/setup.md` at Phase 5. This is a
minimal "just make it work" reference during Phase 1-4 development.

## 前提

Workstation 上跑：
```bash
./mobile/scripts/start_with_mobile.sh
```
（bind `0.0.0.0:8000` by default. `mobile/frontend/dist/` will auto-build on
first run — takes ~30s for `npm install` + build.）

## 手机端建立 tunnel

### iOS — Blink Shell
1. Blink 里 `config` → Hosts → Add：填 workstation host / user / 上传 SSH private key
2. 输入命令：
   ```
   ssh -N -L 8000:localhost:8000 workstation-alias
   ```
3. Blink 保持前台运行（切后台会断），或用 mosh 保活

### Android — Termius
1. Hosts → Add：同 iOS
2. Host 详情 → Port Forwarding → Local Forward：`8000 → localhost:8000`
3. Connect — tunnel 后台维持

### Android — Termux
```bash
pkg install openssh
ssh -N -L 8000:localhost:8000 user@workstation
```

## 手机浏览器打开

```
http://localhost:8000/m/
```

首次可能需要 `?token=XXX` 若启用了 `settings.access_token`。之后 cookie 自动保存。

## 常见问题

| 症状 | 排查 |
|---|---|
| WebView 白屏 | Tunnel 未建立；重新 SSH |
| `/m/` 返回 404 | 用了 `scripts/start.sh` 而不是 `mobile/scripts/start_with_mobile.sh` |
| `/m/` 返回 404 (wrapper 已跑) | `mobile/frontend/dist/index.html` 不存在 → `(cd mobile/frontend && npm run build)` |
| WS 频繁断连 | 加 `ServerAliveInterval 30` 到 `~/.ssh/config` |
| 手机切前后台断连 | WS 会自动重连（≤ 30s 指数退避），或下拉刷新 |
