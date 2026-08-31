# CSM Mobile 端

**分支**：`llj_mobile`
**独立目录**：全部移动端代码 / 测试 / 脚本 / 文档集中在此。主 repo (`backend/`, `frontend/`, `scripts/`, `tests/`, `docs/`) **零改动**。

## 目录结构

```
mobile/
├── README.md                       # 本文件
├── frontend/                       # Vue 3 移动端 SPA（Vite + Vant 4）
├── backend_patch/                  # FastAPI mount patch（/m/ + WS ping）
│   ├── __init__.py
│   ├── mount.py                    # register(app) 挂载 /m/ + SPA fallback
│   └── README.md
├── tests/
│   ├── backend/                    # pytest 移动端后端 patch 相关（隔离目录）
│   └── frontend/                   # vitest 移动端组件单测
├── scripts/
│   ├── start_with_mobile.sh        # 启动 wrapper（拉起 csm.main:app + 注册 mobile mount）
│   └── ssh_tunnel_hint.md
└── docs/
    └── README.md                   # 文档索引
```

宏观设计（跨 phase）：**[`docs/mobile_design.md`](../docs/mobile_design.md)**（保留在主 repo `docs/` 便于跟其他 design docs 一处发现）。

## 启动方式

**主 repo 零改动方案**：

```bash
# 常规桌面端启动（不影响）
./scripts/start.sh

# 启用移动端 mount 的启动
./mobile/scripts/start_with_mobile.sh
```

`start_with_mobile.sh` 内部导入 `csm.main:app`，调用 `mobile.backend_patch.register(app)` 挂载 `/m/`，再交给 uvicorn 启动。**桌面端 `/` 行为不变**；不跑 wrapper 则移动端不 mount。

## 关键原则

1. **主 repo 零改动**：`backend/csm/main.py` / `frontend/*` / `scripts/*` / `tests/*` / `docs/*` 一行都不改
2. **桌面端零回归**：不跑 wrapper 时移动端相关代码不加载
3. **隔离测试目录**：`mobile/tests/` 独立 pytest / vitest，不动主 repo `tests/`
4. **无 xterm**：移动端所有 session/chat 走消息流 UI
5. **无 push**：Phase 6 只做 PWA 静态强化

## Known limitations

- **No xterm**: PTY terminal interaction (raw stdin/stdout with cursor
  positioning) is not rendered on mobile. Use the desktop client for
  interactive `claude` sessions and any codex TUI. Mobile shows a
  "please use desktop" card for TUI sessions.
- **No iOS native app**: iOS users tunnel via [Blink Shell](https://blink.sh/).
- **No native push**: notifications are polled (60s) + delivered via
  WebSocket when the tab is open. If you close the browser, you won't
  get pinged. Existing Lark webhook still works if configured.
- **Android background quirks**: Termux/Termius may kill the SSH tunnel
  in the background on aggressive Chinese ROMs (MIUI/ColorOS). Keep
  the app in foreground while using CSM.
- **WebSocket reconnect delay ≤ 30s**: after backgrounding the phone,
  WS will reconnect on visibility change; brief message gaps possible.
- **Workflow YAML editing disabled**: mobile shows workflow list +
  Launch only. Edit on desktop.
- **Preferences / Lark settings read-only**: mobile displays but does
  not edit. This may relax if the backend gains PATCH endpoints later.
- **Concurrent writes**: mobile + desktop can simultaneously send
  messages / cancel missions. Message-level append semantics prevent
  byte-level races; mission cancel is idempotent (second returns 4xx).

See `mobile/docs/setup.md` for full setup + troubleshooting.
