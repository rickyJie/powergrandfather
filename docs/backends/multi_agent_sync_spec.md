# CSM 多 Agent 同步 · P0 v3 · 实现规格

> Spec 定稿于 P0（Phase 0.1-0.4）· Phase 1 起按本文实现
> 关联决策：`docs/backends/multi_agent_sync_p0_v3_plan.md`
> Review 来源：
> - `/tmp/backend-review-multi-cli-sync-2026-07-26-153951.md`（backend）
> - `/tmp/pm-review-multi-cli-sync-2026-07-26-152202.md`（product）

## 0. 目的与范围

本子系统在 CSM 内部维护三类"多 agent 共享配置"的**单一事实源（single source of truth）**，
并把每份资源双写到每个 enrolled agent 的原生配置文件（`~/.claude/*` / `~/.codex/*`）：

| 模块 | 资源 | 落到 claude 侧 | 落到 codex 侧 |
|---|---|---|---|
| `memory` | Instruction 段（marker block 包裹） | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` |
| `mcp` | MCP Server 定义 | `claude mcp add` CLI | `codex mcp add` CLI |
| `skills` | Skill 目录（`SKILL.md` + 资源） | `~/.claude/skills/<name>/` | `~/.codex/skills/<name>/`（若支持） |

**非目标**：
- 不做 agent 到 agent 的 P2P 同步（永远通过 CSM 中转）
- 不做冲突合并 UI（drift 只标记 + 通知，不自动 merge）
- 不做跨机器同步（单机 workspace）

---

## 1. B1 · 并发写守卫：write-hash-compare

### 问题

Claude REPL 用 Node.js 的 `write tmp → rename over` 原子写 `~/.claude.json` 等文件。
Python `fcntl.flock` 对 Node.js 的 rename **不构成互斥**（rename 会替换 inode），CSM 侧的
锁只能保护 CSM 内部多写路径的互斥。CSM 和 REPL 之间的 TOCTOU 不可避免。

### 决策

- 用 `python-filelock` 只管 CSM 内部互斥；
- 用 **write-hash-compare** 检测 REPL 并发覆盖：
  1. read 目标文件 → sha256 `expected_before`
  2. atomic write（tmp + rename）新内容
  3. re-read 目标文件 → sha256 `actual_after`
  4. 期望 `actual_after == sha256(new_content)`；若不一致 → 记 `drift_record`、跳过 sync、返 warning。
- **不用 mtime**：多数 FS 上 mtime 精度是 1s，不可靠。

### Code pattern

```python
# backend/csm/modules/sync/atomic_write.py
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from filelock import FileLock

from csm.modules.sync.errors import ConcurrentWriteDrift


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _read_or_empty(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def atomic_write_with_hash_guard(
    path: Path,
    new_content: bytes,
    *,
    lock_path: Path | None = None,
    lock_timeout: float = 5.0,
) -> None:
    """Atomic write + post-write hash verification.

    Raises `ConcurrentWriteDrift` when a concurrent writer (typically the
    claude REPL) overwrote the file between our write and our verify read.
    Caller should log a `drift_record` and skip the current sync attempt —
    do NOT retry silently (that hides real drift).
    """
    lock = FileLock(str(lock_path or path.with_suffix(path.suffix + ".lock")),
                    timeout=lock_timeout)
    expected_hash = _sha256_bytes(new_content)

    with lock:
        # 1. read pre-hash (for drift diagnostics only; not used to gate write)
        pre_hash = _sha256_bytes(_read_or_empty(path))

        # 2. atomic write via tmp + rename in the same directory
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".csm_sync_",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)  # atomic on POSIX; overwrites
        except Exception:
            # cleanup tmp on failure
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

        # 3. verify: re-read and compare
        actual_hash = _sha256_bytes(_read_or_empty(path))
        if actual_hash != expected_hash:
            raise ConcurrentWriteDrift(
                path=path,
                expected_hash=expected_hash,
                actual_hash=actual_hash,
                pre_hash=pre_hash,
            )
```

### 调用侧契约

- 每次 `SyncService.push_to_agent(agent, module)` 内部调用一次；
- 捕获 `ConcurrentWriteDrift` → `DriftRecorder.record(resource_type, resource_id, agent, reason="concurrent_write")` → 返 `SyncStatus.SKIPPED`；
- **不重试**：drift poll worker 30s 内会再来一轮。

---

## 2. B2 · Partial Success Response Schema

### 问题

一个 CRUD 请求可能触发多个 agent 的 sync；某个 agent CLI timeout 时是 5xx 还是 200？
- 5xx：客户端认为服务器错误，DB 状态也可能被回滚；
- 200：客户端认为全成功，实际部分失败 → silent partial failure。

### 决策

- **DB commit ok 即 HTTP 200**；
- Response body 显式列 per-agent sync 结果；
- Sync 失败**不是**服务器错误，是业务上的"部分完成"。

### Schema

```json
{
  "data": <resource_json>,
  "sync": [
    {"agent": "claude", "status": "ok",     "detail": null},
    {"agent": "codex",  "status": "timeout","detail": "codex mcp add exceeded 10s"}
  ],
  "warnings": [
    "codex: sync timed out; drift poll will retry"
  ]
}
```

`status` 枚举：`ok` · `timeout` · `unsupported` · `skipped` · `error`

- `ok`：CLI returncode == 0；
- `timeout`：subprocess timeout（10s）；
- `unsupported`：adapter capability probe 表明该模块不支持（e.g. codex 不支持 skills 时）；
- `skipped`：drift 检测跳过（B1 触发）；
- `error`：CLI returncode != 0，`detail` 存 stderr 首行。

### Pydantic 模型

```python
# backend/csm/api/schemas/sync.py
from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel


class SyncStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"
    ERROR = "error"


class PerAgentSyncResult(BaseModel):
    agent: str            # "claude" | "codex" | future
    status: SyncStatus
    detail: str | None = None


T = TypeVar("T", bound=BaseModel)


class SyncEnvelope(BaseModel, Generic[T]):
    data: T
    sync: list[PerAgentSyncResult]
    warnings: list[str] = []
```

### 幂等性要求

CLI add 操作**必须幂等**——同一份 `csm-*` 前缀的 MCP entry 重复 add 不能报错。
每个 adapter 需覆盖测试：连续 `mcp_add(server)` 两次，第二次 status 仍返 ok。

---

## 3. B3 · Subprocess Wrapper 标准化

### 决策

- `asyncio.create_subprocess_exec(*argv, ...)`；**永不 `shell=True`**。
- `timeout=10s`；本地 CLI 调用不该超过这个。
- 只看 `returncode`：`0 → ok`，`!=0 → error`。
- `stderr` 全量写 `sync_activity.detail_json.stderr`；**不解析自然语言**。
- 启动时探测：`<cli> mcp --help`（returncode 判可用性），结果缓存到 `AdapterStatus.capabilities`。

### Code pattern

```python
# backend/csm/modules/sync/cli_runner.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CLIResult:
    argv: tuple[str, ...]
    returncode: int | None       # None → timed out
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


async def run_cli(
    argv: list[str],
    *,
    timeout: float = 10.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CLIResult:
    """Run a local CLI subprocess with hard timeout. Never uses shell=True.

    `env`, when passed, MERGES with `os.environ` — caller is responsible for
    scoping (e.g. B5 ${VAR} expansion only injects the specific variables
    needed for this call).
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        # drain to avoid ResourceWarning
        try:
            await proc.communicate()
        except Exception:
            pass
        return CLIResult(
            argv=tuple(argv),
            returncode=None,
            stdout="",
            stderr="",
            duration_ms=int((loop.time() - started) * 1000),
            timed_out=True,
        )

    return CLIResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        duration_ms=int((loop.time() - started) * 1000),
        timed_out=False,
    )
```

### Capability probe

```python
async def probe_sync_capabilities(cli_name: str) -> frozenset[str]:
    """Return the set of {"mcp", "skills"} the CLI actually supports.

    Called once at adapter construction; result cached on the AdapterStatus.
    """
    supported: set[str] = set()
    r = await run_cli([cli_name, "mcp", "--help"], timeout=3.0)
    if r.returncode == 0:
        supported.add("mcp")
    r = await run_cli([cli_name, "skill", "--help"], timeout=3.0)
    if r.returncode == 0:
        supported.add("skills")
    return frozenset(supported)
```

### 批量策略

- 多条 MCP entry 逐条串行调（并行会撞 auth 缓存）；
- **单条失败不整批回滚**；
- 收集所有 per-entry `CLIResult` → 汇总到 `SyncEnvelope.sync[*].detail`。

---

## 4. B4 · Schema 修正

### 问题

- `drift_record.resource_id` 若为纯 int，无法区分是 `instruction.id` / `mcp_server.id` / `skill.id`；
- `sync_config.module` 未加 UNIQUE，可能双写。

### 决策

- `drift_record` 加 `resource_type` 列（enum）+ `resource_id`（int，指向对应表的 pk）；
- 不用多态外键（跨表 FK 在 SQLite 上没有 DB 级约束），查询走 `(resource_type, resource_id)` 联合；
- `sync_config.module` 加 `UniqueConstraint("module")`。

### DDL 片段（Phase 0.2 完整版）

```sql
-- drift_record
CREATE TABLE drift_record (
    id            INTEGER PRIMARY KEY,
    ts            DATETIME NOT NULL,
    module        TEXT NOT NULL,             -- "memory"|"mcp"|"skills"
    resource_type TEXT NOT NULL,             -- "instruction"|"mcp_server"|"skill"
    resource_id   INTEGER NOT NULL,          -- pk of the row in that table
    agent         TEXT NOT NULL,             -- "claude"|"codex"
    reason        TEXT NOT NULL,             -- "concurrent_write"|"external_edit"|"missing"
    expected_hash TEXT,
    actual_hash   TEXT,
    resolved      BOOLEAN NOT NULL DEFAULT 0,
    resolved_at   DATETIME,
    detail_json   TEXT                       -- free-form JSON
);
CREATE INDEX ix_drift_record_unresolved
    ON drift_record(resolved, ts DESC)
    WHERE resolved = 0;
CREATE INDEX ix_drift_record_resource
    ON drift_record(resource_type, resource_id);

-- sync_config
CREATE TABLE sync_config (
    id                INTEGER PRIMARY KEY,
    module            TEXT NOT NULL UNIQUE,  -- "memory"|"mcp"|"skills"
    enrolled_agents   TEXT NOT NULL,         -- JSON list, e.g. ["claude","codex"]
    poll_interval_sec INTEGER NOT NULL DEFAULT 30,
    enabled           BOOLEAN NOT NULL DEFAULT 1,
    updated_at        DATETIME NOT NULL
);
```

### Enum 常量

```python
# backend/csm/models/sync_common.py
from enum import StrEnum


class SyncModule(StrEnum):
    MEMORY = "memory"
    MCP = "mcp"
    SKILLS = "skills"


class DriftResourceType(StrEnum):
    INSTRUCTION = "instruction"
    MCP_SERVER = "mcp_server"
    SKILL = "skill"


class DriftReason(StrEnum):
    CONCURRENT_WRITE = "concurrent_write"   # B1 hash mismatch after write
    EXTERNAL_EDIT = "external_edit"         # poll saw a value we didn't write
    MISSING = "missing"                     # marker/entry disappeared
```

---

## 5. B5 · `${VAR}` 展开规则

### 问题

MCP entry 常带 secrets（`SLACK_TOKEN=${SLACK_TOKEN}`）。若把展开值拼进 argv：
- `/proc/<pid>/cmdline` 泄露；
- shell history、audit log 也可能记录。

### 决策

- **只走 subprocess env dict**：展开值放 `env={"SLACK_TOKEN": "..."}` 传给 `create_subprocess_exec`；
- **不进 argv**；不写 log；
- 未定义变量抛 `SyncPreflightError`，**阻止本次 sync**（不空字符串占位）；
- Error 里**只写变量名**，不写值（也不写"变量名的形状"避免侧信道）。

### Code pattern

```python
# backend/csm/modules/sync/env_expand.py
from __future__ import annotations

import os
import re

from csm.modules.sync.errors import SyncPreflightError

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_env_refs(
    template_values: dict[str, str],
) -> dict[str, str]:
    """Resolve `${VAR}` refs in each value against os.environ.

    Values with no ${...} are returned as-is. Values with unresolved refs
    raise SyncPreflightError listing the missing variable names (no values).
    """
    missing: set[str] = set()
    resolved: dict[str, str] = {}
    for k, raw in template_values.items():
        def _repl(m: re.Match[str]) -> str:
            var = m.group(1)
            val = os.environ.get(var)
            if val is None:
                missing.add(var)
                return ""
            return val
        resolved[k] = _VAR_RE.sub(_repl, raw)
    if missing:
        raise SyncPreflightError(
            f"undefined env vars: {sorted(missing)}",
            missing=sorted(missing),
        )
    return resolved
```

### 调用侧

```python
# in ClaudeAdapter.mcp_add(server)
env_pairs = resolve_env_refs(server.env)   # may raise SyncPreflightError
argv = ["claude", "mcp", "add", server.name, "--transport", server.transport,
        "--command", server.command]
# NOTE: server.env keys go via subprocess env, NOT argv
r = await run_cli(argv, env={**os.environ, **env_pairs}, timeout=10.0)
```

---

## 6. 数据模型 6 表

### 6.1 命名 & 时区约定

- 所有 `DATETIME` 字段一律 UTC naive（与 CSM 现有约定一致 · 见 CLAUDE.md "datetimes have no timezone info in API responses"）。
- 表名单数（与 `session` / `mission` / `run` 现有约定一致）。
- 所有 `enabled` / `resolved` 用 `BOOLEAN NOT NULL DEFAULT` 显式默认。
- `detail_json` / `env_json` / `metadata_json` 一律 `TEXT`（SQLite 无 JSONB）· 上层用 Pydantic 反序列化。

### 6.2 完整 DDL

```sql
-- ============================================================
-- 1. sync_config — per-module enrollment
-- ============================================================
CREATE TABLE sync_config (
    id                INTEGER PRIMARY KEY,
    module            TEXT NOT NULL UNIQUE,   -- SyncModule enum
    enrolled_agents   TEXT NOT NULL,          -- JSON list, e.g. ["claude","codex"]
    poll_interval_sec INTEGER NOT NULL DEFAULT 30,
    enabled           BOOLEAN NOT NULL DEFAULT 1,
    updated_at        DATETIME NOT NULL       -- UTC
);

-- ============================================================
-- 2. instruction — memory-module marker blocks
-- ============================================================
CREATE TABLE instruction (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- e.g. "python-lint-rules"; used as marker id
    title         TEXT NOT NULL,              -- human title shown in UI
    body          TEXT NOT NULL,              -- the actual instruction text (markdown)
    share_scope   TEXT NOT NULL,              -- JSON list ["claude","codex"]
    priority      INTEGER NOT NULL DEFAULT 0, -- ordering within CLAUDE.md/AGENTS.md
    created_at    DATETIME NOT NULL,
    updated_at    DATETIME NOT NULL
);
CREATE INDEX ix_instruction_name ON instruction(name);

-- ============================================================
-- 3. mcp_server — MCP server definitions
-- ============================================================
CREATE TABLE mcp_server (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,        -- CSM-authoritative; will be prefixed csm-<name> on the CLI side
    transport    TEXT NOT NULL,               -- "stdio" | "http" | "sse"
    command      TEXT,                        -- for stdio transport
    args_json    TEXT NOT NULL DEFAULT '[]',  -- JSON list of extra argv
    url          TEXT,                        -- for http/sse transport
    env_json     TEXT NOT NULL DEFAULT '{}',  -- JSON dict; values may contain ${VAR}
    enabled_for  TEXT NOT NULL,               -- JSON list ["claude","codex"]
    created_at   DATETIME NOT NULL,
    updated_at   DATETIME NOT NULL
);
CREATE INDEX ix_mcp_server_name ON mcp_server(name);

-- ============================================================
-- 4. skill — skill metadata (SKILL.md content lives on filesystem)
-- ============================================================
CREATE TABLE skill (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,       -- filesystem dir name; must match [a-z0-9-]+
    description   TEXT NOT NULL,              -- triggers snippet
    body_md       TEXT NOT NULL,              -- rendered into SKILL.md at sync time
    share_scope   TEXT NOT NULL,              -- JSON list ["claude","codex"]
    created_at    DATETIME NOT NULL,
    updated_at    DATETIME NOT NULL
);
CREATE INDEX ix_skill_name ON skill(name);

-- ============================================================
-- 5. sync_activity — append-only per-CRUD sync log
-- ============================================================
CREATE TABLE sync_activity (
    id             INTEGER PRIMARY KEY,
    ts             DATETIME NOT NULL,
    module         TEXT NOT NULL,             -- SyncModule enum value
    resource_type  TEXT NOT NULL,             -- DriftResourceType enum value
    resource_id    INTEGER,                   -- pk of the row that triggered sync (nullable for full-module rebuild)
    agent          TEXT NOT NULL,             -- "claude"|"codex"
    action         TEXT NOT NULL,             -- "add"|"remove"|"update"|"probe"
    status         TEXT NOT NULL,             -- SyncStatus enum value
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    detail_json    TEXT                       -- stderr, argv (redacted env), etc.
);
CREATE INDEX ix_sync_activity_module_ts ON sync_activity(module, ts DESC);
CREATE INDEX ix_sync_activity_resource ON sync_activity(resource_type, resource_id);

-- ============================================================
-- 6. drift_record — poll-detected divergence
-- ============================================================
CREATE TABLE drift_record (
    id            INTEGER PRIMARY KEY,
    ts            DATETIME NOT NULL,
    module        TEXT NOT NULL,              -- SyncModule enum value
    resource_type TEXT NOT NULL,              -- DriftResourceType enum value
    resource_id   INTEGER NOT NULL,           -- pk of the drifted row
    agent         TEXT NOT NULL,              -- "claude"|"codex"
    reason        TEXT NOT NULL,              -- DriftReason enum value
    expected_hash TEXT,
    actual_hash   TEXT,
    resolved      BOOLEAN NOT NULL DEFAULT 0,
    resolved_at   DATETIME,
    detail_json   TEXT
);
CREATE INDEX ix_drift_record_unresolved
    ON drift_record(resolved, ts DESC)
    WHERE resolved = 0;
CREATE INDEX ix_drift_record_resource ON drift_record(resource_type, resource_id);
```

### 6.3 Cross-table 约束（Python-enforced，非 DB-enforced）

- `sync_config.enrolled_agents` 每项必须在 `AdapterRegistry.names()` 中；写入前校验。
- `mcp_server.enabled_for` / `instruction.share_scope` / `skill.share_scope` 同上。
- `(resource_type, resource_id)` 在 `sync_activity` / `drift_record` 中不做 FK：
    - 资源被 hard-delete 时保留历史 log 更有价值；
    - Query 侧走 LEFT JOIN，前端展示"资源已删除"。

---

## 7. API 契约

### 7.1 通用约定

- 所有 mutating 端点返 `SyncEnvelope[T]`（见 §2）；
- Read 端点返裸资源（无 `sync` 字段）；
- 所有时间戳字段：ISO 8601 无 tz suffix，视为 UTC；
- 枚举字段：小写字符串（`sync_status`, `drift_reason`, `module`, `resource_type`）。

### 7.2 Path map

```
GET  /api/sync/config                                # list 3 rows (memory/mcp/skills)
PUT  /api/sync/config/{module}                       # update enrolled_agents / poll_interval
GET  /api/sync/status                                # per-module summary + drift count

# memory
GET  /api/sync/memory/instructions
POST /api/sync/memory/instructions                   → SyncEnvelope[Instruction]
GET  /api/sync/memory/instructions/{id}
PUT  /api/sync/memory/instructions/{id}              → SyncEnvelope[Instruction]
DELETE /api/sync/memory/instructions/{id}            → SyncEnvelope[dict] (data.deleted=true)

# mcp
GET  /api/sync/mcp/servers
POST /api/sync/mcp/servers                           → SyncEnvelope[McpServer]
PUT  /api/sync/mcp/servers/{id}                      → SyncEnvelope[McpServer]
DELETE /api/sync/mcp/servers/{id}                    → SyncEnvelope[dict]

# skills
GET  /api/sync/skills
POST /api/sync/skills                                → SyncEnvelope[Skill]
PUT  /api/sync/skills/{id}                           → SyncEnvelope[Skill]
DELETE /api/sync/skills/{id}                         → SyncEnvelope[dict]

# import-preview (pure-read; no state write)
GET  /api/sync/{module}/import-preview?agent=claude  # list config found on the agent side

# drift
GET  /api/sync/drift?resolved=false&limit=50
POST /api/sync/drift/{id}/resolve                    # user-driven mark-as-resolved

# activity
GET  /api/sync/activity?module=mcp&limit=100&since=<ts>
```

### 7.3 JSON Schema — key request/response

#### Instruction (POST body)

```json
{
  "name": "python-lint-rules",
  "title": "Python lint rules",
  "body": "Prefer ruff over black+flake8. ...",
  "share_scope": ["claude", "codex"],
  "priority": 10
}
```

`name` 校验：`^[a-z0-9][a-z0-9-]{0,63}$`（用作 marker id）

#### McpServer (POST body)

```json
{
  "name": "slack",
  "transport": "stdio",
  "command": "mcp-slack",
  "args_json": ["--workspace", "eng"],
  "url": null,
  "env_json": {"SLACK_TOKEN": "${SLACK_TOKEN}"},
  "enabled_for": ["claude", "codex"]
}
```

约束：
- `transport in {"stdio", "http", "sse"}`；
- `transport == "stdio"` → `command` 必填、`url` 必空；
- `transport in {"http", "sse"}` → `url` 必填、`command` 必空；
- `env_json` 值中的 `${VAR}` 在 sync 时 preflight（B5），CRUD 时**不**展开（保留 template）。

#### Skill (POST body)

```json
{
  "name": "grep-anywhere",
  "description": "Quick fuzzy-grep across the workspace.",
  "body_md": "---\nname: grep-anywhere\n...\n",
  "share_scope": ["claude"]
}
```

约束：
- `name` 匹配 `^[a-z0-9][a-z0-9-]{0,63}$`；
- `body_md` 首行必须是 `---`（frontmatter block）。

#### SyncEnvelope 通用 response

```json
{
  "data": { "...": "..." },
  "sync": [
    {"agent": "claude", "status": "ok", "detail": null},
    {"agent": "codex",  "status": "timeout", "detail": "codex mcp add exceeded 10s"}
  ],
  "warnings": ["codex: sync timed out; drift poll will retry"]
}
```

#### DriftRecord (GET response item)

```json
{
  "id": 42,
  "ts": "2026-07-29T03:15:22.401",
  "module": "mcp",
  "resource_type": "mcp_server",
  "resource_id": 7,
  "agent": "codex",
  "reason": "external_edit",
  "expected_hash": "a1b2c3...",
  "actual_hash": "9f8e7d...",
  "resolved": false,
  "resolved_at": null,
  "detail_json": {"note": "user edited ~/.codex/config.toml directly"}
}
```

### 7.4 错误响应

统一走 FastAPI 现有约定（`{"detail": "..."}` + HTTP 4xx/5xx）：

| HTTP | 场景 |
|---|---|
| 400 | 请求 body 校验失败（Pydantic）· `SyncPreflightError`（未定义 env var） |
| 404 | resource id 不存在 |
| 409 | `name` 冲突（UniqueConstraint 撞） |
| 422 | `share_scope` 含未知 agent 名 |
| 500 | 未知异常（未捕获的 DB error） |

**注意**：sync 失败**不走 5xx**，走 200 + envelope warnings（见 §2 决策）。

---

## 8. Adapter Protocol 扩展

新增 3 个 Capability（叠加到现有 4 个），10 个新方法签名。这一节的
Python 代码将在 Phase 1.3 落到 `backend/csm/backends/base.py`；本节是
签名 + docstring 定稿。

### 8.1 新 Capability

```python
# additions to Capability(StrEnum) in backends/base.py
class Capability(StrEnum):
    # ...(existing 4)...

    # Adapter can read/write the CLI's memory file (CLAUDE.md / AGENTS.md)
    # and manages marker blocks for CSM-owned instructions.
    SYNC_MEMORY = "sync_memory"

    # Adapter's CLI implements `mcp add / remove / list` subcommands.
    # Probed at startup via `<cli> mcp --help`. Absent → memory-module
    # sync ignores this adapter for MCP entries.
    SYNC_MCP = "sync_mcp"

    # Adapter's CLI honours a skills directory under home_dir().
    # For claude: ~/.claude/skills/. For codex: pending upstream support;
    # capability off by default until codex adds discovery.
    SYNC_SKILLS = "sync_skills"
```

### 8.2 新方法签名（加到 `CLIAdapter` Protocol）

所有方法均在 sync 子系统内部调用；domain code (SessionManager, EventStream)
**不**调用它们，也**不**特判 adapter 名字。

```python
# --- memory sync ------------------------------------------------

def memory_paths(self, scope: Literal["user", "project"]) -> list[Path]:
    """Return the ordered list of files CSM may write marker blocks into
    for the given scope.

    - scope="user"    : global config, e.g. ~/.claude/CLAUDE.md
                                      or ~/.codex/AGENTS.md
    - scope="project" : per-repo config (if the CLI supports it),
                        e.g. <project_root>/CLAUDE.md — else [].

    List order defines write priority; SyncService only writes to the
    first path that exists OR (if none exist) creates the first entry.
    Empty list means the adapter does not expose memory for that scope.
    """

def read_memory(self, path: Path) -> str:
    """Read a memory file; return "" if it does not exist.

    Never raises for missing files (that is the expected empty-state).
    Raises OSError only for genuine I/O failures.
    """

def write_memory_marker_block(
    self,
    path: Path,
    marker_id: str,
    body: str,
) -> None:
    """Write / replace a single marker-fenced block inside `path`.

    Marker fencing is per-adapter (see marker_syntax()). If a block with
    `marker_id` already exists it is REPLACED in place; otherwise the
    block is APPENDED at end of file.

    Internally MUST use the B1 atomic_write_with_hash_guard() so that
    concurrent REPL writes are detected as ConcurrentWriteDrift. This
    method never retries — SyncService catches drift and records it.
    """

# --- mcp sync ---------------------------------------------------

async def mcp_add(
    self,
    name: str,
    *,
    transport: Literal["stdio", "http", "sse"],
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
) -> "CLIResult":
    """Invoke `<cli> mcp add <name> [...]` and return the raw CLIResult.

    Idempotent contract: calling with identical args twice MUST produce
    (returncode==0) both times. Adapters that lack native idempotence
    should implement it by first calling `mcp_list()` and short-circuiting.

    `env` values are passed to the child process's environment dict
    (per B5) — NOT concatenated into argv. Caller is responsible for
    running resolve_env_refs() before this call.

    Called only when SYNC_MCP in capabilities.
    """

async def mcp_remove(self, name: str) -> "CLIResult":
    """Invoke `<cli> mcp remove <name>`.

    Idempotent: removing a non-existent name returns (returncode==0,
    detail="not found") — adapters should NOT treat "already absent" as
    an error.
    """

async def mcp_list(self) -> list[dict[str, Any]]:
    """Return currently-installed MCP entries as parsed dicts.

    Shape: `[{"name": str, "transport": str, "command"|"url": str, ...}]`.
    Values are what the CLI reports (post-expansion, minus secrets).
    Drift poll uses this to detect external edits.
    """

# --- skills sync ------------------------------------------------

def skills_dir(self) -> Path | None:
    """Return the directory the CLI scans for skills, or None if
    unsupported. Claude: `<home_dir>/skills`. Codex: None (2026-07 status)."""

def list_skills(self) -> list[dict[str, Any]]:
    """Enumerate skills currently visible to the CLI.

    Shape: `[{"name": str, "path": str, "description": str}]`. Reads
    each SKILL.md's frontmatter. Returns [] when skills_dir() is None.
    """

def write_simple_skill(self, spec: dict[str, Any]) -> None:
    """Materialise a `Skill` row onto disk as `<skills_dir>/<name>/SKILL.md`.

    `spec` shape:
        {"name": str, "description": str, "body_md": str}

    body_md is expected to already contain the YAML frontmatter (per §7
    validation). The adapter creates the parent dir if missing and uses
    B1 atomic_write_with_hash_guard() on the SKILL.md target.

    Raises NotImplementedError when SYNC_SKILLS not in capabilities.
    """

def remove_skill(self, name: str) -> None:
    """Delete `<skills_dir>/<name>/` recursively.

    Idempotent: no-op if the dir does not exist. Refuses to descend
    outside skills_dir() (path traversal guard).
    """

# --- misc -------------------------------------------------------

def marker_syntax(self) -> "MarkerSyntax":
    """Return the comment-style marker fences this adapter uses.

    Claude / Codex both use markdown files, so the default fence is HTML
    comment (`<!-- csm:start id=... -->` / `<!-- csm:end id=... -->`).
    Adapters MAY override — the field is exposed so we don't hardcode
    the fence in SyncService.
    """

async def probe_sync_capabilities(self) -> frozenset["Capability"]:
    """Runtime probe of the adapter's sync capability set.

    Called by SyncService at boot and every `poll_interval_sec` tick
    IFF adapter.probe() reports installed. Result is diffed against
    `self.capabilities`; on change the adapter's cached status is
    updated and a NotificationBus event fires.

    Implementation should call `<cli> <sub> --help` for each sub
    (`mcp`, `skill`, ...) via run_cli() with a 3s timeout.
    """
```

### 8.3 `MarkerSyntax` dataclass

```python
@dataclass(frozen=True)
class MarkerSyntax:
    """Fencing tokens for a memory marker block.

    Rendered as (using default HTML-comment style):
        <open_prefix> csm:start id={marker_id} <open_suffix>
        <body content>
        <close_prefix> csm:end id={marker_id} <close_suffix>

    The block is idempotently replaceable by matching the outer fences
    with `marker_id` — even if the body has been externally edited,
    the drift poll will detect divergence via hash compare.
    """
    open_prefix: str      # e.g. "<!--"
    open_suffix: str      # e.g. "-->"
    close_prefix: str     # e.g. "<!--"
    close_suffix: str     # e.g. "-->"

    @classmethod
    def html_comment(cls) -> "MarkerSyntax":
        return cls("<!--", "-->", "<!--", "-->")
```

### 8.4 Capability 与方法调用矩阵

| Capability | 检查前提 | 若缺失时 SyncService 行为 |
|---|---|---|
| `SYNC_MEMORY` | `memory_paths(*)` 非空 | 该 adapter 从 memory 模块 enrollment 移除；warning |
| `SYNC_MCP`    | `mcp --help` returncode==0 | `mcp_add/remove/list` 全部 skip；per-agent status="unsupported" |
| `SYNC_SKILLS` | `skills_dir()` 非 None | `write_simple_skill/remove_skill` 全部 skip；status="unsupported" |

Capability 变化通过 `probe_sync_capabilities()` 每 tick 检测；由 `unsupported → ok`
或反向变化时，SyncService 触发一次 full-module reconcile。

### 8.5 现有 4 个 Capability 不变

（`PRE_SPAWN_SESSION_ID` / `POST_SPAWN_BIND` / `HOOKS` / `INTERACTIVE_STREAM`
不在本 sync 子系统影响范围内，签名和行为保持现状。）

---

## 9. 未决问题（转入 Non-blocking）

- N1 · Drift poll adaptive backoff（30s ↔ 120s）
- N2 · `sync_activity` retention（默认 30 天，走 RollupWorker 同一 tick）
- N5 · Drift 事件走 NotificationBus 直发，不进 EventStream

以上进入 P1 实现清单，不改变 P0 spec。
