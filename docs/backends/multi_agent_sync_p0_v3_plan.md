# CSM 多 Agent 同步 · P0 v3 · 执行计划

> Scheduled task 2026-07-27 03:02 已错过窗口（Claude 未运行）· 用户切 session 手动执行
> 本文保留原 prompt 全部决策 · 下一 session 直接读本文启动，无需恢复对话历史

## Metadata

| 字段 | 值 |
|---|---|
| Worktree | `<CSM_REPO>-codex` |
| Branch | `llj_dev_codex`（直接 commit · 不开 feature branch） |
| 原窗口 | 03:00-10:00 · 7h · 用户明确不等 sign off · 每 Phase 一个原子 commit |
| 关联 review | `/tmp/backend-review-multi-cli-sync-2026-07-26-153951.md`<br>`/tmp/pm-review-multi-cli-sync-2026-07-26-152202.md` |

## Phase 列表

| Phase | 时长 | 交付物 | Commit |
|---|---|---|---|
| 0.1 | 1h | B1-B5 code pattern 撰写 | — |
| 0.2 | 1h | 数据模型 6 表 SQL DDL + API 契约 JSON schema | — |
| 0.3 | 1h | Adapter Protocol 10 个新方法签名 + docstring | — |
| 0.4 | 1h | 汇总落盘 `docs/backends/multi_agent_sync_spec.md`（P0 结束） | ✅ |
| 1.1-1.2 | 2h | Alembic revision + 6 models 落地 · `alembic upgrade head` 无错 | ✅ |
| 1.3 | 1h | Adapter Protocol 代码扩展 + ClaudeAdapter/CodexAdapter 空 stub | ✅ |

**10:00 收尾** · Phase 2-5 明天继续

## B1-B5 决策（backend review 敲实）

- **B1 · 并发写守卫**：write-hash-compare —— read hash → atomic write（tmp+rename）→ re-read hash → compare 与 expected；不一致记 drift 跳过 sync
- **B2 · partial success response schema**：
  ```
  {
    "data": <resource>,
    "sync": [{"agent", "status", "detail"}],
    "warnings": [...]
  }
  ```
  DB commit ok 即 HTTP 200
- **B3 · subprocess wrapper**：
  - `asyncio.create_subprocess_exec`（**永不 `shell=True`**）
  - `timeout=10s` · 只看 `returncode` · `stderr` 只记不解析
  - 启动时 `<cli> mcp --help` 探测能力
- **B4 · schema**：
  - `drift_record` 加 `resource_type` enum(`instruction` | `mcp_server` | `skill`) + `resource_id: int`（不用多态外键）
  - `sync_config.module` 加 `UniqueConstraint`
- **B5 · `${VAR}` 展开**：只走 subprocess env dict · 不进 argv；未定义变量抛 `SyncPreflightError`

## 数据模型 6 表

`sync_config` · `instruction` · `mcp_server` · `skill` · `sync_activity` · `drift_record`

> 详细字段见 v3 plan 对话历史（原 prompt 未展开列出）· Phase 0.2 会补全完整 DDL

## CLIAdapter Protocol 新增

- `memory_paths(scope)` · `read_memory(path)` · `write_memory_marker_block(path, content)`
- `async mcp_add(...)` · `async mcp_remove(...)` · `async mcp_list()`
- `skills_dir()` · `list_skills()` · `write_simple_skill(spec)` · `remove_skill(name)`
- `marker_syntax()` · `async probe_sync_capabilities()`
- 新增 `Capability.SYNC_MEMORY` / `Capability.SYNC_MCP` / `Capability.SYNC_SKILLS`

## 开工步骤（下一 session 直接执行）

1. `TaskCreate` 6 个 Phase 任务 · Phase 0.1 mark `in_progress`
2. `cd <CSM_REPO>-codex && git status && git branch --show-current` 确认在 `llj_dev_codex`
3. Phase 0.1 起步：新建 `docs/backends/multi_agent_sync_spec.md` 骨架，先写 B1-B5 code pattern 五节

## 状态勾选（session 内滚动更新）

- [ ] Phase 0.1 · B1-B5 code pattern
- [ ] Phase 0.2 · 6 表 DDL + API 契约
- [ ] Phase 0.3 · Adapter Protocol 签名
- [ ] Phase 0.4 · 汇总 spec 落盘（**commit**）
- [ ] Phase 1.1-1.2 · Alembic + models（**commit**）
- [ ] Phase 1.3 · Protocol 代码 + adapter stub（**commit**）
- [ ] Phase 2-5 · 明天继续
