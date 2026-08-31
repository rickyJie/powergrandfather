"""SyncService + DriftPoller.

`SyncService` is the orchestrator for the "single source of truth ⇒
per-agent materialisation" flow. Every mutating API endpoint calls one
of the `sync_*` / `remove_*` methods, receives a `list[PerAgentResult]`,
and returns it inside a `SyncEnvelope[T]` (see spec §2 B2).

`DriftPoller` runs on a background asyncio task. Every tick (default
60s) it inspects each enrolled (module, agent) pair and records any
divergence between the DB source of truth and the on-agent state.

Neither class special-cases on adapter name — capability checks and
protocol calls only. Adding a new adapter is a zero-touch operation as
long as it implements the `SYNC_*` Capability contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.backends import AdapterRegistry, CLIAdapter
from csm.backends.base import Capability, MarkerSyntax
from csm.models.drift_record import DriftRecord
from csm.models.instruction import Instruction
from csm.models.mcp_server import McpServer
from csm.models.skill import Skill
from csm.models.sync_activity import SyncActivity
from csm.models.sync_common import (
    DriftReason,
    DriftResourceType,
    SyncModule,
    SyncStatus,
)
from csm.models.sync_config import SyncConfig
from csm.modules.sync.atomic_write import atomic_write_with_hash_guard
from csm.modules.sync.bundle import BundleTooLarge, bundle_hash_from_manifest
from csm.modules.sync.cli_runner import CLIResult
from csm.modules.sync.env_expand import resolve_env_refs
from csm.modules.sync.errors import (
    ConcurrentWriteDrift,
    ExternalSkillSource,
    SyncPreflightError,
)
from csm.modules.sync.marker_block import remove_marker_block
from csm.modules.sync.skill_store import replace_skill_files
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerAgentResult:
    """One row of the `SyncEnvelope.sync` list. See spec §2 B2."""

    agent: str
    status: SyncStatus
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "status": self.status.value, "detail": self.detail}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cliresult_to_status(r: CLIResult) -> tuple[SyncStatus, str | None]:
    """Translate a CLIResult into the wire-facing (status, detail) pair."""
    if r.timed_out:
        return SyncStatus.TIMEOUT, f"exceeded {r.duration_ms}ms"
    if r.ok:
        return SyncStatus.OK, None
    # non-zero rc → first line of stderr as diagnostic; NEVER parsed further.
    detail = (r.stderr or "").strip().splitlines()[:1]
    return SyncStatus.ERROR, detail[0] if detail else f"returncode={r.returncode}"


class SyncService:
    """CRUD-driven sync for Instructions / McpServers / Skills.

    Constructed once at lifespan build; state is the DB (via sessionmaker)
    and the adapter registry. Every mutating operation records at least
    one `sync_activity` row per (agent, action, resource) tuple; drift
    situations additionally write a `drift_record` row.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        adapter_registry: AdapterRegistry,
    ) -> None:
        self._sm = sessionmaker
        self._reg = adapter_registry

    # ---- enrollment helpers -----------------------------------------

    async def _enrolled_for(self, module: SyncModule) -> list[str]:
        """Return agent names enrolled + still registered.

        Capability is NOT filtered here — per spec §8.4, an enrolled
        adapter that lacks the module's SYNC_* cap MUST surface as
        `status="unsupported"` in the envelope (silent skip is worse UX:
        the user thinks it worked). The per-module push method checks
        the capability and returns UNSUPPORTED.
        """
        async with self._sm() as session:
            row = (
                await session.execute(select(SyncConfig).where(SyncConfig.module == module.value))
            ).scalar_one_or_none()
        if row is None or not row.enabled:
            return []
        return [n for n in row.enrolled_agents if n in self._reg]

    async def _record_activity(
        self,
        *,
        module: SyncModule,
        resource_type: DriftResourceType,
        resource_id: int | None,
        agent: str,
        action: str,
        status: SyncStatus,
        duration_ms: int,
        detail: dict[str, Any] | None,
    ) -> None:
        async with self._sm() as session:
            session.add(
                SyncActivity(
                    ts=now_utc_naive(),
                    module=module.value,
                    resource_type=resource_type.value,
                    resource_id=resource_id,
                    agent=agent,
                    action=action,
                    status=status.value,
                    duration_ms=duration_ms,
                    detail_json=detail,
                )
            )
            await session.commit()

    async def _record_drift(
        self,
        *,
        module: SyncModule,
        resource_type: DriftResourceType,
        resource_id: int,
        agent: str,
        reason: DriftReason,
        expected_hash: str | None,
        actual_hash: str | None,
        detail: dict[str, Any] | None,
    ) -> None:
        async with self._sm() as session:
            session.add(
                DriftRecord(
                    ts=now_utc_naive(),
                    module=module.value,
                    resource_type=resource_type.value,
                    resource_id=resource_id,
                    agent=agent,
                    reason=reason.value,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                    detail_json=detail,
                )
            )
            await session.commit()

    # ---- v2 agent-driven dispatch ------------------------------------

    async def sync_by_type_id(
        self,
        resource_type: str,
        resource_id: int,
        target_agents: list[str],
    ) -> list[PerAgentResult]:
        """Fan-out a single (resource_type, resource_id) to `target_agents`.

        Ignores the row's own `share_scope` — the caller (SyncOrchestrator)
        has already decided who should receive this fanout based on the
        SyncAgent's decisions. Adapter capability + registry availability
        are still respected (missing agent → PerAgentResult(SKIPPED)).
        """
        if not target_agents:
            return []
        async with self._sm() as session:
            if resource_type == "instruction":
                row = await session.get(Instruction, resource_id)
            elif resource_type == "mcp_server":
                row = await session.get(McpServer, resource_id)
            elif resource_type == "skill":
                row = await session.get(Skill, resource_id)
            else:
                raise ValueError(f"unknown resource_type: {resource_type!r}")
        if row is None:
            # Row was deleted after decision-time; caller should treat
            # this as a soft-fail (see design v3 §14 "deleted_after_collect").
            return [
                PerAgentResult(
                    agent=a,
                    status=SyncStatus.SKIPPED,
                    detail="csm row deleted before fanout",
                )
                for a in target_agents
            ]
        module_map = {
            "instruction": SyncModule.MEMORY,
            "mcp_server": SyncModule.MCP,
            "skill": SyncModule.SKILLS,
        }
        drift_type_map = {
            "instruction": DriftResourceType.INSTRUCTION,
            "mcp_server": DriftResourceType.MCP_SERVER,
            "skill": DriftResourceType.SKILL,
        }
        skill_bundle: list[dict[str, Any]] = []
        skill_manifests: dict[str, Any] = {}
        if resource_type == "skill":
            skill_bundle, skill_manifests = await self._load_skill_bundle(resource_id)
        push_map = {
            "instruction": lambda ad: self._push_memory(ad, row),
            "mcp_server": lambda ad: self._push_mcp(ad, row),
            "skill": lambda ad: self._push_skill(
                ad, row, skill_bundle, skill_manifests.get(ad.name),
            ),
        }
        return await self._fanout(
            target_agents,
            module=module_map[resource_type],
            resource_type=drift_type_map[resource_type],
            resource_id=resource_id,
            action="add",
            per_agent=push_map[resource_type],
        )

    # ---- deterministic A→B migration --------------------------------

    async def migrate_agent_to_agent(
        self,
        module: SyncModule,
        source: str,
        target: str,
        names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Migrate a source agent's existing resources to a target agent.

        The user-controlled, LLM-free counterpart to the agent-tick's
        emergent AdoptToCsm→PropagateToAgent: read the source agent's
        on-disk state, upsert each item as a CSM canonical row (source is
        source-of-truth — same-named rows are overwritten with the source's
        content and the target is unioned into their scope), then fan the
        row out to `target` via `sync_by_type_id`.

        Returns one dict per item: {name, action, sync?, detail?} where
        action ∈ created | updated | skipped | unsupported | error.

        Scope by module:
        - skills : full support (SKILL.md body_md is complete on disk).
        - memory : the source's whole user-memory is packed into a single
                   instruction `migrated-from-<source>` and marker-injected
                   into the target (isolated block — does not clobber the
                   target's own memory).
        - mcp    : UNSUPPORTED — reconstructing a server needs command/url/
                   env, which `<cli> mcp list` does not expose. Define the
                   server once in CSM Resources to fan it out instead.
        """
        if source == target:
            return [
                {"name": None, "action": "error", "detail": "source and target are the same agent"}
            ]
        if source not in self._reg:
            return [{"name": None, "action": "error", "detail": f"unknown source agent {source!r}"}]
        enrolled = await self._enrolled_for(module)
        if target not in enrolled:
            return [
                {
                    "name": None,
                    "action": "error",
                    "detail": f"target {target!r} not enrolled for {module.value}",
                }
            ]

        if module is SyncModule.MCP:
            return [
                {
                    "name": None,
                    "action": "unsupported",
                    "detail": (
                        "mcp migration needs the full server definition "
                        "(command/url/env), which `mcp list` does not expose. "
                        "Define the server once via POST /api/sync/mcp/servers "
                        "with enabled_for including the target — it fans out."
                    ),
                }
            ]

        source_adapter = self._reg.get(source)
        name_filter = set(names) if names else None
        scope = {source, target}
        out: list[dict[str, Any]] = []

        if module is SyncModule.SKILLS:
            for it in source_adapter.list_skills_full():
                nm = it.get("name")
                if not nm or (name_filter and nm not in name_filter):
                    continue
                # list_skills_full deliberately omits bundle bytes (it runs
                # on every agent tick); re-read the one skill in full.
                try:
                    full_skill = source_adapter.read_skill_bundle(nm)
                except BundleTooLarge as e:
                    out.append({"name": nm, "action": "skipped", "detail": str(e)})
                    continue
                except Exception as e:
                    log.exception("read_skill_bundle failed for %s/%s", source, nm)
                    out.append({
                        "name": nm,
                        "action": "error",
                        "detail": f"{type(e).__name__}: {e}",
                    })
                    continue
                bundle = list((full_skill or {}).get("files") or [])
                skipped = list((full_skill or {}).get("skipped") or [])
                rid, action = await self._upsert_skill_row(
                    name=nm,
                    description=(it.get("description") or ""),
                    body_md=(it.get("body_md") or ""),
                    scope_agents=scope,
                    files=bundle,
                )
                results = await self.sync_by_type_id("skill", rid, [target])
                entry: dict[str, Any] = {
                    "name": nm,
                    "action": action,
                    "file_count": len(bundle),
                    "sync": envelope_sync_list(results),
                }
                # Surface omissions rather than shipping a bundle that only
                # looks complete — that failure mode is the whole reason
                # this code path exists.
                if skipped:
                    entry["skipped_files"] = skipped
                out.append(entry)
            return out

        # module is MEMORY
        full = source_adapter.read_memory_full("user") or ""
        if not full.strip():
            return [
                {
                    "name": None,
                    "action": "skipped",
                    "detail": f"source {source!r} has empty user memory",
                }
            ]
        mem_name = f"migrated-from-{source}"
        # Scope the shared instruction to the TARGET only — never the source.
        # Memory is injected as a marker block into the agent's memory FILE,
        # which on the source already holds this content (hand-written). If the
        # source were enrolled, sync would inject the block back into the
        # source's own file and duplicate it. Skills don't have this problem
        # (each is a standalone file), so their branch keeps {source, target}.
        rid, action = await self._upsert_instruction_row(
            name=mem_name,
            title=f"Memory migrated from {source}",
            body=full,
            scope_agents={target},
        )
        results = await self.sync_by_type_id("instruction", rid, [target])
        return [{"name": mem_name, "action": action, "sync": envelope_sync_list(results)}]

    async def _upsert_skill_row(
        self,
        *,
        name: str,
        description: str,
        body_md: str,
        scope_agents: set[str],
        files: list[Any] | None = None,
    ) -> tuple[int, str]:
        """Create or update a Skill row, bundle included.

        `files` accepts `BundleFile`s or plain dicts. Passing None leaves an
        existing bundle untouched — that's the "I only have the body" caller
        (adopt-from-conflict), not an instruction to empty the bundle.
        """
        async with self._sm() as session:
            existing = (
                await session.execute(select(Skill).where(Skill.name == name))
            ).scalar_one_or_none()
            if existing is None:
                row = Skill(
                    name=name,
                    description=description,
                    body_md=body_md,
                    share_scope=sorted(scope_agents),
                )
                session.add(row)
                action = "created"
            else:
                existing.description = description
                existing.body_md = body_md
                existing.share_scope = sorted(set(existing.share_scope or []) | scope_agents)
                row = existing
                action = "updated"
            if files is not None:
                await session.flush()  # row.id for the child rows
                await replace_skill_files(session, row, files)
            await session.commit()
            await session.refresh(row)
            return row.id, action

    async def _upsert_instruction_row(
        self,
        *,
        name: str,
        title: str,
        body: str,
        scope_agents: set[str],
    ) -> tuple[int, str]:
        async with self._sm() as session:
            existing = (
                await session.execute(select(Instruction).where(Instruction.name == name))
            ).scalar_one_or_none()
            if existing is None:
                row = Instruction(
                    name=name, title=title, body=body, share_scope=sorted(scope_agents), priority=0
                )
                session.add(row)
                action = "created"
            else:
                existing.title = title
                existing.body = body
                existing.share_scope = sorted(set(existing.share_scope or []) | scope_agents)
                row = existing
                action = "updated"
            await session.commit()
            await session.refresh(row)
            return row.id, action

    # ---- memory -----------------------------------------------------

    async def sync_instruction(self, instruction: Instruction) -> list[PerAgentResult]:
        """Materialise `instruction` into every enrolled+capable agent.

        Failures on one agent do NOT roll back the others (spec §2 B2 —
        partial success is a first-class outcome).
        """
        enrolled = await self._enrolled_for(SyncModule.MEMORY)
        # Intersect with the resource's own share_scope so a per-instruction
        # scope override wins over the module-level enrollment.
        scope = set(instruction.share_scope or [])
        targets = [a for a in enrolled if a in scope]
        return await self._fanout(
            targets,
            module=SyncModule.MEMORY,
            resource_type=DriftResourceType.INSTRUCTION,
            resource_id=instruction.id,
            action="add",
            per_agent=lambda ad: self._push_memory(ad, instruction),
        )

    async def remove_instruction(self, instruction: Instruction) -> list[PerAgentResult]:
        """Strip `instruction`'s marker block from every enrolled agent."""
        enrolled = await self._enrolled_for(SyncModule.MEMORY)
        scope = set(instruction.share_scope or [])
        targets = [a for a in enrolled if a in scope]
        return await self._fanout(
            targets,
            module=SyncModule.MEMORY,
            resource_type=DriftResourceType.INSTRUCTION,
            resource_id=instruction.id,
            action="remove",
            per_agent=lambda ad: self._strip_memory(ad, instruction),
        )

    async def _push_memory(
        self,
        adapter: CLIAdapter,
        instruction: Instruction,
    ) -> tuple[SyncStatus, str | None]:
        if Capability.SYNC_MEMORY not in adapter.capabilities:
            return SyncStatus.UNSUPPORTED, "adapter lacks SYNC_MEMORY"
        paths = adapter.memory_paths("user")
        if not paths:
            return SyncStatus.UNSUPPORTED, "memory_paths('user') is empty"
        target = paths[0]
        try:
            adapter.write_memory_marker_block(target, instruction.name, instruction.body)
        except ConcurrentWriteDrift as e:
            await self._record_drift(
                module=SyncModule.MEMORY,
                resource_type=DriftResourceType.INSTRUCTION,
                resource_id=instruction.id,
                agent=adapter.name,
                reason=DriftReason.CONCURRENT_WRITE,
                expected_hash=e.expected_hash,
                actual_hash=e.actual_hash,
                detail={"path": str(e.path), "pre_hash": e.pre_hash},
            )
            return SyncStatus.SKIPPED, "concurrent write drift (recorded)"
        return SyncStatus.OK, None

    async def _strip_memory(
        self,
        adapter: CLIAdapter,
        instruction: Instruction,
    ) -> tuple[SyncStatus, str | None]:
        if Capability.SYNC_MEMORY not in adapter.capabilities:
            return SyncStatus.UNSUPPORTED, "adapter lacks SYNC_MEMORY"
        paths = adapter.memory_paths("user")
        if not paths:
            return SyncStatus.UNSUPPORTED, "memory_paths('user') is empty"
        target = paths[0]
        original = adapter.read_memory(target)
        stripped = remove_marker_block(original, adapter.marker_syntax(), instruction.name)
        if stripped == original:
            # Marker wasn't there; nothing to do — count as ok (idempotent).
            return SyncStatus.OK, "marker absent"
        try:
            atomic_write_with_hash_guard(target, stripped.encode("utf-8"))
        except ConcurrentWriteDrift as e:
            await self._record_drift(
                module=SyncModule.MEMORY,
                resource_type=DriftResourceType.INSTRUCTION,
                resource_id=instruction.id,
                agent=adapter.name,
                reason=DriftReason.CONCURRENT_WRITE,
                expected_hash=e.expected_hash,
                actual_hash=e.actual_hash,
                detail={"path": str(e.path), "pre_hash": e.pre_hash},
            )
            return SyncStatus.SKIPPED, "concurrent write drift (recorded)"
        return SyncStatus.OK, None

    # ---- mcp --------------------------------------------------------

    async def sync_mcp_server(self, server: McpServer) -> list[PerAgentResult]:
        """`mcp add` on every enrolled+capable agent listed in `enabled_for`."""
        enrolled = await self._enrolled_for(SyncModule.MCP)
        targets = [a for a in enrolled if a in set(server.enabled_for or [])]
        return await self._fanout(
            targets,
            module=SyncModule.MCP,
            resource_type=DriftResourceType.MCP_SERVER,
            resource_id=server.id,
            action="add",
            per_agent=lambda ad: self._push_mcp(ad, server),
        )

    async def remove_mcp_server(self, server: McpServer) -> list[PerAgentResult]:
        enrolled = await self._enrolled_for(SyncModule.MCP)
        targets = [a for a in enrolled if a in set(server.enabled_for or [])]
        return await self._fanout(
            targets,
            module=SyncModule.MCP,
            resource_type=DriftResourceType.MCP_SERVER,
            resource_id=server.id,
            action="remove",
            per_agent=lambda ad: self._strip_mcp(ad, server),
        )

    async def _push_mcp(
        self,
        adapter: CLIAdapter,
        server: McpServer,
    ) -> tuple[SyncStatus, str | None]:
        if Capability.SYNC_MCP not in adapter.capabilities:
            return SyncStatus.UNSUPPORTED, "adapter lacks SYNC_MCP"
        try:
            resolved_env = resolve_env_refs(server.env_json or {})
        except SyncPreflightError as e:
            return SyncStatus.ERROR, f"undefined env: {e.missing}"
        try:
            r = await adapter.mcp_add(
                server.name,
                transport=server.transport,
                command=server.command,
                args=list(server.args_json or []),
                url=server.url,
                env=resolved_env,
            )
        except Exception as e:
            log.exception("mcp_add crashed for agent=%s server=%s", adapter.name, server.name)
            return SyncStatus.ERROR, f"{type(e).__name__}: {e}"
        return _cliresult_to_status(r)

    async def _strip_mcp(
        self,
        adapter: CLIAdapter,
        server: McpServer,
    ) -> tuple[SyncStatus, str | None]:
        if Capability.SYNC_MCP not in adapter.capabilities:
            return SyncStatus.UNSUPPORTED, "adapter lacks SYNC_MCP"
        try:
            r = await adapter.mcp_remove(server.name)
        except Exception as e:
            log.exception(
                "mcp_remove crashed for agent=%s server=%s",
                adapter.name,
                server.name,
            )
            return SyncStatus.ERROR, f"{type(e).__name__}: {e}"
        return _cliresult_to_status(r)

    # ---- skills -----------------------------------------------------

    async def sync_skill(
        self, skill: Skill, *, exclude_agents: set[str] | None = None
    ) -> list[PerAgentResult]:
        """Materialise `skill` on every enrolled agent in its share_scope.

        `exclude_agents` drops targets the caller knows already hold this
        content — the agent a reingest just READ from, above all. Writing a
        bundle straight back to its own source is a no-op at best; at worst it
        rewrites files CSM doesn't own (a symlinked skill-book checkout) and
        churns their permission bits for nothing.
        """
        enrolled = await self._enrolled_for(SyncModule.SKILLS)
        skip = exclude_agents or set()
        targets = [
            a for a in enrolled if a in set(skill.share_scope or []) and a not in skip
        ]
        bundle, manifests = await self._load_skill_bundle(skill.id)
        return await self._fanout(
            targets,
            module=SyncModule.SKILLS,
            resource_type=DriftResourceType.SKILL,
            resource_id=skill.id,
            action="add",
            per_agent=lambda ad: self._push_skill(
                ad, skill, bundle, manifests.get(ad.name),
            ),
        )

    async def remove_skill(self, skill: Skill) -> list[PerAgentResult]:
        enrolled = await self._enrolled_for(SyncModule.SKILLS)
        targets = [a for a in enrolled if a in set(skill.share_scope or [])]
        return await self._fanout(
            targets,
            module=SyncModule.SKILLS,
            resource_type=DriftResourceType.SKILL,
            resource_id=skill.id,
            action="remove",
            per_agent=lambda ad: self._strip_skill(ad, skill),
        )

    async def _push_skill(
        self,
        adapter: CLIAdapter,
        skill: Skill,
        bundle: list[dict[str, Any]],
        prune: dict[str, str] | None,
    ) -> tuple[SyncStatus, str | None]:
        """Materialise the whole skill directory on one agent.

        `bundle` / `prune` are passed in rather than read off `skill` because
        the caller holds them from a session that is already closed by the
        time fanout runs — lazy-loading `skill.files` here would raise on a
        detached instance.
        """
        if Capability.SYNC_SKILLS not in adapter.capabilities:
            return SyncStatus.UNSUPPORTED, "adapter lacks SYNC_SKILLS"
        try:
            result = adapter.write_skill_bundle(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "body_md": skill.body_md,
                    "files": bundle,
                    "prune": prune,
                }
            )
        except ConcurrentWriteDrift as e:
            await self._record_drift(
                module=SyncModule.SKILLS,
                resource_type=DriftResourceType.SKILL,
                resource_id=skill.id,
                agent=adapter.name,
                reason=DriftReason.CONCURRENT_WRITE,
                expected_hash=e.expected_hash,
                actual_hash=e.actual_hash,
                detail={"path": str(e.path)},
            )
            return SyncStatus.SKIPPED, "concurrent write drift (recorded)"
        except ExternalSkillSource as e:
            # The target skill dir is a symlink into content we don't own
            # (skill-book repo checkout). Writing would edit the user's
            # working tree behind their back — record and skip. This one
            # never self-heals, so it stays visible until they act on it.
            await self._record_drift(
                module=SyncModule.SKILLS,
                resource_type=DriftResourceType.SKILL,
                resource_id=skill.id,
                agent=adapter.name,
                reason=DriftReason.EXTERNAL_SOURCE,
                expected_hash=None,
                actual_hash=None,
                detail={"path": str(e.path), "symlink_target": str(e.target)},
            )
            return SyncStatus.SKIPPED, f"skill dir is a symlink to {e.target} (recorded)"
        except NotImplementedError as e:
            return SyncStatus.UNSUPPORTED, str(e)
        except Exception as e:
            return SyncStatus.ERROR, f"{type(e).__name__}: {e}"

        await self._remember_manifest(skill.id, adapter.name, bundle)
        pruned = result.get("pruned") or []
        detail = f"{len(result.get('written') or [])} file(s)"
        if pruned:
            detail += f", pruned {len(pruned)}"
        return SyncStatus.OK, detail

    async def _remember_manifest(
        self, skill_id: int, agent: str, bundle: list[dict[str, Any]]
    ) -> None:
        """Record what we just wrote, so the next push knows what to prune.

        Scoped per agent: two agents can legitimately be at different
        bundle versions when one of them was skipped.
        """
        async with self._sm() as session:
            row = await session.get(Skill, skill_id)
            if row is None:
                return
            manifest = dict(row.last_synced_files or {})
            manifest[agent] = {f["rel_path"]: f["sha256"] for f in bundle}
            row.last_synced_files = manifest
            await session.commit()

    async def _load_skill_bundle(
        self, skill_id: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read a skill's bundle rows + the per-agent manifests, eagerly.

        Returns `(files, last_synced_files)` as plain dicts so they survive
        the session closing.
        """
        async with self._sm() as session:
            row = await session.get(Skill, skill_id)
            if row is None:
                return [], {}
            files = [
                {
                    "rel_path": f.rel_path,
                    "content": f.content,
                    "mode": f.mode,
                    "sha256": f.sha256,
                }
                for f in row.files
            ]
            return files, dict(row.last_synced_files or {})

    async def _strip_skill(
        self,
        adapter: CLIAdapter,
        skill: Skill,
    ) -> tuple[SyncStatus, str | None]:
        if Capability.SYNC_SKILLS not in adapter.capabilities:
            return SyncStatus.UNSUPPORTED, "adapter lacks SYNC_SKILLS"
        try:
            adapter.remove_skill(skill.name)
        except NotImplementedError as e:
            return SyncStatus.UNSUPPORTED, str(e)
        except Exception as e:
            return SyncStatus.ERROR, f"{type(e).__name__}: {e}"
        return SyncStatus.OK, None

    # ---- fanout helper ----------------------------------------------

    async def _fanout(
        self,
        agents: list[str],
        *,
        module: SyncModule,
        resource_type: DriftResourceType,
        resource_id: int | None,
        action: str,
        per_agent,
    ) -> list[PerAgentResult]:
        """Run `per_agent(adapter)` for each agent name; record activity.

        Runs SERIALLY (not in parallel) — multi-agent CLIs share auth
        caches; concurrent invocations can produce sporadic 401s that
        aren't real errors. Serial is fine — each call has a 10s cap.
        """
        results: list[PerAgentResult] = []
        for name in agents:
            adapter = self._reg.get(name)
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                status, detail = await per_agent(adapter)
            except Exception as e:
                log.exception("per_agent crashed for agent=%s action=%s", name, action)
                status, detail = SyncStatus.ERROR, f"{type(e).__name__}: {e}"
            duration_ms = int((loop.time() - started) * 1000)
            await self._record_activity(
                module=module,
                resource_type=resource_type,
                resource_id=resource_id,
                agent=name,
                action=action,
                status=status,
                duration_ms=duration_ms,
                detail=None if detail is None else {"detail": detail},
            )
            results.append(PerAgentResult(agent=name, status=status, detail=detail))
        return results


# ============================================================================
# DriftPoller
# ============================================================================


class DriftPoller:
    """Async background task that reconciles CSM DB ⇄ on-agent state.

    Runs one tick every `tick_interval_sec` (spec Non-blocking N1 pins
    30-120s; default 60s is a compromise). Each tick iterates:

    - `memory`  : for each Instruction × enrolled agent, check the
                  marker block content matches `sha256(instruction.body)`;
                  mismatch → drift(EXTERNAL_EDIT); missing → drift(MISSING).
    - `mcp`     : `adapter.mcp_list()` diff against DB (McpServer where
                  enabled_for ∋ agent). CLI-side extra → we ignore
                  (user may run their own MCP entries); DB-side missing
                  → drift(MISSING).
    - `skills`  : `adapter.list_skills()` diff against Skill rows;
                  same semantics as mcp.

    No writes ever happen from the poller — only DriftRecord rows. Fix
    is a user-driven action from the UI: POST /api/sync/drift/{id}/resolve
    re-pushes the CSM authoritative version to the drifted agent (via
    SyncService.sync_by_type_id) and only then marks the row resolved.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        adapter_registry: AdapterRegistry,
        sync_service: SyncService,
        *,
        tick_interval_sec: float = 60.0,
    ) -> None:
        self._sm = sessionmaker
        self._reg = adapter_registry
        self._svc = sync_service
        self._tick_interval = tick_interval_sec
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="sync-drift-poller")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick_once()
            except Exception:
                log.exception("drift poller tick raised; continuing")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._tick_interval,
                )
            except TimeoutError:
                continue

    async def tick_once(self) -> None:
        """One full pass over (module, agent) pairs. Public for tests."""
        for module in SyncModule:
            enrolled = await self._svc._enrolled_for(module)
            for agent_name in enrolled:
                adapter = self._reg.get(agent_name)
                try:
                    if module is SyncModule.MEMORY:
                        await self._check_memory_drift(adapter)
                    elif module is SyncModule.MCP:
                        await self._check_mcp_drift(adapter)
                    elif module is SyncModule.SKILLS:
                        await self._check_skills_drift(adapter)
                except Exception:
                    log.exception(
                        "drift check crashed for module=%s agent=%s",
                        module.value,
                        agent_name,
                    )

    async def _check_memory_drift(self, adapter: CLIAdapter) -> None:
        paths = adapter.memory_paths("user")
        if not paths:
            return
        text = adapter.read_memory(paths[0])
        syntax = adapter.marker_syntax()

        async with self._sm() as session:
            rows = (await session.execute(select(Instruction))).scalars().all()
        for ins in rows:
            if adapter.name not in (ins.share_scope or []):
                continue
            block = _extract_marker_body(text, syntax, ins.name)
            expected_hash = _sha256(ins.body)
            if block is None:
                await self._svc._record_drift(
                    module=SyncModule.MEMORY,
                    resource_type=DriftResourceType.INSTRUCTION,
                    resource_id=ins.id,
                    agent=adapter.name,
                    reason=DriftReason.MISSING,
                    expected_hash=expected_hash,
                    actual_hash=None,
                    detail={"path": str(paths[0])},
                )
                continue
            actual_hash = _sha256(block)
            if actual_hash != expected_hash:
                await self._svc._record_drift(
                    module=SyncModule.MEMORY,
                    resource_type=DriftResourceType.INSTRUCTION,
                    resource_id=ins.id,
                    agent=adapter.name,
                    reason=DriftReason.EXTERNAL_EDIT,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                    detail={"path": str(paths[0])},
                )

    async def _check_mcp_drift(self, adapter: CLIAdapter) -> None:
        if Capability.SYNC_MCP not in adapter.capabilities:
            return
        try:
            cli_entries = await adapter.mcp_list()
        except Exception:
            log.exception("mcp_list crashed on agent=%s during drift tick", adapter.name)
            return
        cli_names = {e.get("name") for e in cli_entries if e.get("name")}

        async with self._sm() as session:
            rows = (await session.execute(select(McpServer))).scalars().all()
        for srv in rows:
            if adapter.name not in (srv.enabled_for or []):
                continue
            if srv.name in cli_names:
                continue  # present as expected
            await self._svc._record_drift(
                module=SyncModule.MCP,
                resource_type=DriftResourceType.MCP_SERVER,
                resource_id=srv.id,
                agent=adapter.name,
                reason=DriftReason.MISSING,
                expected_hash=None,
                actual_hash=None,
                detail={"cli_names_seen": sorted(str(n) for n in cli_names)},
            )

    async def _check_skills_drift(self, adapter: CLIAdapter) -> None:
        """Compare each CSM skill against the agent's copy — bundle included.

        Until 2026-08-30 this only asked whether the skill *directory*
        existed, which is why a bundle that lost every file but SKILL.md
        polled green indefinitely. We now compare a hash over SKILL.md plus
        every bundle file's (rel_path, mode, content), so a deleted helper
        script or a stripped exec bit both register.
        """
        if Capability.SYNC_SKILLS not in adapter.capabilities:
            return
        try:
            fs_entries = adapter.list_skills_full()
        except Exception:
            log.exception(
                "list_skills_full crashed on agent=%s during drift tick",
                adapter.name,
            )
            return
        by_name = {e["name"]: e for e in fs_entries if e.get("name")}

        async with self._sm() as session:
            rows = (await session.execute(select(Skill))).scalars().all()
            expected: dict[int, tuple[str, str, dict[str, Any]]] = {}
            for sk in rows:
                manifest = {
                    f.rel_path: {"sha256": f.sha256, "mode": f.mode} for f in sk.files
                }
                expected[sk.id] = (
                    sk.name,
                    bundle_hash_from_manifest(sk.body_md or "", manifest),
                    manifest,
                )

        for sk in rows:
            if adapter.name not in (sk.share_scope or []):
                continue
            name, expected_hash, manifest = expected[sk.id]
            entry = by_name.get(name)
            if entry is None:
                await self._svc._record_drift(
                    module=SyncModule.SKILLS,
                    resource_type=DriftResourceType.SKILL,
                    resource_id=sk.id,
                    agent=adapter.name,
                    reason=DriftReason.MISSING,
                    expected_hash=expected_hash,
                    actual_hash=None,
                    detail={"fs_names_seen": sorted(by_name)},
                )
                continue

            actual_hash = entry.get("bundle_hash")
            if actual_hash == expected_hash:
                continue

            # Same skill, different bytes. Name the missing files explicitly —
            # "bundle_hash differs" is not something a user can act on.
            fs_count = int(entry.get("file_count") or 0)
            detail: dict[str, Any] = {
                "expected_file_count": len(manifest),
                "agent_file_count": fs_count,
            }
            reason = (
                DriftReason.MISSING
                if fs_count < len(manifest)
                else DriftReason.EXTERNAL_EDIT
            )
            await self._svc._record_drift(
                module=SyncModule.SKILLS,
                resource_type=DriftResourceType.SKILL,
                resource_id=sk.id,
                agent=adapter.name,
                reason=reason,
                expected_hash=expected_hash,
                actual_hash=actual_hash,
                detail=detail,
            )


# ============================================================================
# helpers
# ============================================================================


def _module_capability(module: SyncModule) -> Capability:
    return {
        SyncModule.MEMORY: Capability.SYNC_MEMORY,
        SyncModule.MCP: Capability.SYNC_MCP,
        SyncModule.SKILLS: Capability.SYNC_SKILLS,
    }[module]


def _extract_marker_body(text: str, syntax: MarkerSyntax, marker_id: str) -> str | None:
    """Return the body between csm:start/csm:end for `marker_id`, or None."""
    open_line = f"{syntax.open_prefix} csm:start id={marker_id} {syntax.open_suffix}"
    close_line = f"{syntax.close_prefix} csm:end id={marker_id} {syntax.close_suffix}"
    pattern = re.compile(
        rf"{re.escape(open_line)}\n(.*?)\n{re.escape(close_line)}",
        flags=re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# JSON helpers for API-layer detail payloads (kept here so both routes and
# tests use the same shape when serializing PerAgentResult lists).
# ----------------------------------------------------------------------------


def envelope_sync_list(results: list[PerAgentResult]) -> list[dict[str, Any]]:
    return [r.as_dict() for r in results]


def envelope_warnings(results: list[PerAgentResult]) -> list[str]:
    warnings: list[str] = []
    for r in results:
        if r.status is SyncStatus.OK:
            continue
        warnings.append(f"{r.agent}: {r.status.value}" + (f" — {r.detail}" if r.detail else ""))
    return warnings


__all__ = [
    "SyncService",
    "DriftPoller",
    "PerAgentResult",
    "envelope_sync_list",
    "envelope_warnings",
]
