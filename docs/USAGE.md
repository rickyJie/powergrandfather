# USAGE — PowerGrandFather (CSM) 日常使用速查

> 与 [README.md](../README.md)（PRD 取向）不同，本文件是 **跑过的命令清单**。上次 E2E full-run：2026-06-21；本次 last-reviewed（静态审查 + endpoint 校对，未跑 backend）：**2026-07-25**。
>
> 注意：M4 TaskDefinition（`/api/tasks/*`）和 v1 AlertRule（`/api/tokens/alert-rules/*`）已在 2026-07-06 / 2026-07-10 分别下线。旧文档里的 curl 已按 P2 / P5 迁移替换，见下面的 M4 Automation / M5 Tokens 章节。

## Daily Loop（最短路径）

1. **启动 CSM**：`./scripts/start.sh`（默认 `0.0.0.0:8000` — 见「非明显坑」）
2. **开一个 session**：浏览器 → Sessions → `+ New Session` → 选 project → attach
3. **触发 workflow**：Automation → 选 workflow → `Run` → auto session 自动 attach
4. **看 token 用量**：Tokens 页 — live probe + burn rate + agent-alert 状态
5. **收通知**：右上角 badge / in-app panel（Lark 可选）

详情见下方分模块 curl 示例。

> **每条 `/api/*` 请求都必须带 `-H 'X-CSM-Client: 1'`。** 这是 CSM 的 CSRF
> 防御（`RequireClientHeaderMiddleware`）：浏览器的表单式跨站 POST 无法设置
> 自定义头，所以缺这个头的请求一律 400 `missing X-CSM-Client: 1 header`。
> 少数几个前缀豁免，因为它们由 `window.open` / `<img src>` / EventSource
> 发起，浏览器不允许这些携带自定义头：`/api/hooks/*`、`/api/metrics`、
> `/api/events/stream`、`/api/files/{preview,raw,inline}`。下面的示例一律带上
> 这个头，包括豁免的那些——多带无害，少带就是 400。**注意所有 API 只从 loopback (127.0.0.1) 响应**，`/api/hooks/*` 还额外做 Host-header 白名单防 DNS rebinding（见 `backend/csm/api/_deps.py::_require_loopback_and_host`）。

## 0. 环境

```bash
# 一次性
conda activate csm                    # python 3.11
# 内部 devpi 缺 pytest-asyncio / mypy 等公共 dev 包 —— 加清华源兜底
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
alembic upgrade head                  # SQLite 迁移
cd frontend && npm install && npm run build && cd ..   # 已 build 过可跳过
```

## 1. 启动 / 停止

### Prod 模式（单 uvicorn，serves API + 前端 dist）

```bash
./scripts/start.sh                                 # 默认 0.0.0.0:8000（LAN-reachable）
./scripts/start.sh 127.0.0.1 8001                  # 自定义 host + port
./scripts/stop.sh                                  # SIGINT → SIGTERM → SIGKILL
```

浏览器开 <http://localhost:8000>。

### Dev 模式（vite hot-reload + uvicorn --reload）

```bash
./scripts/dev.sh
```

backend 在 :8000，frontend dev 在 :5173（vite 把 `/api/*` 反代到 backend）。

### 手动启动（调试 / 改 env）

```bash
# 测试模式 — AUTO sessions 用 bash 而不是 claude（避免真消耗 token）
CSM_PORT=8001 CSM_CLAUDE_ARGV='bash -i' \
  python -m uvicorn csm.main:app --host 127.0.0.1 --port 8001
```

完整 env 见 README "Configuration" 表。

## 2. 模块常用动作

### M1 Sessions（PTY + WS）

```bash
# 创建一个 session。argv[0] 现在强制为 "claude"（slot 2 C2 安全 gate）；
# 需要跑 bash / 其他二进制时，必须 export CSM_ALLOW_ARBITRARY_ARGV=1。
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"cwd":"/tmp","type":"interactive","title":"my-session"}'

# 开发覆盖 argv（dev/test 才允许）
CSM_ALLOW_ARBITRARY_ARGV=1 \
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"cwd":"/tmp","type":"interactive","title":"bash","argv":["bash","-i"]}'

# 列出
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/sessions | jq

# WS attach（前端用 xterm）：
#   ws://host:8000/api/sessions/{sid}/ws
#   - 服务端发：binary，PTY 输出（含 1 MB ring buffer 回放）
#   - 客户端发：binary，写入 PTY stdin

# 优雅停（最长 15s — SIGINT 5s → SIGTERM 5s → SIGKILL 5s）
curl -H 'X-CSM-Client: 1' -X DELETE 'http://127.0.0.1:8000/api/sessions/{sid}?graceful=true'

# 强杀（立即 SIGKILL）
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/sessions/{sid}/kill

# 手动 reap 掉 PID 已死的 zombie 行
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/sessions/reap-stale
```

**注意**：DELETE 阻塞最长 15s；HTTP client timeout 设 `>= 20s`。

### Hooks（Claude Code → CSM 回调）

`POST /api/hooks/{sid}` 是 spawned claude 通过 `--settings` 注入的回调入口。仅接受 **loopback + Host header 白名单**（`127.0.0.1` / `localhost` / `[::1]` / 显式 `settings.host`），任何外部/伪造 Host 直接 403；见 `backend/csm/api/_deps.py`。日常无需手工调用。

### M2 Event Stream（Claude JSONL tail → 派生事件）

```bash
# SSE stream — 前端订阅入口，也可拿来 debug（curl 会一直挂）
curl -H 'X-CSM-Client: 1' -N http://127.0.0.1:8000/api/events/stream
```

服务端每 5s 扫一次 `~/.claude/projects/**/*.jsonl`，派生 11 种事件。订阅者（NotificationBus、TokenAggregator、AutomationRunner、SupervisorAgent）实时收到。**EventStream 不持久化**，需要 replay 的模块必须自己落盘。

### M3 Notifications

```bash
# 看未读 badge
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/notifications/unread-summary
# {"total_unread": 8}

# 列通知
curl -H 'X-CSM-Client: 1' 'http://127.0.0.1:8000/api/notifications?limit=20' | jq

# 单条标已读
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/notifications/{nid}/read

# session 级一键清（推荐）
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/notifications/mark-session-read/{csm_session_id}

# 关闭（隐藏 + 视为已读）
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/notifications/{nid}/dismiss

# 全清
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/notifications/clear-all
```

InApp WS：`ws://host:8000/api/notifications/ws` — 仅服务端推送，客户端不发数据。

### Automation（workflows + missions；P2 后取代 M4 tasks）

> 2026-07-06 P2 retire：`/api/tasks/*` 全部下线，`task_def_id` 字段已从 schedule / run 表 drop。旧文档里的 `POST /api/tasks/reload` / `POST /api/runs/launch` **不再存在**，请改用下面的 workflow + mission 端点。

```bash
# 1) 写 YAML：tasks/*.workflow.yaml（schema 见 docs/workflow_authoring_guide.md
#    或让 agent 用 POST /api/workflows/generate 自动生成）

# 2) reload（磁盘上改完 YAML 后调用）
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/workflows/reload

# 3) 列 workflow
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/workflows | jq '.items[].name'

# 4) 手动 launch 一个 mission
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/missions/launch \
  -H 'Content-Type: application/json' \
  -d '{"workflow_name":"my_workflow","params":{}}'

# 5) 看 mission 状态
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/missions/{mission_id} | jq

# 6) 取消 / 重跑
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/missions/{mission_id}/cancel
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/missions/{mission_id}/retry

# 7) 排程（cron 表达式；schedule 现在挂 workflow 不挂 task）
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/schedules \
  -H 'Content-Type: application/json' \
  -d '{"workflow_name":"my_workflow","cron":"0 22 * * *","enabled":true,"params":{}}'   # unverified, code review only 2026-07-25

# 8) stage 级执行记录（旧 `run` 表改名为 `stage_execution`）
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/runs | jq
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/runs/{run_id} | jq
```

**Mission status enum**（小写）：`pending` / `running` / `succeeded` / `failed` / `cancelled`。

### M5 Tokens

```bash
# 当前 5h 窗口
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/tokens/current | jq

# 24h 趋势（hourly buckets）
curl -H 'X-CSM-Client: 1' 'http://127.0.0.1:8000/api/tokens/history?hours=24&granularity=hour' | jq

# top consumers（by cache_creation_tokens）
curl -H 'X-CSM-Client: 1' 'http://127.0.0.1:8000/api/tokens/top?scope=session&hours=5&limit=10' | jq

# 历史 rate-limit hit 记录
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/tokens/hit-observations | jq
```

#### Agent-authored alerts（2026-07-10 起替换旧 `/api/tokens/alert-rules/*`）

v1 `AlertRule` / hard-coded threshold check 已删除。新流程是「NL 描述 → agent 生成 Python check 脚本 → dry-run → commit」两步走。旧路径的 curl 会 404。

```bash
# Step 1: describe rule in NL, get agent-generated Python check + dry-run.
# Nothing is persisted yet.
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/tokens/agent-alerts/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"5h_msg_cap",
    "nl_description":"fire when 5h window message count exceeds 800",
    "threshold_spec":{"metric":"msg_count","op":">=","value":800},
    "escalate": false
  }' | jq

# Response has `script` (Python source), `dry_run` (would it fire now?), and
# `window_snapshot`. Review it before committing.

# Step 2: commit the previewed script.
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/tokens/agent-alerts \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"5h_msg_cap",
    "nl_description":"fire when 5h window message count exceeds 800",
    "threshold_spec":{"metric":"msg_count","op":">=","value":800},
    "check_script":"def check(window):\n    fired = window[\"msg_count\"] >= 800\n    return (fired, {\"metric\":\"msg_count\",\"actual\":window[\"msg_count\"],\"threshold\":800})\n",
    "poll_interval_sec": 60,
    "cooldown_sec": 300,
    "channels": ["inapp", "lark"],
    "escalate": true,
    "lark_chat_id": "oc_xxx"
  }' | jq

# List / toggle / snooze / delete
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/tokens/agent-alerts | jq
curl -H 'X-CSM-Client: 1' -X PATCH http://127.0.0.1:8000/api/tokens/agent-alerts/<id> \
  -H 'Content-Type: application/json' -d '{"enabled": false}'
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/tokens/agent-alerts/<id>/snooze \
  -H 'Content-Type: application/json' -d '{"minutes": 60}'   # unverified, code review only 2026-07-25
curl -H 'X-CSM-Client: 1' -X DELETE http://127.0.0.1:8000/api/tokens/agent-alerts/<id>

# Presets + dry-run simulate
curl -H 'X-CSM-Client: 1' http://127.0.0.1:8000/api/tokens/agent-alerts/presets | jq
curl -H 'X-CSM-Client: 1' -X POST http://127.0.0.1:8000/api/tokens/agent-alerts/<id>/simulate | jq   # unverified, code review only 2026-07-25
```

**Channels**：`inapp`（always writes DB row + WS push）+ `lark`（只有列表里含 `lark` 才触发）。`escalate: true` 触发时会再起一次 `claude -p` 做 root-cause + recommendations（见 `backend/csm/modules/token/agent_alert/escalate.py`）。

**编辑规则**：PATCH 只接受 operational 字段（enabled / poll_interval_sec / cooldown_sec / channels / escalate / lark ids）。改 check 逻辑或 threshold 请 delete + regenerate，避免 script 与 spec 漂移。

### Files（session artifacts + OSS redirect）

```bash
# Session 里 agent 最近碰过的文件（前端 FileTouches 面板用）
curl -H 'X-CSM-Client: 1' 'http://127.0.0.1:8000/api/files/recent/{sid}?limit=50' | jq

# HTML 预览（markdown 双 view，代码 pygments 高亮，图片 inline）
open 'http://127.0.0.1:8000/api/files/preview?path=/abs/path/to/file.md'

# 原始下载
curl -H 'X-CSM-Client: 1' -OJ 'http://127.0.0.1:8000/api/files/raw?path=/abs/path/to/file.log'

# s3:// → oss_base_url 302 redirect（需要 settings.oss_base_url 有值）
curl -H 'X-CSM-Client: 1' -I 'http://127.0.0.1:8000/api/files/oss-redirect?uri=s3://example-bucket/foo.md'
```

## 3. 跑 E2E 自检（验证安装）

```bash
source $CONDA_PREFIX/bin/activate csm
python -m pytest tests/ -q                    # 全量 unit + integration

# 单模块 pytest
pytest tests/unit/test_event_stream.py
pytest -k "token and not rollup"

# 老 E2E 脚本（/tmp/csm_t*_e2e.py）绝大多数依赖 M4 tasks 端点，2026-07-06 起
# 已失效。真要跑 E2E 请用 tests/integration 或让 supervisor / workflow 触发。
```

## 4. 非明显坑（开发期常踩）

- **默认 bind 是 `0.0.0.0:8000`**（LAN 可达）；`POST /api/sessions` 允许 argv 覆盖 → LAN 内任何人都能 spawn 进程。不要在不受信任网络上跑。argv[0] 已强制为 `claude`，其它二进制要 `CSM_ALLOW_ARBITRARY_ARGV=1`。
- **`/api/hooks/{sid}` 做 Host header 白名单**（防 DNS rebinding），任何外部 Host 直接 403。
- **DELETE `/api/sessions/{sid}` 阻塞最长 15s** → HTTP client timeout 调 `>= 20s`。
- **API datetime 没 timezone**：看到 `2026-07-25T06:14:37` 默认当 UTC。
- **测试 / 开发请用 `CSM_CLAUDE_ARGV='bash -i'`**，否则 auto session launch 会真去 spawn `claude` 消耗 quota。
- **`/api/tasks/*` 已 404**（M4 retire）；`/api/tokens/alert-rules/*` 已 404（v1 alerts retire）。任何还在调用这些的老脚本要更新。
- **EventStream 不持久化** — 需要 replay 的消费者要自己写库。

更详细的 v2 路线 / 已知问题：`docs/known_issues.md`。

## 5. 文件清单（哪里改什么）

| 想改 | 改这里 |
|---|---|
| 加新自动化 workflow | `tasks/*.workflow.yaml` → POST `/api/workflows/reload` |
| 改前端 UI | `frontend/src/views/*.vue` → `npm run build` |
| 改后端业务 | `backend/csm/modules/{session_manager,automation,workflow,token,ports,agent,supervisor}/` |
| 改派生事件类型 | `backend/csm/core/events.py` + `event_stream.py` |
| 改通知路由 | `backend/csm/core/notification_bus.py: _route()` |
| 改全局配置 | `backend/csm/config.py` + 设 `CSM_*` env |
| 加 alembic migration | `alembic revision -m "..."` → 编辑 `alembic/versions/` |
| Workflow 生成 prompt / R9-R19 review | `backend/csm/modules/workflow/authoring/` |
| Agent alert check 脚本生成 | `backend/csm/modules/token/agent_alert/` |
