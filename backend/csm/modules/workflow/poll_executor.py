"""Poll-stage executor for the workflow Mission orchestrator (M8 night2 T5).

A `poll` stage doesn't spawn a claude session: it just runs a `check:` block
on a fixed cadence until the check passes (advance) or `max_attempts` is
exhausted (fail the mission). This module owns those asyncio loops.

Three forms of check entry are supported, mirroring `PollCheckBlock` in
the schema (after T5's `command` addition):

- **shell-exec** — `command: list[str]`; passes iff subprocess exits 0.
- **primitive** — `file: ..., primitives: [...]`; renders the path template
  with the live mission's workspace / params / prior outputs, then applies
  the T7 primitives.
- **load-binding** — `{file, load_as: json|text, extract_field, as}`; reads
  the rendered path, extracts a field, and binds it as a mission parameter
  visible to **later checks in the same iteration** and to **downstream
  stages**. The sample_experiment `wait_train` stage relies on this so
  `{exp_path}` resolves for the subsequent `jsonschema` check on
  `{exp_path}/train_log/report.json`. Bindings from a **passing** poll
  iteration are persisted to `mission.parameters`; bindings from a partial
  or failing iteration are discarded.

Out of scope for T5 (per night_plan):
- 30s cron rescuer for orphaned stage Runs (T6)
- Mission-level timeout / startup reap (T7)

The executor is wired into `WorkflowOrchestrator` and started / stopped
alongside it. Callbacks (`on_pass` / `on_fail`) call back into the
orchestrator's advancement / failure paths so the poll executor itself
does not need to know how missions transition.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import math
import os
import re as _re
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.config import settings
from csm.models import Mission, Output, Run, WorkflowDefinition
from csm.modules.workflow.engine import TemplateRenderError, _render_template
from csm.modules.workflow.primitives import (
    CheckResult,
    check_contains_substring,
    check_file_exists,
    check_jsonschema,
    check_min_chars,
    check_regex_match,
    check_required_sections,
)
from csm.modules.workflow.schema import (
    PollCheckBlock,
    StageSpec,
    load_workflow_spec,
)

log = logging.getLogger(__name__)

# (callable, list of pydantic attr names → positional args to the primitive).
# Mirrors `engine._PRIMITIVE_DISPATCH` but reads attrs off the pydantic model
# instead of a compiled dict — we keep the live `StageSpec` because tests
# (and the orchestrator) pass it directly without re-compiling.
_PRIMITIVE_DISPATCH: dict[str, tuple[Any, tuple[str, ...]]] = {
    "file_exists": (check_file_exists, ()),
    "min_chars": (check_min_chars, ("count",)),
    "required_sections": (check_required_sections, ("sections",)),
    "regex_match": (check_regex_match, ("pattern",)),
    "jsonschema": (check_jsonschema, ("json_schema",)),
    "contains_substring": (check_contains_substring, ("text",)),
}


PollPassCallback = Callable[[str, str], Awaitable[None]]
PollFailCallback = Callable[[str, str, str], Awaitable[None]]
SleepCallable = Callable[[float], Awaitable[None]]


class PollExecutor:
    """Run periodic check loops for poll-kind workflow stages.

    One asyncio.Task per `(mission_id, stage_name)` is held in `_tasks`. The
    task is removed from `_tasks` only inside the loop's `finally` block, so
    a `start_poll` immediately after `cancel_poll` is safe (cancellation
    awaits the task before returning).
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        *,
        on_pass: PollPassCallback,
        on_fail: PollFailCallback,
        default_interval_sec: float = 60.0,
        default_max_attempts: int = 1440,
        sleep: SleepCallable | None = None,
    ):
        self._sm = sessionmaker
        self._on_pass = on_pass
        self._on_fail = on_fail
        self._default_interval_sec = float(default_interval_sec)
        self._default_max_attempts = default_max_attempts
        self._sleep: SleepCallable = sleep if sleep is not None else asyncio.sleep
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_poll(
        self, mission_id: str, stage_name: str, stage_spec: StageSpec
    ) -> asyncio.Task:
        """Spawn the loop for `(mission_id, stage_name)`.

        Returns the asyncio.Task so callers (especially tests) can `await`
        it to completion. Calling `start_poll` for a key that already has a
        live task no-ops and returns the existing task — the orchestrator
        treats restart as idempotent so a duplicate SESSION_ENDED event
        can't spawn two loops.
        """
        key = (mission_id, stage_name)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            log.warning(
                "poll already running for mission=%s stage=%s; ignoring start",
                mission_id,
                stage_name,
            )
            return existing
        task = asyncio.create_task(
            self._poll_loop(mission_id, stage_name, stage_spec),
            name=f"poll:{mission_id}:{stage_name}",
        )
        self._tasks[key] = task
        return task

    async def cancel_poll(self, mission_id: str, stage_name: str) -> None:
        """Cancel the loop for `(mission_id, stage_name)` if any, then await it."""
        key = (mission_id, stage_name)
        task = self._tasks.pop(key, None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(
                "poll task raised during cancel mission=%s stage=%s",
                mission_id,
                stage_name,
            )

    async def cancel_all_for_mission(self, mission_id: str) -> None:
        keys = [k for k in list(self._tasks.keys()) if k[0] == mission_id]
        for k in keys:
            await self.cancel_poll(*k)

    async def stop(self) -> None:
        """Cancel every live poll. Safe to call multiple times."""
        for key in list(self._tasks.keys()):
            await self.cancel_poll(*key)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_max_attempts(self, stage_spec: StageSpec) -> int:
        """Cap on iterations.

        If the stage declares `timeout`, derive from `ceil(timeout /
        poll_interval)` so the loop honours the YAML's wall-clock budget.
        Otherwise fall back to `default_max_attempts`.
        """
        interval = stage_spec.poll_interval or int(self._default_interval_sec)
        if stage_spec.timeout is not None and interval > 0:
            return max(1, math.ceil(stage_spec.timeout / interval))
        return self._default_max_attempts

    async def _poll_loop(
        self,
        mission_id: str,
        stage_name: str,
        stage_spec: StageSpec,
    ) -> None:
        interval = float(stage_spec.poll_interval or self._default_interval_sec)
        max_attempts = self._compute_max_attempts(stage_spec)
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    passed, reason = await self._run_all_checks(
                        mission_id, stage_spec
                    )
                except Exception as e:
                    log.exception(
                        "poll iteration raised mission=%s stage=%s attempt=%d",
                        mission_id,
                        stage_name,
                        attempt,
                    )
                    passed, reason = False, f"check raised: {e}"

                if passed:
                    log.info(
                        "poll passed mission=%s stage=%s attempt=%d/%d",
                        mission_id,
                        stage_name,
                        attempt,
                        max_attempts,
                    )
                    await self._on_pass(mission_id, stage_name)
                    return

                if attempt >= max_attempts:
                    msg = (
                        f"poll stage {stage_name!r} exceeded max_attempts={max_attempts} "
                        f"(last failure: {reason or 'check not satisfied'})"
                    )
                    log.warning(
                        "poll failed mission=%s stage=%s: %s",
                        mission_id,
                        stage_name,
                        msg,
                    )
                    await self._on_fail(mission_id, stage_name, msg)
                    return

                await self._sleep(interval)
        except asyncio.CancelledError:
            log.info(
                "poll cancelled mission=%s stage=%s after attempt=%d",
                mission_id,
                stage_name,
                attempt,
            )
            raise
        finally:
            self._tasks.pop((mission_id, stage_name), None)

    async def _run_all_checks(
        self, mission_id: str, stage_spec: StageSpec
    ) -> tuple[bool, str]:
        """Run every check entry; True iff every one passes.

        Returns the first failing entry's reason so the audit log captures
        WHICH check held the loop up (rather than a generic 'check failed').

        Load-binding entries mutate a **local** copy of params so later
        entries in the same iteration see the binding; on final pass
        (all entries green) the new bindings are persisted to
        `mission.parameters` so downstream stages and subsequent poll
        iterations inherit them.
        """
        ctx = await self._load_render_context(mission_id)
        if ctx is None:
            return False, "mission or workflow missing"
        workspace_path, params, prior_outputs = ctx

        live_params = dict(params)
        new_bindings: dict[str, Any] = {}

        for cb in stage_spec.check or []:
            if cb.bind_as is not None:
                ok, reason, value = await self._run_load_binding(
                    cb, workspace_path, live_params, prior_outputs
                )
                if not ok:
                    return False, reason
                live_params[cb.bind_as] = value
                new_bindings[cb.bind_as] = value
                continue
            ok, reason = await self._run_one_check(
                cb, workspace_path, live_params, prior_outputs
            )
            if not ok:
                return False, reason

        if new_bindings:
            await self._persist_bindings(mission_id, new_bindings)
        return True, ""

    async def _run_one_check(
        self,
        block: PollCheckBlock,
        workspace_path: str,
        params: dict[str, Any],
        prior_outputs: dict[str, list[str]],
    ) -> tuple[bool, str]:
        # Shell-exec form: subprocess + exit code.
        if block.command is not None:
            return await self._run_shell_check(block.command)

        # Load-binding form is dispatched separately by _run_all_checks so
        # the extracted value can flow into `live_params`. Reaching here
        # with `primitives is None` means an under-specified entry.
        if block.primitives is None:
            return True, ""

        # Primitive form: render path, apply each primitive in order.
        try:
            rendered_path = _render_template(
                block.file or "", workspace_path, params, prior_outputs
            )
        except TemplateRenderError as e:
            return False, f"check path render failed: {e}"

        for prim in block.primitives:
            ok, reason = self._run_primitive(prim, rendered_path)
            if not ok:
                return False, reason
        return True, ""

    async def _run_load_binding(
        self,
        block: PollCheckBlock,
        workspace_path: str,
        params: dict[str, Any],
        prior_outputs: dict[str, list[str]],
    ) -> tuple[bool, str, Any]:
        """Read + parse + extract a value from `block.file`.

        Returns `(passed, reason, extracted_value)`. `extracted_value` is
        meaningful only when `passed` is True.

        Path resolution follows the same template contract as primitive
        checks — the file path may reference `{params.X}`, `{ws}`, and
        `{stages.X.outputs[N]}`. This lets a workflow chain bindings
        across iterations (a later poll pass sees earlier-iteration
        bindings via `mission.parameters`).

        For `load_as: json`, `extract_field` is a **dotted path** into the
        parsed structure (e.g. `metrics.exp_path`). Bracket indexing is
        not yet supported — MVP sample YAML doesn't need it; will add if a
        real workflow hits the wall.

        For `load_as: text`, `extract_field` is a regex applied with
        `re.search`; the first capture group (or the whole match when the
        pattern has no groups) is bound. If the pattern is absent, the
        binding is the entire file contents, stripped.
        """
        try:
            rendered_path = _render_template(
                block.file or "", workspace_path, params, prior_outputs
            )
        except TemplateRenderError as e:
            return False, f"binding file render failed: {e}", None

        p = Path(rendered_path)
        if not p.exists():
            return False, f"binding file {rendered_path!r} does not exist", None
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, f"binding file read failed for {rendered_path!r}: {e}", None

        if block.load_as == "json":
            try:
                data = _json.loads(content)
            except _json.JSONDecodeError as e:
                return (
                    False,
                    f"binding file {rendered_path!r} is not valid JSON: {e}",
                    None,
                )
            value = _extract_dotted_path(data, block.extract_field or "")
            if value is None:
                return (
                    False,
                    (
                        f"binding path {block.extract_field!r} not found in "
                        f"{rendered_path!r}"
                    ),
                    None,
                )
            return True, "", value

        if block.load_as == "text":
            if not block.extract_field:
                return True, "", content.strip()
            try:
                match = _re.search(block.extract_field, content)
            except _re.error as e:
                return (
                    False,
                    f"binding extract_field {block.extract_field!r} is not a valid regex: {e}",
                    None,
                )
            if match is None:
                return (
                    False,
                    (
                        f"regex {block.extract_field!r} did not match content of "
                        f"{rendered_path!r}"
                    ),
                    None,
                )
            return True, "", (match.group(1) if match.groups() else match.group(0))

        return False, f"unknown load_as {block.load_as!r}", None

    async def _persist_bindings(
        self, mission_id: str, new_bindings: dict[str, Any]
    ) -> None:
        """Merge `new_bindings` into `mission.parameters`.

        Called only after every check entry in an iteration has passed so
        we never leak a partial binding into a mission that ultimately
        fails on a later check. JSON column mutation must reassign the
        attribute for SQLAlchemy to track the change.
        """
        async with self._sm() as db:
            mission = await db.get(Mission, mission_id)
            if mission is None:
                return
            merged = dict(mission.parameters or {})
            merged.update(new_bindings)
            mission.parameters = merged
            await db.commit()

    @staticmethod
    async def _run_shell_check(command: list[str]) -> tuple[bool, str]:
        """Run an external command; pass iff exit code is 0.

        Stdout/stderr are captured but not echoed — keeps the orchestrator
        log clean. The exit code goes in the failure reason so an operator
        can re-run the command by hand to diagnose.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            return False, f"failed to spawn {command!r}: {e}"
        try:
            rc = await asyncio.wait_for(
                proc.wait(),
                timeout=settings.workflow_shell_check_timeout_sec,
            )
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            except Exception:
                log.exception("failed to terminate timed-out workflow check %r", command)
            return (
                False,
                f"command {command!r} timed out after "
                f"{settings.workflow_shell_check_timeout_sec:g}s",
            )
        if rc == 0:
            return True, ""
        return False, f"command {command!r} exited with code {rc}"

    @staticmethod
    def _run_primitive(prim: Any, path: str) -> tuple[bool, str]:
        name = getattr(prim, "primitive", None)
        if name not in _PRIMITIVE_DISPATCH:
            return False, f"unknown primitive {name!r}"
        func, arg_keys = _PRIMITIVE_DISPATCH[name]
        try:
            args = [getattr(prim, k) for k in arg_keys]
        except AttributeError as e:
            return False, f"primitive {name!r} missing required arg: {e}"
        result: CheckResult = func(path, *args)
        return result.passed, result.reason

    async def _load_render_context(
        self, mission_id: str
    ) -> tuple[str, dict[str, Any], dict[str, list[str]]] | None:
        """Load workspace/params/prior outputs needed to render path templates.

        Returns None if the mission row (or its workflow row) is missing —
        callers treat that as "can't check, count as failing iteration"
        rather than crashing the loop.
        """
        async with self._sm() as db:
            mission = await db.get(Mission, mission_id)
            if mission is None:
                return None
            wf_row = await db.get(WorkflowDefinition, mission.workflow_def_id)
            if wf_row is None:
                return None
            spec = load_workflow_spec(wf_row.yaml_content)
            stage_names = [s.name for s in spec.stages]
            current = mission.current_stage
            try:
                upto = stage_names.index(current) if current else len(stage_names)
            except ValueError:
                upto = len(stage_names)
            prior = stage_names[:upto]

            prior_outputs: dict[str, list[str]] = {}
            if prior:
                rows = (
                    await db.execute(
                        select(Run.stage_name, Output.path)
                        .join(Output, Output.run_id == Run.id)
                        .where(
                            Run.mission_id == mission_id,
                            Run.stage_name.in_(prior),
                        )
                        .order_by(Output.discovered_at)
                    )
                ).all()
                for sn, p in rows:
                    prior_outputs.setdefault(sn, []).append(p)
            return (
                mission.workspace_path,
                dict(mission.parameters or {}),
                prior_outputs,
            )


def _extract_dotted_path(data: Any, path: str) -> Any:
    """Traverse `data` via a dotted path like ``metrics.exp_path``.

    Returns None when any segment is missing or the intermediate value
    isn't a dict. Bracket indexing is intentionally not supported yet —
    the sample_experiment YAML only needs flat dotted paths, and adding
    array support without a driving use case invites bad edge-case
    handling.
    """
    if not path:
        return None
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
