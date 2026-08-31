"""SyncOrchestrator — three-phase agent-driven multi-agent config sync.

Owns the tick lifecycle:

1. `run_tick(trigger)` locks (`_tick_running` bool, single asyncio task
   at a time), creates a `sync_agent_run` row (phase='collecting'),
   builds the input payload, calls `SyncAgent.decide(...)`, then applies
   the returned decisions.

2. `apply_decisions(...)` sorts decisions by `_ACTION_PRIORITY`,
   truncates non-skip to 30 when the agent overshoots its output cap,
   and dispatches each via the three-phase machinery.

3. `_apply_one_three_phase(d, collected_hashes)` implements the crash-
   recovery-safe apply (design v7 §3):

     Phase 1 (short DB tx): stale-read check + AdoptToCsm insert or
                            PropagateToAgent spec, allocates a
                            `fanout_ledger` row (status='pending').
                            Skip / ProposeConflict / dup-adopt return
                            without a fanout spec.

     Phase 2 (no DB lock):  `SyncService.sync_by_type_id(...)` fans out
                            to target agents. Adapter idempotency
                            contract (docs/backends/adapter_idempotency
                            _contract.md) guarantees repeat calls are
                            safe.

     Phase 3 (short DB tx, merged): writes `fanout_result_json`,
                            updates `last_synced_hashes` on the row for
                            each successful agent, closes the ledger
                            (status='done'). Failures re-raise; the
                            ledger row stays 'pending' and will be
                            re-processed by the next tick (all sites
                            are idempotent).

Bool tick flag (not asyncio.Lock): the between-if-and-set section is
`await`-free so the single asyncio thread reliably enforces mutual
exclusion (design v6 §11 rationale — Lock+timeout has a 3.11 leak
window).

Startup replay: `replay_pending_fanout_ledger()` picks up ONLY
`status='phase2_done'` rows (v6 legacy path — Phase 3 crashed after
Phase 2 completed). `pending` rows are left for the next scheduler
tick to re-drive from scratch. Bounded by `asyncio.wait_for(..., 30)`
in the lifespan so a large backlog can't block startup.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models.fanout_ledger import FanoutLedger
from csm.models.instruction import Instruction
from csm.models.mcp_server import McpServer
from csm.models.pending_decision import PendingDecision
from csm.models.skill import Skill
from csm.models.sync_agent_run import SyncAgentRun
from csm.models.sync_config import SyncConfig
from csm.modules.sync.schema import (
    AdoptToCsm,
    Decision,
    PropagateToAgent,
    ProposeConflict,
    Skip,
    sort_decisions_by_priority,
)
from csm.modules.sync.sentinels import (
    HASH_SENTINEL_UNSUPPORTED,
)
from csm.modules.sync.skill_store import replace_skill_files
from csm.modules.sync.state import (
    build_input_payload,
    compute_input_state_hash,
    redact_for_snapshot,
)
from csm.utils.time import now_utc_naive

if TYPE_CHECKING:
    from csm.backends import AdapterRegistry
    from csm.modules.sync.agent import SyncAgent
    from csm.modules.sync.service import PerAgentResult, SyncService

log = logging.getLogger(__name__)


# Cold-start batching threshold (design v6 §6). When the total resource
# count exceeds this, one tick splits into 3 sub-ticks (memory / mcp /
# skills) sharing a parent_run_id so the audit trail stays linked.
# Env-overridable for load-testing.
COLD_START_BATCH_THRESHOLD = int(
    os.environ.get("CSM_SYNC_BATCH_THRESHOLD", "400")
)


# ---------------------------------------------------------------------------
# Result / spec dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FanoutSpec:
    """What Phase 1 hands to Phase 2 for the actual fanout."""

    resource_type: str
    resource_id: int
    body_hash: str
    target_agents: list[str]


@dataclass
class ApplyResult:
    applied: int = 0
    rejected: int = 0
    stale: int = 0
    deleted: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_MODEL_MAP: dict[str, type] = {
    "instruction": Instruction,
    "mcp_server": McpServer,
    "skill": Skill,
}


def _body_of(row: Any) -> str:
    """Canonical body string for hashing.

    - Instruction: `body` text.
    - McpServer:   JSON of stable subset (name + transport + command/url).
    - Skill:       `body_md` text.
    """
    if isinstance(row, Instruction):
        return row.body or ""
    if isinstance(row, McpServer):
        stable = {
            "name": row.name,
            "transport": row.transport,
            "command": row.command,
            "url": row.url,
            "args_json": row.args_json,
        }
        return json.dumps(stable, sort_keys=True, ensure_ascii=False)
    if isinstance(row, Skill):
        return row.body_md or ""
    return ""


# ---------------------------------------------------------------------------
# SyncOrchestrator
# ---------------------------------------------------------------------------


class SyncOrchestrator:
    """Ticks the SyncAgent + applies its decisions through the three-phase
    ledger. See module docstring."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        adapter_registry: AdapterRegistry,
        sync_service: SyncService,
        sync_agent: SyncAgent,
    ) -> None:
        self._sm = sessionmaker
        self._reg = adapter_registry
        self._svc = sync_service
        self._agent = sync_agent
        # Bool flag not asyncio.Lock — the if/set section is await-free
        # so single-thread asyncio guarantees mutual exclusion.
        self._tick_running: bool = False
        self._current_run_id: int | None = None
        self._current_phase: str | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ---- lock ---------------------------------------------------------

    def try_acquire_tick(self) -> bool:
        """Non-reentrant, non-blocking acquire. Safe under single-thread
        asyncio because the check-and-set section has NO await."""
        if self._tick_running:
            return False
        self._tick_running = True
        return True

    def release_tick(self) -> None:
        self._tick_running = False

    # ---- state collection -------------------------------------------

    async def collect_state(
        self, meta: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build the input payload for the SyncAgent + `collected_hashes`
        for stale-read protection.

        Returns `(payload, collected_hashes)` where `collected_hashes`
        is `{f"{resource_type}:{id}": body_hash}`.
        """
        self._current_phase = "collecting"
        collected_hashes: dict[str, str] = {}

        async with self._sm() as db:
            # ---- CSM rows -------------------------------------------
            instr_rows = (await db.execute(select(Instruction))).scalars().all()
            mcp_rows = (await db.execute(select(McpServer))).scalars().all()
            skill_rows = (await db.execute(select(Skill))).scalars().all()

            csm_resources: dict[str, list[dict[str, Any]]] = {
                "instructions": [],
                "mcp_servers": [],
                "skills": [],
            }
            for r in instr_rows:
                body = r.body or ""
                collected_hashes[f"instruction:{r.id}"] = _sha256(body)
                csm_resources["instructions"].append({
                    "id": r.id,
                    "name": r.name,
                    "title": r.title,
                    "body": body,
                    "share_scope": r.share_scope or [],
                    "origin": getattr(r, "origin", "csm"),
                    "last_synced_hashes": getattr(r, "last_synced_hashes", {}) or {},
                })
            for m in mcp_rows:
                collected_hashes[f"mcp_server:{m.id}"] = _sha256(_body_of(m))
                csm_resources["mcp_servers"].append({
                    "id": m.id,
                    "name": m.name,
                    "transport": m.transport,
                    "command": m.command,
                    "args_json": m.args_json,
                    "url": m.url,
                    "env_json": m.env_json or {},
                    "enabled_for": m.enabled_for or [],
                    "origin": getattr(m, "origin", "csm"),
                    "last_synced_hashes": getattr(m, "last_synced_hashes", {}) or {},
                })
            for s in skill_rows:
                body_md = s.body_md or ""
                collected_hashes[f"skill:{s.id}"] = _sha256(body_md)
                # Skills are metadata-only on the wire to the LLM: it decides on
                # the content HASH (adopt/propagate/conflict/skip), never the
                # body. body_sha256 lets it compare against each agent's hash.
                # file_count travels alongside so a skill whose helper scripts
                # went missing is distinguishable from one that never had any.
                csm_resources["skills"].append({
                    "id": s.id,
                    "name": s.name,
                    "body_sha256": _sha256(body_md),
                    "file_count": len(s.files),
                    "share_scope": s.share_scope or [],
                    "origin": getattr(s, "origin", "csm"),
                    "last_synced_hashes": getattr(s, "last_synced_hashes", {}) or {},
                })

            # ---- pending decisions recent (last 30d, non-resolved) --
            cutoff = now_utc_naive() - timedelta(days=30)
            pending_rows = (await db.execute(
                select(PendingDecision).where(
                    PendingDecision.status.in_(("pending", "resolve_failed")),
                    PendingDecision.ts >= cutoff,
                ).order_by(PendingDecision.ts.desc()).limit(50)
            )).scalars().all()
            pending_recent = [
                {
                    "id": p.id,
                    "ts": p.ts.isoformat() if p.ts else None,
                    "resource_type": p.resource_type,
                    "resource_id": p.resource_id,
                    "proposed_action": p.proposed_action,
                    "candidates_json": p.candidates_json,
                    "status": p.status,
                }
                for p in pending_rows
            ]

        # Per-module resource-name allowlists: when set on a module's
        # SyncConfig, sync only considers those resources. None = no filter.
        # This is how the user scopes sync to their own resources (e.g. own
        # skills) instead of everything the agent exposes. Generalizes across
        # modules — matched by name.
        async def _allowlist(module: str) -> set[str] | None:
            async with self._sm() as db:
                cfg = (await db.execute(
                    select(SyncConfig).where(SyncConfig.module == module)
                )).scalar_one_or_none()
            if cfg is not None and getattr(cfg, "resource_allowlist", None) is not None:
                return set(cfg.resource_allowlist)
            return None

        skills_allowlist = await _allowlist("skills")
        mcp_allowlist = await _allowlist("mcp")

        # ---- agent-side state ---------------------------------------
        agents_state: dict[str, dict[str, Any]] = {}
        for name in self._reg.names():
            adapter = self._reg.get(name)
            try:
                memory_full = adapter.read_memory_full("user")
            except Exception:
                log.exception("read_memory_full failed for %s", name)
                memory_full = None
            try:
                skills_full = adapter.list_skills_full()
            except Exception:
                log.exception("list_skills_full failed for %s", name)
                skills_full = []
            if skills_allowlist is not None:
                skills_full = [
                    s for s in skills_full if s.get("name") in skills_allowlist
                ]
            # Metadata-only skills for the LLM: name + description + body hash,
            # never the body_md. This is what keeps the decision payload tiny
            # regardless of how many / how large the skills are.
            # name + hash only — descriptions/bodies aren't needed to decide
            # sync by hash, and dropping them keeps the payload tiny (62 skills
            # was ~140KB with descriptions; a few KB without).
            skills_meta = [
                {
                    "name": s.get("name"),
                    "body_sha256": _sha256(s.get("body_md") or ""),
                }
                for s in skills_full
            ]
            try:
                mcp_full = await adapter.list_mcp_servers_full()
            except Exception:
                log.exception("list_mcp_servers_full failed for %s", name)
                mcp_full = []
            if mcp_allowlist is not None:
                mcp_full = [
                    m for m in mcp_full if m.get("name") in mcp_allowlist
                ]
            agents_state[name] = {
                "memory_full": memory_full,
                "mcp_servers": mcp_full,
                "skills": skills_meta,
            }

        payload = build_input_payload(
            csm_resources=csm_resources,
            agent_states=agents_state,
            pending_recent=pending_recent,
            meta=meta or {},
        )
        return payload, collected_hashes

    # ---- run_tick -----------------------------------------------------

    def _payload_total_count(self, payload: dict[str, Any]) -> int:
        csm = payload.get("csm_resources") or {}
        return (
            len(csm.get("instructions") or [])
            + len(csm.get("mcp_servers") or [])
            + len(csm.get("skills") or [])
        )

    def _filter_payload_by_module(
        self, payload: dict[str, Any], module: str,
    ) -> dict[str, Any]:
        """Return a copy of `payload` scoped to a single module.

        module ∈ {'memory', 'mcp', 'skills'} maps to the resource-list keys
        {'instructions', 'mcp_servers', 'skills'} respectively. Agent-side
        state is likewise filtered so the SyncAgent doesn't see the other
        modules and get confused about scope.
        """
        key_map = {
            "memory": "instructions",
            "mcp": "mcp_servers",
            "skills": "skills",
        }
        rk = key_map[module]
        csm_full = payload.get("csm_resources") or {}
        csm_scoped: dict[str, list] = {"instructions": [], "mcp_servers": [], "skills": []}
        csm_scoped[rk] = list(csm_full.get(rk) or [])

        agents_scoped: dict[str, dict[str, Any]] = {}
        for name, ad in (payload.get("agents") or {}).items():
            if not isinstance(ad, dict):
                continue
            scoped = dict(ad)
            if module == "memory":
                scoped["mcp_servers"] = []
                scoped["skills"] = []
            elif module == "mcp":
                scoped["memory_full"] = None
                scoped["skills"] = []
            else:  # skills
                scoped["memory_full"] = None
                scoped["mcp_servers"] = []
            agents_scoped[name] = scoped

        return {
            **payload,
            "csm_resources": csm_scoped,
            "agents": agents_scoped,
            "meta": {**(payload.get("meta") or {}), "module_filter": module},
        }

    async def create_run_row(self, trigger: str) -> int:
        """Insert the placeholder run row (phase='collecting') and return its
        id, stamping `_current_run_id` so `live_phase` is pollable at once.

        Split out of `run_tick` so the /agent-tick endpoint can grab the
        run_id up front and then run the tick in the background — without
        racing the tick's own row creation (a fast/disabled tick used to
        finish and clear `_current_run_id` before the caller could read it).
        """
        async with self._sm() as db:
            run_row = SyncAgentRun(
                ts=now_utc_naive(),
                trigger=trigger,
                prompt_hash="",
                input_state_hash="",
                input_snapshot_json={},
                phase="collecting",
            )
            db.add(run_row)
            await db.commit()
            await db.refresh(run_row)
            rid = run_row.id
        self._current_run_id = rid
        return rid

    async def run_tick(
        self, trigger: str, user_intent: str | None = None,
        run_row_id: int | None = None,
    ) -> SyncAgentRun:
        """Execute one full tick end-to-end. Caller MUST have already
        called `try_acquire_tick()`; this method releases the lock in
        its `finally`.

        `run_row_id`: reuse a row already created via `create_run_row`
        (background path). When None, one is created now (synchronous /
        scheduler path).

        `user_intent` (optional free text, e.g. "migrate claude's mcp to
        codex") is threaded into the payload meta so the SyncAgent can bias
        its decisions toward the user's goal — without changing the
        four-phase architecture or the safety rules (secrets stay
        human-gated; conflicts still escalate to pending decisions).

        Batches into 3 sub-ticks when total_resource > COLD_START_BATCH_THRESHOLD
        (v6 §6 cold-start protection). Parent + sub rows share parent_run_id.
        """
        if run_row_id is None:
            run_row_id = await self.create_run_row(trigger)
        else:
            self._current_run_id = run_row_id

        try:
            _meta: dict[str, Any] = {"trigger": trigger}
            if user_intent and user_intent.strip():
                _meta["user_intent"] = user_intent.strip()
            payload, collected_hashes = await self.collect_state(
                meta=_meta,
            )

            # Cold-start batching (design v6 §6).
            total = self._payload_total_count(payload)
            if total > COLD_START_BATCH_THRESHOLD:
                log.info(
                    "cold-start batching: %d resources > %d threshold; "
                    "splitting into 3 sub-ticks",
                    total, COLD_START_BATCH_THRESHOLD,
                )
                await self._run_batched_sub_ticks(
                    run_row_id, payload, collected_hashes,
                )
                async with self._sm() as db:
                    await db.execute(update(SyncAgentRun).where(
                        SyncAgentRun.id == run_row_id,
                    ).values(
                        phase="done",
                        prompt_hash="batched",
                        input_state_hash=compute_input_state_hash(
                            redact_for_snapshot(payload),
                        ),
                    ))
                    await db.commit()
                return await self._reload(run_row_id)
            redacted = redact_for_snapshot(payload)
            input_hash = compute_input_state_hash(redacted)

            self._current_phase = "deciding"
            async with self._sm() as db:
                await db.execute(update(SyncAgentRun).where(
                    SyncAgentRun.id == run_row_id,
                ).values(
                    input_snapshot_json=redacted,
                    input_state_hash=input_hash,
                    phase="deciding",
                ))
                await db.commit()

            decisions_payload, meta = await self._agent.decide(payload)

            # Persist raw + prompt_hash regardless of parse outcome.
            async with self._sm() as db:
                await db.execute(update(SyncAgentRun).where(
                    SyncAgentRun.id == run_row_id,
                ).values(
                    prompt_hash=meta.get("prompt_hash", ""),
                    response_raw=meta.get("raw_text"),
                    response_parsed=(
                        decisions_payload.model_dump()
                        if decisions_payload is not None else None
                    ),
                    token_usage_json=meta.get("token_usage"),
                    duration_ms=meta.get("duration_ms"),
                    error=meta.get("error") or meta.get("parse_error"),
                ))
                await db.commit()

            if decisions_payload is None:
                self._current_phase = "done"
                async with self._sm() as db:
                    await db.execute(update(SyncAgentRun).where(
                        SyncAgentRun.id == run_row_id,
                    ).values(phase="done"))
                    await db.commit()
                return await self._reload(run_row_id)

            self._current_phase = "applying"
            async with self._sm() as db:
                await db.execute(update(SyncAgentRun).where(
                    SyncAgentRun.id == run_row_id,
                ).values(phase="applying"))
                await db.commit()

            result = await self.apply_decisions(
                decisions_payload.decisions,
                collected_hashes,
                run_id=run_row_id,
            )

            self._current_phase = "done"
            async with self._sm() as db:
                await db.execute(update(SyncAgentRun).where(
                    SyncAgentRun.id == run_row_id,
                ).values(
                    phase="done",
                    decisions_count=len(decisions_payload.decisions),
                    applied_count=result.applied,
                    rejected_count=result.rejected,
                    stale_skipped_count=result.stale,
                    deleted_after_collect_count=result.deleted,
                ))
                await db.commit()
            return await self._reload(run_row_id)
        finally:
            self._current_run_id = None
            self._current_phase = None
            self.release_tick()

    async def _run_batched_sub_ticks(
        self, parent_run_id: int,
        full_payload: dict[str, Any],
        collected_hashes: dict[str, str],
    ) -> None:
        """Split one big cold-start into 3 module-scoped sub-ticks.

        Each sub-tick gets its own sync_agent_run row (parent_run_id
        pointing to the batch parent), independent SyncAgent call, and
        independent apply. Errors on one module DON'T abort the others.
        """
        for module in ("memory", "mcp", "skills"):
            sub_payload = self._filter_payload_by_module(full_payload, module)
            sub_started = now_utc_naive()
            async with self._sm() as db:
                sub_row = SyncAgentRun(
                    ts=sub_started,
                    trigger="sub_run",
                    prompt_hash="",
                    input_state_hash="",
                    input_snapshot_json=redact_for_snapshot(sub_payload),
                    parent_run_id=parent_run_id,
                    phase="deciding",
                )
                db.add(sub_row)
                await db.commit()
                await db.refresh(sub_row)
                sub_id = sub_row.id

            try:
                decisions_payload, meta = await self._agent.decide(sub_payload)
                async with self._sm() as db:
                    await db.execute(update(SyncAgentRun).where(
                        SyncAgentRun.id == sub_id,
                    ).values(
                        prompt_hash=meta.get("prompt_hash", ""),
                        response_raw=meta.get("raw_text"),
                        response_parsed=(
                            decisions_payload.model_dump()
                            if decisions_payload is not None else None
                        ),
                        token_usage_json=meta.get("token_usage"),
                        duration_ms=meta.get("duration_ms"),
                        error=meta.get("error") or meta.get("parse_error"),
                        phase="applying" if decisions_payload else "done",
                    ))
                    await db.commit()

                if decisions_payload is None:
                    continue
                result = await self.apply_decisions(
                    decisions_payload.decisions,
                    collected_hashes,
                    run_id=sub_id,
                )
                async with self._sm() as db:
                    await db.execute(update(SyncAgentRun).where(
                        SyncAgentRun.id == sub_id,
                    ).values(
                        phase="done",
                        decisions_count=len(decisions_payload.decisions),
                        applied_count=result.applied,
                        rejected_count=result.rejected,
                        stale_skipped_count=result.stale,
                        deleted_after_collect_count=result.deleted,
                    ))
                    await db.commit()
            except Exception:
                log.exception(
                    "sub-tick module=%s parent=%d failed; other modules continue",
                    module, parent_run_id,
                )

    async def _reload(self, run_id: int) -> SyncAgentRun:
        async with self._sm() as db:
            return await db.get(SyncAgentRun, run_id)

    # ---- apply_decisions ---------------------------------------------

    async def apply_decisions(
        self,
        decisions: list[Decision],
        collected_hashes: dict[str, str],
        run_id: int | None = None,
    ) -> ApplyResult:
        """Sort → truncate → dispatch each via three-phase apply."""
        # v7 §4: sort by _ACTION_PRIORITY; if non-skip > 40 truncate to 30.
        non_skip = [d for d in decisions if not isinstance(d, Skip)]
        skips = [d for d in decisions if isinstance(d, Skip)]
        if len(non_skip) > 40:
            log.warning(
                "SyncAgent violated output cap: %d non-skip decisions; "
                "truncating to top 30 by _ACTION_PRIORITY",
                len(non_skip),
            )
            non_skip = sort_decisions_by_priority(non_skip)[:30]
        else:
            non_skip = sort_decisions_by_priority(non_skip)
        ordered = non_skip + skips

        result = ApplyResult()
        for d in ordered:
            if self._stop_event.is_set():
                log.info(
                    "stop_event set, breaking apply_decisions loop "
                    "after %d applied",
                    result.applied,
                )
                break
            outcome = await self._apply_one_three_phase(
                d, collected_hashes, run_id=run_id,
            )
            if outcome == "applied":
                result.applied += 1
            elif outcome == "stale":
                result.stale += 1
            elif outcome == "deleted":
                result.deleted += 1
            elif outcome == "rejected":
                result.rejected += 1
            # any other string is treated as no-op counter-wise
        return result

    # ---- three-phase apply ------------------------------------------

    async def _apply_one_three_phase(
        self,
        d: Decision,
        collected_hashes: dict[str, str],
        run_id: int | None = None,
    ) -> str:
        """Return one of: applied / rejected / stale / deleted."""
        # ---- Phase 1: DB only ----------------------------------------
        spec: FanoutSpec | str | None = None
        ledger_id: int | None = None
        try:
            async with self._sm() as session, session.begin():
                spec_or_status = await self._db_phase1(
                    session, d, collected_hashes, run_id=run_id,
                )
                if isinstance(spec_or_status, str):
                    return spec_or_status
                spec = spec_or_status
                if spec is not None:
                    ledger_id = await self._insert_fanout_ledger(
                        session, spec,
                    )
        except Exception:
            log.exception("Phase 1 failed for decision action=%s", getattr(d, "action", "?"))
            return "rejected"

        if spec is None:
            return "applied"

        # ---- Phase 2: no DB lock -----------------------------------
        try:
            fanout_results = await self._svc.sync_by_type_id(
                spec.resource_type, spec.resource_id, spec.target_agents,
            )
        except Exception:
            log.exception(
                "Phase 2 fanout failed; ledger id=%s left pending for retry",
                ledger_id,
            )
            # Leave ledger 'pending' — next tick will re-drive.
            return "rejected"

        # ---- Phase 3: merged (save result + hashes + close) --------
        try:
            async with self._sm() as session, session.begin():
                await self._save_and_close_ledger(
                    session, ledger_id, spec, fanout_results,
                )
        except Exception:
            log.exception(
                "Phase 3 failed; ledger id=%s left pending (adapter "
                "idempotency will handle retry)",
                ledger_id,
            )
            return "rejected"

        return "applied"

    # ---- Phase 1 detail ---------------------------------------------

    async def _db_phase1(
        self,
        session,
        d: Decision,
        collected_hashes: dict[str, str],
        run_id: int | None = None,
    ) -> FanoutSpec | str | None:
        """DB-side leg of the three-phase apply. Returns:

        - `"applied"` / `"rejected"` / `"stale"` / `"deleted"`  when the
          decision is fully handled inside this transaction (Skip,
          ProposeConflict, idempotent Adopt, name-collision Adopt).
        - `FanoutSpec`  when Phase 2 must fan out to agents.
        - `None`  when nothing further is required but the outcome is
          `"applied"` (equivalent to the string form above; kept for
          call-site clarity — callers check `isinstance(x, str)`).
        """
        if isinstance(d, Skip):
            # Recorded via response_parsed on the run row; no DB change.
            return "applied"

        if isinstance(d, ProposeConflict):
            await self._create_pending_from_conflict(session, d, run_id=run_id)
            return "applied"

        if isinstance(d, AdoptToCsm):
            model = _MODEL_MAP[d.resource_type]
            # Resolve (name, body) by shape: skill reads the body from the
            # source agent's disk (reference-style); instruction/mcp carry the
            # body in the candidate. `None` body → the resource vanished from
            # the source since the decision was made; treat as deleted.
            if d.resource_type == "skill":
                disk = self._read_agent_skill(d.source_agent, d.resource_name or "")
                if disk is None:
                    return "deleted"
                name = d.resource_name
                candidate_body = disk.get("body_md") or ""
            else:
                name = d.candidate.name
                candidate_body = self._body_of_candidate(d)
            new_hash = _sha256(candidate_body)

            existing = (await session.execute(
                select(model).where(model.name == name)
            )).scalar_one_or_none()
            if existing is not None:
                existing_body = _body_of(existing)
                if _sha256(existing_body) == new_hash:
                    # Idempotent — nothing to do.
                    return "applied"
                # Name collision, different content → surface as conflict.
                await self._create_pending_conflict_from_diff(
                    session, d, existing, existing_body, candidate_body,
                    run_id=run_id,
                )
                return "applied"

            # New adoption path: INSERT.
            if d.resource_type == "skill":
                row = self._new_skill_row_from_disk(
                    str(name), disk.get("description"), candidate_body,
                    d.recommended_scope,
                )
            else:
                row = self._new_row_from_candidate(d)
            session.add(row)
            await session.flush()  # populate row.id for ledger FK
            if d.resource_type == "skill":
                # Bring the helper scripts / references across too, else CSM
                # becomes the authoritative copy of a bundle it only ever
                # saw the SKILL.md of.
                await replace_skill_files(session, row, (disk or {}).get("files") or [])
            spec = FanoutSpec(
                resource_type=d.resource_type,
                resource_id=row.id,
                body_hash=new_hash,
                # source_agent already has this content — no need to push back.
                target_agents=[
                    a for a in d.recommended_scope if a != d.source_agent
                ],
            )
            # Also stamp source_agent's hash so we don't re-propose to it.
            hashes = dict(getattr(row, "last_synced_hashes", None) or {})
            hashes[d.source_agent] = new_hash
            row.last_synced_hashes = hashes
            row.origin = f"agent_adopt:{d.source_agent}"
            return spec if spec.target_agents else "applied"

        if isinstance(d, PropagateToAgent):
            model = _MODEL_MAP[d.resource_type]
            row = await session.get(model, d.resource_id)
            if row is None:
                return "deleted"
            cur_body = _body_of(row)
            cur_hash = _sha256(cur_body)
            snap_key = f"{d.resource_type}:{d.resource_id}"
            if collected_hashes.get(snap_key) != cur_hash:
                return "stale"
            return FanoutSpec(
                resource_type=d.resource_type,
                resource_id=d.resource_id,
                body_hash=cur_hash,
                target_agents=[d.target_agent],
            )

        return "rejected"

    # ---- ledger writes ---------------------------------------------

    async def _insert_fanout_ledger(
        self, session, spec: FanoutSpec,
    ) -> int:
        entry = FanoutLedger(
            ts=now_utc_naive(),
            resource_type=spec.resource_type,
            resource_id=spec.resource_id,
            body_hash=spec.body_hash,
            target_agents=list(spec.target_agents),
            status="pending",
            attempt_count=0,
        )
        session.add(entry)
        await session.flush()
        return entry.id

    async def _save_and_close_ledger(
        self,
        session,
        ledger_id: int,
        spec: FanoutSpec,
        fanout_results: list[PerAgentResult],
    ) -> None:
        """Merged Phase 2.5 + Phase 3 (v7 §3): write result_json + update
        per-agent hashes + status='done' — all in one short tx."""
        # Deferred import to avoid module-import cycle.
        from csm.models.sync_common import SyncStatus

        entry = await session.get(FanoutLedger, ledger_id)
        if entry is None:
            return
        entry.fanout_result_json = [r.as_dict() for r in fanout_results]
        entry.attempted_at = now_utc_naive()
        entry.attempt_count = (entry.attempt_count or 0) + 1

        model = _MODEL_MAP[spec.resource_type]
        row = await session.get(model, spec.resource_id)
        if row is None:
            entry.status = "done"
            return

        hashes = dict(getattr(row, "last_synced_hashes", None) or {})
        for r in fanout_results:
            if r.status is SyncStatus.OK:
                hashes[r.agent] = spec.body_hash
            elif r.status is SyncStatus.UNSUPPORTED:
                hashes[r.agent] = HASH_SENTINEL_UNSUPPORTED
            # TIMEOUT / ERROR / SKIPPED: leave hash untouched → next tick
            # will retry (agent's `agent_needs_sync` will see mismatch).
        row.last_synced_hashes = hashes
        entry.status = "done"

    # ---- pending decision helpers -----------------------------------

    async def _create_pending_from_conflict(
        self, session, d: ProposeConflict, run_id: int | None = None,
    ) -> None:
        if d.resource_type == "skill":
            # Reference-style: the agent gave names, not bodies. Fetch each
            # diverging agent's body from disk + CSM's canonical body from DB so
            # the user sees a real diff.
            candidates: dict[str, str] = {}
            for agent in (d.conflict_agents or []):
                sk = self._read_agent_skill(agent, d.resource_name or "")
                if sk is not None:
                    candidates[agent] = sk.get("body_md") or ""
            existing = (await session.execute(
                select(Skill).where(Skill.name == d.resource_name)
            )).scalar_one_or_none()
            if existing is not None:
                candidates["csm"] = existing.body_md or ""
        else:
            candidates = dict(d.candidates or {})
        session.add(PendingDecision(
            agent_run_id=run_id or 0,
            ts=now_utc_naive(),
            resource_type=d.resource_type,
            resource_id=d.resource_id,
            proposed_action="propose_conflict",
            candidates_json=candidates,
            status="pending",
        ))

    async def _create_pending_conflict_from_diff(
        self,
        session,
        d: AdoptToCsm,
        existing_row: Any,
        existing_body: str,
        candidate_body: str,
        run_id: int | None = None,
    ) -> None:
        # `candidate_body` is passed in (from disk for skill, from candidate for
        # instruction/mcp) so this helper never has to know the source shape.
        session.add(PendingDecision(
            agent_run_id=run_id or 0,
            ts=now_utc_naive(),
            resource_type=d.resource_type,
            resource_id=existing_row.id,
            proposed_action="adopt_conflict",
            candidates_json={
                "csm": existing_body,
                d.source_agent: candidate_body,
            },
            status="pending",
        ))

    # ---- agent-side skill disk read (reference-style adopt) ---------

    def _read_agent_skill(
        self, agent_name: str, skill_name: str,
    ) -> dict[str, Any] | None:
        """Read one skill in full ({name, description, body_md, files}) from
        an agent's on-disk skills dir. Returns None if the agent/skill is gone.

        Reference-style adopt reads the body HERE (at apply time) instead of
        trusting an LLM-echoed copy — always the freshest content, and the LLM
        never had to carry it.

        Uses `read_skill_bundle()` rather than `list_skills_full()`: adopting
        a skill has to bring its helper scripts along, otherwise CSM becomes
        the authoritative copy of a bundle it only ever saw one file of."""
        if not skill_name:
            return None
        try:
            adapter = self._reg.get(agent_name)
            return adapter.read_skill_bundle(skill_name)
        except Exception:
            log.exception(
                "sync: read skill %r from agent %s failed", skill_name, agent_name,
            )
        return None

    @staticmethod
    def _new_skill_row_from_disk(
        name: str, description: str | None, body_md: str, scope: list[str],
    ) -> Skill:
        return Skill(
            name=name,
            description=description or "",
            body_md=body_md,
            share_scope=list(scope),
            created_at=now_utc_naive(),
            updated_at=now_utc_naive(),
        )

    # ---- candidate → row + body extraction --------------------------

    @staticmethod
    def _body_of_candidate(d: AdoptToCsm) -> str:
        cand = d.candidate
        if d.resource_type == "instruction":
            return cand.body  # type: ignore[attr-defined]
        if d.resource_type == "skill":
            return cand.body_md  # type: ignore[attr-defined]
        if d.resource_type == "mcp_server":
            # Same canonical shape as _body_of(McpServer).
            return json.dumps({
                "name": cand.name,
                "transport": cand.transport,  # type: ignore[attr-defined]
                "command": getattr(cand, "command", None),
                "url": getattr(cand, "url", None),
                "args_json": getattr(cand, "args_json", []),
            }, sort_keys=True, ensure_ascii=False)
        return ""

    @staticmethod
    def _new_row_from_candidate(d: AdoptToCsm) -> Any:
        cand = d.candidate
        if d.resource_type == "instruction":
            return Instruction(
                name=cand.name,
                title=cand.title,  # type: ignore[attr-defined]
                body=cand.body,  # type: ignore[attr-defined]
                share_scope=list(d.recommended_scope),
                priority=0,
                created_at=now_utc_naive(),
                updated_at=now_utc_naive(),
            )
        if d.resource_type == "mcp_server":
            return McpServer(
                name=cand.name,
                transport=cand.transport,  # type: ignore[attr-defined]
                command=getattr(cand, "command", None),
                args_json=getattr(cand, "args_json", []),
                url=getattr(cand, "url", None),
                env_json=getattr(cand, "env_json", {}) or {},
                enabled_for=list(d.recommended_scope),
                created_at=now_utc_naive(),
                updated_at=now_utc_naive(),
            )
        if d.resource_type == "skill":
            return Skill(
                name=cand.name,
                description=cand.description,  # type: ignore[attr-defined]
                body_md=cand.body_md,  # type: ignore[attr-defined]
                share_scope=list(d.recommended_scope),
                created_at=now_utc_naive(),
                updated_at=now_utc_naive(),
            )
        raise ValueError(f"unknown resource_type: {d.resource_type!r}")

    # ---- startup replay ---------------------------------------------

    async def replay_pending_fanout_ledger(self) -> None:
        """v7 §2: replay ONLY status='phase2_done' rows at startup.

        The `pending` state is left to the next scheduled tick — a tick
        re-collects state and re-decides, and adapter idempotency
        prevents duplicate writes.
        """
        async with self._sm() as db:
            entries = (await db.execute(
                select(FanoutLedger).where(
                    FanoutLedger.status == "phase2_done",
                    FanoutLedger.attempt_count < 3,
                ).order_by(FanoutLedger.ts.asc())
            )).scalars().all()

        for entry in entries:
            try:
                await self._replay_one_phase2_done_entry(entry.id)
            except Exception:
                log.exception("replay phase2_done failed id=%d", entry.id)
                await self._bump_attempt_or_terminal(entry.id)

    async def _replay_one_phase2_done_entry(self, entry_id: int) -> None:
        """Phase 3 only — no adapter call. Short DB tx."""
        async with self._sm() as db, db.begin():
            entry = await db.get(FanoutLedger, entry_id)
            if entry is None or entry.status != "phase2_done":
                return
            entry.attempt_count = (entry.attempt_count or 0) + 1
            entry.attempted_at = now_utc_naive()

            model = _MODEL_MAP.get(entry.resource_type)
            if model is None:
                entry.status = "done"
                return
            row = await db.get(model, entry.resource_id)
            if row is None:
                entry.status = "done"
                return

            current_body = _body_of(row)
            current_hash = _sha256(current_body)
            # Deferred import.
            from csm.models.sync_common import SyncStatus

            hashes = dict(getattr(row, "last_synced_hashes", None) or {})
            for item in entry.fanout_result_json or []:
                agent = item.get("agent")
                status_str = item.get("status")
                if not agent or not status_str:
                    continue
                try:
                    st = SyncStatus(status_str)
                except ValueError:
                    continue
                if st is SyncStatus.OK:
                    hashes[agent] = current_hash
                elif st is SyncStatus.UNSUPPORTED:
                    hashes[agent] = HASH_SENTINEL_UNSUPPORTED
            row.last_synced_hashes = hashes
            entry.status = "done"

    async def _bump_attempt_or_terminal(self, entry_id: int) -> None:
        async with self._sm() as db, db.begin():
            entry = await db.get(FanoutLedger, entry_id)
            if entry is None:
                return
            entry.attempt_count = (entry.attempt_count or 0) + 1
            entry.attempted_at = now_utc_naive()
            if entry.attempt_count >= 3:
                entry.status = "failed_terminal"
                log.error(
                    "fanout_ledger id=%d → failed_terminal after 3 attempts",
                    entry_id,
                )

    async def cleanup_stale_pending_ledger(self, older_than_days: int = 30) -> int:
        """Scheduler-called: sweep `pending` rows older than N days to
        `failed_terminal` so the ledger doesn't grow forever."""
        cutoff = now_utc_naive() - timedelta(days=older_than_days)
        async with self._sm() as db, db.begin():
            result = await db.execute(
                update(FanoutLedger).where(
                    FanoutLedger.status == "pending",
                    FanoutLedger.ts < cutoff,
                ).values(status="failed_terminal")
            )
            return result.rowcount or 0


__all__ = [
    "SyncOrchestrator",
    "FanoutSpec",
    "ApplyResult",
]
