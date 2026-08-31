"""Workflow YAML generator — spawns `claude -p` in the user's repo,
waits for it to write a YAML file, then runs the reviewer.

Sync entry point: `generate_workflow(...)` — returns a `GenerationResult`.
Spawn method: `asyncio.create_subprocess_exec("claude", "-p", ...)` per
project decision (option A: one-shot subprocess, no Session row).

Timeout: hard 10 min. Anything past that we kill the process and return
a fail result — the user will retry with a clearer requirement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import re as _re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from csm.config import settings
from csm.modules.workflow.authoring.prompt import (
    CLARIFY_SYSTEM_APPEND,
    EDIT_SYSTEM_APPEND,
    GUIDE_HOT_LOCAL_PATH,
    GUIDE_LOCAL_PATH,
    SYSTEM_APPEND,
    TASKS_DIR,
    build_clarify_prompt,
    build_edit_prompt,
    build_generate_prompt,
)
from csm.modules.workflow.authoring.semantic_reviewer import (
    semantic_review_workflow,
)
from csm.modules.workflow.reviewer import review_workflow
from csm.modules.workflow.schema import load_workflow_spec

log = logging.getLogger(__name__)

# Hard timeout for the whole generation. Empirical: T1 workflows finish in
# ~1-2 min; T2/T3 with tool exploration ~3-5 min. 10 min gives 2x headroom.
_GENERATE_TIMEOUT_SEC = 600

# Clarify skims the repo (≤ 3 tool calls) + emits a small JSON block.
# Empirical target: ~15-40s for simple requirements. Bumped to 180s to
# cover requirements that reference a long doc/playbook the agent has to
# read before it can even ask boundary questions (90s was too tight and
# users were silently degraded to one-shot generate).
_CLARIFY_TIMEOUT_SEC = 180

# Regex that must appear as (or near) the last line of claude's output.
# The prompt tells claude to emit exactly "wrote <absolute path>".
# The legacy Chinese marker stays matchable: the authoring guides are spliced
# into the prompt verbatim and a stale copy — or a session already in flight
# when the wording changed — would otherwise produce an unparseable success.
# Derived from TASKS_DIR so the same source tree works on any machine.
_CONFIRM_RE = re.compile(
    r"(?:wrote|已写入)\s+("
    + re.escape(TASKS_DIR)
    + r"/[a-z][a-z0-9_]*\.workflow\.yaml)"
)


class GenerationError(Exception):
    """Raised when generation fails at a point the caller needs to know about
    (not just "bad YAML" — those go through the review report).
    """


@dataclass
class GenerationResult:
    """What POST /api/workflows/generate returns."""

    workflow_id: str | None            # WorkflowDefinition.id if upserted, else None
    workflow_name: str | None
    yaml_path: str | None              # absolute path of written YAML, or None
    review_status: str                 # "passed" | "rejected" | "generation_failed"
    review_report: dict[str, Any] | None
    stdout_tail: str                   # last ~4 KB of claude's stdout (for debug)
    duration_sec: float
    error: str | None = None           # human-readable failure reason
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClarifyResult:
    """What POST /api/workflows/clarify returns.

    `needs_clarify=False` means the clarify agent looked at the requirement
    and decided there's no meaningful boundary to ask about — but the
    frontend should STILL show the stage preview card if `stages` is
    non-empty (user can adjust decomposition even when no boundary
    questions need answering).
    """
    clarification_id: str | None       # None when both needs_clarify=False AND no stages
    needs_clarify: bool
    stage_preview: str                 # agent's one-line summary of how it read the requirement
    stages: list[dict[str, Any]] = field(default_factory=list)  # [{name, kind, purpose}]
    questions: list[dict[str, Any]] = field(default_factory=list)
    duration_sec: float = 0.0
    error: str | None = None
    stdout_tail: str = ""


def _localize_guide(body: str) -> str:
    """Resolve `<CSM_REPO>` in the guide body to this checkout's real path.

    The guide is spliced verbatim into the prompt handed to a spawned agent,
    and it tells that agent where to write the YAML. A literal `<CSM_REPO>`
    reaching the prompt puts a second, unresolvable path in front of the agent
    alongside the real one `prompt.py` interpolates — it either writes to a
    directory named `<CSM_REPO>` or stalls choosing.

    The token only exists in the sanitized copy of the guide published on the
    public branches; on a private checkout the guide already carries a real
    path and this is a no-op. It runs unconditionally anyway, because "is this
    the sanitized copy?" is not something this function should have to know.
    """
    return body.replace("<CSM_REPO>", str(settings.project_root))


def _fetch_guide(hot: bool = True) -> str:
    """Return the authoring guide body from the bundled repo path.

    `hot=True` returns the ~10-15KB distilled version spliced into every
    generate prompt (§0/§3/§4/§6/§7 + one real flagship example). `hot=False`
    returns the full reference; the agent reads the full guide on demand
    via its own `Read` tool from `GUIDE_LOCAL_PATH`.
    """
    local_path = GUIDE_HOT_LOCAL_PATH if hot else GUIDE_LOCAL_PATH
    local = Path(local_path)
    if not local.is_file():
        raise GenerationError(
            f"authoring guide not found at {local_path}. "
            "The guide ships with the repo under docs/; ensure your "
            "checkout is intact."
        )
    try:
        return _localize_guide(local.read_text(encoding="utf-8"))
    except OSError as e:
        raise GenerationError(
            f"cannot read authoring guide at {local_path}: {e}"
        ) from e


def _validate_repo_path(repo_path: str) -> Path:
    """Sanity-check the caller-supplied repo path.

    - Must be absolute
    - Must exist
    - Must be a directory
    - Must not be the CSM repo itself (users get confused if we allow that)

    Returns the resolved Path; raises GenerationError on any check failure.
    """
    if not repo_path or not repo_path.strip():
        raise GenerationError("repo_path is empty")
    p = Path(repo_path).expanduser()
    if not p.is_absolute():
        raise GenerationError(f"repo_path must be absolute, got: {repo_path!r}")
    try:
        p = p.resolve(strict=True)
    except (OSError, FileNotFoundError) as e:
        raise GenerationError(f"repo_path does not exist or is not accessible: {repo_path!r}: {e}") from e
    if not p.is_dir():
        raise GenerationError(f"repo_path is not a directory: {repo_path!r}")
    # Derived from settings, never hardcoded: the checkout path differs per
    # machine, and a literal would silently disable this guard everywhere but
    # the one box it was written on.
    if p == settings.project_root.resolve():
        raise GenerationError(
            "repo_path is the CSM repo itself — you want to point at the "
            "target automation repo (e.g. /data/projects/sample_pipeline), "
            "not CSM."
        )
    return p


async def _run_claude(
    *,
    prompt: str,
    cwd: Path,
    timeout_sec: int = _GENERATE_TIMEOUT_SEC,
    system_append: str = SYSTEM_APPEND,
    add_tasks_dir: bool = True,
) -> tuple[int, str, str]:
    """Spawn `claude -p <prompt> --dangerously-skip-permissions` with cwd
    set to the target repo.

    Returns (exit_code, stdout, stderr). Kills the process on timeout.

    `system_append` picks which system prompt to append (clarify vs generate);
    `add_tasks_dir` toggles the `--add-dir <TASKS_DIR>` flag (only needed for
    generate — clarify doesn't write files).
    """
    argv = [
        "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--append-system-prompt", system_append,
        "--output-format", "text",
    ]
    if add_tasks_dir:
        # Give claude access to the CSM tasks dir so it can Write files there
        # despite the target repo cwd being elsewhere.
        argv.extend(["--add-dir", TASKS_DIR])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        # Multi-agent v2 graceful degrade: workflow authoring is
        # claude-only (uses `claude -p ... --append-system-prompt`).
        # If the user's only registered adapter is codex, `claude` isn't
        # on PATH — surface a clear error instead of a raw ENOENT
        # traceback landing in the API response.
        raise GenerationError(
            "workflow authoring currently requires the `claude` CLI on "
            "PATH (uses `claude -p ... --append-system-prompt`). "
            "Codex has no equivalent flag family. Install / authenticate "
            "claude, or hand-write the workflow YAML for now."
        ) from e
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec,
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise GenerationError(
            f"claude generation timed out after {timeout_sec}s. Consider "
            f"tightening the requirement or splitting into smaller workflows."
        )
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _extract_yaml_path(stdout: str) -> str | None:
    """Grep claude's stdout for the 'wrote <path>' confirmation line.

    Returns the absolute path if found, else None. Reads the whole stdout
    (not just the last line) because claude sometimes adds trailing text
    after the confirmation.
    """
    matches = _CONFIRM_RE.findall(stdout)
    if not matches:
        return None
    # Last match wins (agent may write the marker mid-reasoning and again
    # at the end; the tail is more authoritative).
    return matches[-1]


async def _run_review_and_upsert(
    *,
    yaml_path: Path,
    sessionmaker,
    semantic_context: dict[str, Any] | None = None,
) -> tuple[str | None, str, dict[str, Any] | None, str | None]:
    """Run schema validation, structural review, upsert WorkflowDefinition row.

    Returns (workflow_id, review_status, review_report, workflow_name).
    On schema-level failure, returns (None, 'generation_failed', None, None).

    `semantic_context`, when provided as `{"requirement", "clarifications",
    "cwd"}`, triggers the Pass-2 semantic reviewer. The verdicts land in
    `review_report["semantic_verdicts"]` — a separate key from the R9-R19
    `rules` list. Semantic failures do NOT flip the top-level review_status
    (which reflects structural correctness); frontend shows them as an
    additional signal for the user to decide whether to iterate.
    """
    from sqlalchemy import select

    from csm.models import WorkflowDefinition
    from csm.modules.workflow.schema import WorkflowSchemaError

    try:
        yaml_text = yaml_path.read_text(encoding="utf-8")
    except OSError as e:
        raise GenerationError(f"cannot read generated YAML {yaml_path}: {e}") from e

    try:
        spec = load_workflow_spec(yaml_text)
    except WorkflowSchemaError as e:
        return None, "generation_failed", {"error": f"schema: {e}"}, None

    review = review_workflow(spec)
    review_report = review.to_dict()

    # Pass-2 semantic review (best-effort — never fails the generation).
    # Runs after R9-R19 so we spend the ~30-60s cost only on YAMLs that
    # parsed cleanly. Structural rejections skip Pass-2 (the user must fix
    # structure first before semantic quality is even meaningful).
    if semantic_context is not None:
        try:
            sem_result = await semantic_review_workflow(
                requirement=str(semantic_context.get("requirement", "")),
                clarifications=semantic_context.get("clarifications"),
                yaml_text=yaml_text,
                cwd=Path(semantic_context.get("cwd") or TASKS_DIR),
            )
            review_report["semantic_verdicts"] = sem_result.to_dict()
        except Exception as e:  # noqa: BLE001
            log.warning("semantic review failed (non-fatal): %s", e)
            review_report["semantic_verdicts"] = {
                "verdicts": [],
                "error": f"pass-2 semantic reviewer crashed: {e}",
                "duration_sec": 0.0,
            }

    async with sessionmaker() as db:
        # Upsert on name — mirrors WorkflowLoader behaviour.
        existing = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == spec.name)
        )
        if existing:
            existing.description = spec.description
            existing.file_path = str(yaml_path)
            existing.yaml_content = yaml_text
            existing.review_report = review_report
            existing.review_status = review.status  # type: ignore[assignment]
            from csm.utils.time import now_utc_naive
            existing.reviewed_at = now_utc_naive()
            wf_id = existing.id
        else:
            from csm.utils.time import now_utc_naive
            new = WorkflowDefinition(
                name=spec.name,
                description=spec.description,
                file_path=str(yaml_path),
                yaml_content=yaml_text,
                review_status=review.status,  # type: ignore[arg-type]
                review_report=review_report,
                reviewed_at=now_utc_naive(),
            )
            db.add(new)
            # Force the PK-default lambda to run NOW so `new.id` is populated
            # before we read it. Without this flush, `new.id` stays None
            # until commit — the row lands in the DB with a valid uuid but
            # the caller's `wf_id` variable is already None, which the
            # generate endpoint interprets as "failed" (→ HTTP 500) even
            # though the workflow was actually persisted.
            await db.flush()
            wf_id = new.id
        await db.commit()

    return wf_id, review.status, review_report, spec.name


# =====================================================================
# Round 1 — clarify
# =====================================================================

_CLARIFY_BLOCK_RE = _re.compile(
    r"<clarifications>\s*(.+?)\s*</clarifications>", _re.DOTALL
)
_JSON_FENCE_RE = _re.compile(r"```(?:json)?\s*(.+?)\s*```", _re.DOTALL)


def _extract_clarifications_json(stdout: str) -> dict[str, Any] | None:
    """Pull the JSON payload out of a <clarifications>...</clarifications> block.

    The agent is instructed to wrap the JSON in the tag; it may or may not
    also wrap it in a ```json fence. Handle both.

    Returns the parsed dict, or None if no valid block found.
    """
    m = _CLARIFY_BLOCK_RE.search(stdout)
    if not m:
        return None
    body = m.group(1).strip()
    # Strip fence if present
    fence = _JSON_FENCE_RE.search(body)
    if fence:
        body = fence.group(1).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


_STAGE_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]*$")
_VALID_STAGE_KINDS = frozenset({"claude", "poll"})


def _normalize_stages(raw_stages: Any) -> list[dict[str, Any]]:
    """Extract + sanitize the stages array from the clarify payload.

    Silently drops bad entries so a single typo doesn't wipe the whole
    skeleton. Enforces name regex, kind vocabulary, purpose non-empty.
    Caps at 8 stages to match the prompt's 3-8 guidance.
    """
    if not isinstance(raw_stages, list):
        return []
    clean: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for s in raw_stages:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        kind = str(s.get("kind") or "").strip()
        purpose = str(s.get("purpose") or "").strip()
        if not name or not _STAGE_NAME_RE.match(name):
            continue
        if kind not in _VALID_STAGE_KINDS:
            continue
        if not purpose:
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        clean.append({"name": name, "kind": kind, "purpose": purpose})
        if len(clean) >= 8:
            break
    return clean


def _normalize_clarify_payload(
    payload: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Validate + normalize the shape { stage_preview, stages[], questions[] }.

    Returns (stage_preview, stages, questions) or None if the shape is
    broken. Each question must have id/text/options; each option must
    have value/label; exactly one option must be `recommended: true`
    (we fix silently if missing by marking the first one).

    `stages` is optional — legacy agent output without the field just
    yields an empty list; frontend degrades to no-preview-card in that
    case (still safe path).
    """
    preview = str(payload.get("stage_preview", "")).strip()
    stages = _normalize_stages(payload.get("stages"))
    raw_questions = payload.get("questions", [])
    if not isinstance(raw_questions, list):
        return None
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        text = str(q.get("text") or "").strip()
        opts = q.get("options")
        if not qid or not text or not isinstance(opts, list) or not opts:
            continue
        if qid in seen_ids:
            # dedupe: skip repeats
            continue
        seen_ids.add(qid)
        clean_opts: list[dict[str, Any]] = []
        for o in opts:
            if not isinstance(o, dict):
                continue
            val = str(o.get("value") or "").strip()
            lbl = str(o.get("label") or "").strip()
            if not val or not lbl:
                continue
            clean_opts.append({
                "value": val,
                "label": lbl,
                "recommended": bool(o.get("recommended", False)),
            })
        if not clean_opts:
            continue
        # Ensure exactly one recommended (default to first if none marked).
        rec_count = sum(1 for o in clean_opts if o["recommended"])
        if rec_count == 0:
            clean_opts[0]["recommended"] = True
        elif rec_count > 1:
            first_seen = False
            for o in clean_opts:
                if o["recommended"] and not first_seen:
                    first_seen = True
                    continue
                o["recommended"] = False
        normalized.append({
            "id": qid,
            "text": text,
            "options": clean_opts,
        })
        if len(normalized) >= 5:
            break  # hard cap 5
    return preview, stages, normalized


async def clarify_workflow(
    *,
    repo_path: str,
    requirement: str,
    workflow_name: str | None,
    cache,  # ClarificationCache
    timeout_sec: int = _CLARIFY_TIMEOUT_SEC,
) -> ClarifyResult:
    """Round-1 entry — spawn claude briefly, get boundary questions.

    On any failure (validation, timeout, unparseable output) we return
    `needs_clarify=False` so the frontend cleanly degrades to the one-shot
    generate path. The error is surfaced in `error` for the UI to show but
    doesn't block progress.
    """
    t0 = time.monotonic()

    try:
        repo = _validate_repo_path(repo_path)
    except GenerationError as e:
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview="", questions=[],
            duration_sec=time.monotonic() - t0, error=str(e),
        )
    if not requirement or not requirement.strip():
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview="", questions=[],
            duration_sec=time.monotonic() - t0,
            error="requirement is empty",
        )

    prompt = build_clarify_prompt(
        requirement=requirement.strip(), workflow_name=workflow_name,
    )

    log.info("workflow-clarify: spawning claude in %s (req=%s...)",
             repo, requirement[:80])
    try:
        exit_code, stdout, stderr = await _run_claude(
            prompt=prompt, cwd=repo, timeout_sec=timeout_sec,
            system_append=CLARIFY_SYSTEM_APPEND, add_tasks_dir=False,
        )
    except GenerationError as e:
        # Includes timeout ("claude generation timed out after Xs").
        log.warning(
            "workflow-clarify: DEGRADED-TO-GENERATE (subprocess failed after %.1fs) — %s",
            time.monotonic() - t0, e,
        )
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview="", questions=[],
            duration_sec=time.monotonic() - t0, error=str(e),
        )
    stdout_tail = stdout[-4096:] if stdout else ""

    if exit_code != 0:
        log.warning(
            "workflow-clarify: DEGRADED-TO-GENERATE (claude exit=%d after %.1fs)",
            exit_code, time.monotonic() - t0,
        )
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview="", questions=[],
            duration_sec=time.monotonic() - t0,
            stdout_tail=stdout_tail,
            error=f"claude clarify exit code {exit_code}; stderr tail: {stderr[-512:]!r}",
        )

    payload = _extract_clarifications_json(stdout)
    if payload is None:
        log.warning(
            "workflow-clarify: DEGRADED-TO-GENERATE (no <clarifications> JSON block in stdout, %.1fs)",
            time.monotonic() - t0,
        )
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview="", questions=[],
            duration_sec=time.monotonic() - t0,
            stdout_tail=stdout_tail,
            error="clarify agent did not emit a parseable <clarifications> JSON block; "
                  "falling back to one-shot generate",
        )
    normalized = _normalize_clarify_payload(payload)
    if normalized is None:
        log.warning(
            "workflow-clarify: DEGRADED-TO-GENERATE (payload shape invalid, %.1fs)",
            time.monotonic() - t0,
        )
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview="", stages=[], questions=[],
            duration_sec=time.monotonic() - t0,
            stdout_tail=stdout_tail,
            error="clarify payload shape invalid; falling back to one-shot generate",
        )
    stage_preview, stages, questions = normalized

    # A cache entry is worth creating IF either there are questions to answer
    # OR the agent gave us a stage skeleton the user might want to adjust.
    # An empty questions AND empty stages means legacy pass-through — treat
    # as no-clarify and let the frontend skip to one-shot generate.
    if not questions and not stages:
        log.info(
            "workflow-clarify: NO-QUESTIONS-OR-STAGES (agent judged workflow linear, %.1fs) preview=%s",
            time.monotonic() - t0, stage_preview[:80],
        )
        return ClarifyResult(
            clarification_id=None, needs_clarify=False,
            stage_preview=stage_preview, stages=[], questions=[],
            duration_sec=time.monotonic() - t0,
            stdout_tail=stdout_tail,
        )

    cid = cache.put(
        requirement=requirement.strip(),
        workflow_name=workflow_name,
        repo_path=str(repo),
        questions=questions,
        stage_preview=stage_preview,
        stages=stages,
    )
    log.info(
        "workflow-clarify: OK — %d question(s), %d stage(s) in %.1fs (clarification_id=%s)",
        len(questions), len(stages), time.monotonic() - t0, cid,
    )
    return ClarifyResult(
        clarification_id=cid,
        # `needs_clarify` really means "the UI should show the clarify screen"
        # — true when there are questions to answer OR a skeleton to review.
        needs_clarify=bool(questions or stages),
        stage_preview=stage_preview, stages=stages, questions=questions,
        duration_sec=time.monotonic() - t0,
        stdout_tail=stdout_tail,
    )


# =====================================================================
# Round 2 — generate
# =====================================================================


async def generate_workflow(
    *,
    repo_path: str,
    requirement: str,
    workflow_name: str | None = None,
    sessionmaker,
    timeout_sec: int = _GENERATE_TIMEOUT_SEC,
    clarifications: list[dict[str, Any]] | None = None,
    confirmed_stages: list[dict[str, Any]] | None = None,
) -> GenerationResult:
    """Main entry — spawns claude, runs review, returns result.

    This is a **long-running** coroutine (may block up to `timeout_sec`).
    Callers should not await it inside a request handler that has its own
    tight timeout — the FastAPI endpoint uses a larger keep-alive.

    `clarifications`, if set, is a list of `{question, answer_label, free_text}`
    dicts spliced into the generate prompt so the agent bakes those
    boundary decisions into the YAML.

    `confirmed_stages`, if set, locks the stage decomposition — the agent
    is told exactly which stages to fill and cannot invent new ones. This
    is the "user pre-approved the skeleton" contract from the clarify
    screen's stage preview card.
    """
    t0 = time.monotonic()

    # 1. Validate inputs
    try:
        repo = _validate_repo_path(repo_path)
    except GenerationError as e:
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error=str(e),
        )
    if not requirement or not requirement.strip():
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error="requirement is empty — describe what you want to automate",
        )

    # 2. Fetch guide, build prompt
    try:
        guide_body = _fetch_guide()
    except GenerationError as e:
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error=str(e),
        )
    prompt = build_generate_prompt(
        requirement=requirement.strip(),
        workflow_name=workflow_name,
        guide_body=guide_body,
        clarifications=clarifications,
        confirmed_stages=confirmed_stages,
    )

    # 3. Spawn claude
    log.info("workflow-gen: spawning claude in %s (requirement=%s...)",
             repo, requirement[:80])
    try:
        exit_code, stdout, stderr = await _run_claude(
            prompt=prompt, cwd=repo, timeout_sec=timeout_sec,
        )
    except GenerationError as e:
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error=str(e),
        )

    stdout_tail = stdout[-4096:] if stdout else ""

    if exit_code != 0:
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=f"claude exit code {exit_code}. stderr tail: {stderr[-1024:]!r}",
        )

    # 4. Extract YAML path from stdout
    yaml_path_str = _extract_yaml_path(stdout)
    if yaml_path_str is None:
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error="claude did not emit the 'wrote <path>' confirmation line. "
                  "This usually means the agent got confused mid-generation — "
                  "check stdout_tail for its last actions and retry with a "
                  "clearer requirement.",
        )
    yaml_path = Path(yaml_path_str)
    if not yaml_path.is_file():
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=yaml_path_str,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=f"claude claims to have written {yaml_path_str} but the file does not exist",
        )

    # 5. Run review + upsert (with Pass-2 semantic review context)
    try:
        wf_id, status, report, wf_name = await _run_review_and_upsert(
            yaml_path=yaml_path, sessionmaker=sessionmaker,
            semantic_context={
                "requirement": requirement.strip(),
                "clarifications": clarifications,
                "cwd": repo,
            },
        )
    except GenerationError as e:
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=yaml_path_str,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=str(e),
        )

    return GenerationResult(
        workflow_id=wf_id,
        workflow_name=wf_name,
        yaml_path=yaml_path_str,
        review_status=status,
        review_report=report,
        stdout_tail=stdout_tail,
        duration_sec=time.monotonic() - t0,
    )


# =====================================================================
# Edit — user gives natural-language feedback on an existing workflow,
# agent reads current YAML + splices feedback + writes new version back.
# =====================================================================


async def _fetch_last_failure_signal(
    workflow_id: str, sessionmaker
) -> dict[str, Any] | None:
    """Look up the workflow's most-recent FAILED mission (if any) and
    package a compact signal the edit prompt can splice.

    Returns None when the workflow has no missions or none have failed —
    the common case for a freshly generated workflow. When something did
    fail we return `{failed_stage, failure_reason, stdout_tail, triggered_rule}`.

    `stdout_tail` and `triggered_rule` are best-effort — the audit_log
    format has drifted over time; this function tolerates missing fields.
    """
    from sqlalchemy import desc, select

    from csm.models import Mission
    from csm.models.mission import MissionStatus

    async with sessionmaker() as db:
        row = await db.scalar(
            select(Mission)
            .where(
                Mission.workflow_def_id == workflow_id,
                Mission.status == MissionStatus.FAILED,
            )
            .order_by(desc(Mission.ended_at), desc(Mission.created_at))
            .limit(1)
        )
    if row is None:
        return None
    # audit_log format (best-effort): a list of {ts, stage, event, detail}
    # entries. We surface the tail (last ~40 entries → ~400 lines of text)
    # so the agent can see the actual crash context rather than just
    # `failure_reason` (which is often just "stage foo failed").
    tail_lines: list[str] = []
    triggered_rule = ""
    audit = row.audit_log or []
    if isinstance(audit, list):
        for entry in audit[-40:]:
            if not isinstance(entry, dict):
                continue
            ts = str(entry.get("ts") or "")[:19]
            stage = str(entry.get("stage") or "")
            event = str(entry.get("event") or "")
            detail = str(entry.get("detail") or "")[:200]
            tail_lines.append(f"[{ts}] {stage} · {event} — {detail}")
            # Sniff out rule id from failure detail (R9/R10/... or F1..F9)
            if not triggered_rule:
                for token in ("R9", "R10", "R11", "R12", "R13", "R14", "R15", "R16",
                              "R17", "R18", "R19", "F1", "F2", "F3", "F4", "F5", "F6",
                              "F7", "F8", "F9"):
                    if token in detail or token in event:
                        triggered_rule = token
                        break
    return {
        "failed_stage": row.current_stage or "",
        "failure_reason": row.failure_reason or "",
        "stdout_tail": "\n".join(tail_lines),
        "triggered_rule": triggered_rule,
    }


async def edit_workflow(
    *,
    workflow_name: str,
    feedback: str,
    sessionmaker,
    timeout_sec: int = _GENERATE_TIMEOUT_SEC,
) -> GenerationResult:
    """Iterate on an EXISTING workflow YAML via natural-language feedback.

    Differences from `generate_workflow`:
    - No repo_path input — cwd is the CSM tasks dir (only edits YAML).
    - Loads the current YAML from the DB row (source of truth) instead
      of asking the agent to fetch.
    - **No guide inlined** — the agent Reads GUIDE_HOT_LOCAL_PATH on demand
      (or the full GUIDE_LOCAL_PATH for section-level detail). This trims
      20-30s off each iteration since ~90% of edits don't need the guide
      at all.
    - Preserves workflow name (agent is told not to rename).
    - Auto-splices the workflow's most-recent FAILED mission signal so
      the agent knows WHY the last run failed without the user having to
      copy/paste error tails into the feedback box.

    Returns the same GenerationResult shape as generate_workflow so the
    frontend UI can reuse the verdict card / fix flow.
    """
    from sqlalchemy import select

    from csm.models import WorkflowDefinition

    t0 = time.monotonic()

    if not workflow_name or not workflow_name.strip():
        return GenerationResult(
            workflow_id=None, workflow_name=None, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error="workflow_name is empty",
        )
    if not feedback or not feedback.strip():
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error="feedback is empty — describe what to change",
        )

    # 1. Load current YAML from DB
    async with sessionmaker() as db:
        existing = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == workflow_name)
        )
    if existing is None:
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error=f"workflow {workflow_name!r} not found",
        )
    current_yaml = existing.yaml_content or ""

    # 2. Best-effort last-failure lookup (not fatal — most edits happen
    # before the workflow has ever launched, or after a successful run).
    last_failure = None
    try:
        last_failure = await _fetch_last_failure_signal(existing.id, sessionmaker)
    except Exception as e:  # noqa: BLE001
        log.warning("workflow-edit: last-failure lookup failed (non-fatal): %s", e)

    prompt = build_edit_prompt(
        workflow_name=workflow_name,
        current_yaml=current_yaml,
        feedback=feedback.strip(),
        last_failure=last_failure,
    )

    # 3. Spawn claude with cwd=TASKS_DIR (per user decision: edit is pure
    # YAML iteration, agent doesn't need to re-crawl the target repo).
    log.info("workflow-edit: spawning claude for %s (feedback=%s...)",
             workflow_name, feedback[:80])
    try:
        exit_code, stdout, stderr = await _run_claude(
            prompt=prompt,
            cwd=Path(TASKS_DIR),
            timeout_sec=timeout_sec,
            system_append=EDIT_SYSTEM_APPEND,
            add_tasks_dir=False,  # cwd IS tasks dir already
        )
    except GenerationError as e:
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail="", duration_sec=time.monotonic() - t0,
            error=str(e),
        )
    stdout_tail = stdout[-4096:] if stdout else ""
    if exit_code != 0:
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=f"claude edit exit code {exit_code}. stderr tail: {stderr[-1024:]!r}",
        )

    # 4. Extract YAML path from stdout (the confirm line)
    yaml_path_str = _extract_yaml_path(stdout)
    if yaml_path_str is None:
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=None,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error="edit agent did not emit the 'wrote <path>' confirmation line",
        )
    # Guard against rename: the emitted path must match the workflow we
    # asked the agent to edit.
    expected = f"{TASKS_DIR}/{workflow_name}.workflow.yaml"
    if yaml_path_str != expected:
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=yaml_path_str,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=f"agent wrote to {yaml_path_str!r} but this edit was for "
                  f"{workflow_name!r} (expected {expected!r}); refusing to upsert",
        )
    yaml_path = Path(yaml_path_str)
    if not yaml_path.is_file():
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=yaml_path_str,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=f"agent claims to have written {yaml_path_str} but the file does not exist",
        )

    # 5. Review + upsert (mirrors generate_workflow). Edit path also runs
    # semantic review — but with less context since we don't have the
    # original clarifications handy. The reviewer still runs on the new
    # YAML + the feedback text as best-effort requirement proxy.
    try:
        wf_id, status, report, wf_name = await _run_review_and_upsert(
            yaml_path=yaml_path, sessionmaker=sessionmaker,
            semantic_context={
                "requirement": f"[edit-round] {feedback.strip()}",
                "clarifications": None,
                "cwd": Path(TASKS_DIR),
            },
        )
    except GenerationError as e:
        return GenerationResult(
            workflow_id=None, workflow_name=workflow_name, yaml_path=yaml_path_str,
            review_status="generation_failed", review_report=None,
            stdout_tail=stdout_tail, duration_sec=time.monotonic() - t0,
            error=str(e),
        )
    return GenerationResult(
        workflow_id=wf_id,
        workflow_name=wf_name,
        yaml_path=yaml_path_str,
        review_status=status,
        review_report=report,
        stdout_tail=stdout_tail,
        duration_sec=time.monotonic() - t0,
    )
