"""Agent-generated check script generator.

Called once at rule-creation time (POST /api/tokens/agent-alerts/generate):
  1. Build a system prompt that pins the function signature + names the window
     dict fields the script may reference.
  2. Spawn `claude -p <prompt> --dangerously-skip-permissions`, capture stdout.
  3. Extract the Python snippet between `<script>…</script>` markers.
  4. Dry-run the script against the current 5h window via sandbox.
  5. Return `(script, dry_run: SandboxResult)` for the UI to preview.

The user then decides to save or discard. Nothing hits the DB from here.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from csm.config import settings
from csm.modules.token.agent_alert.sandbox import SandboxResult, run_check_script

log = logging.getLogger(__name__)

# Empirical: pure-python-code generation is faster than YAML because there's
# less structural exploration — 60s covers >99% of cases.
_GENERATE_TIMEOUT_SEC = 120


_WINDOW_FIELDS = """\
window['msg_count']                : int   assistant messages in the window
window['input_tokens']             : int   raw input tokens (non-cached)
window['cache_creation_tokens']    : int   tokens spent writing prompt cache
window['cache_read_tokens']        : int   tokens read from prompt cache
window['output_tokens']            : int   assistant output tokens
window['total_tokens']             : int   sum of the four token buckets
window['estimated_cost_usd']       : float dollar estimate
window['window_hours']             : float window length (usually 5.0)
window['top_session_id']           : str | None  session id with the most tokens in the window
window['top_session_tokens']       : int   tokens the top session consumed
window['top_session_share']        : float top session's share of total (0.0-1.0)
window['cache_hit_ratio']          : float cache_read / (cache_read+input+output), all models (0.0-1.0)
window['cache_hit_ratio_claude']   : float same ratio but only for models starting with 'claude'
window['claude_total_tokens']      : int   total tokens across models starting with 'claude'
"""


_SYSTEM_APPEND = """\
You are writing the Python check function for a token-usage alert rule.

Contract (follow it exactly):
  - Emit exactly one Python function with the signature:
        def check(window: dict) -> tuple[bool, dict]:
  - Return (fired: bool, payload: dict).
  - Values in the payload dict go straight into the notification body, so
    **write human-readable fields in English**, e.g.
    {"metric": "messages", "actual": 1234, "threshold": 1000, "unit": "messages"}.
  - stdlib only (you generally need no imports at all).
  - No I/O, no sleeping, no shell calls. Pure arithmetic on the input dict.
  - No print, no logging — just return.

Output format:
  - Wrap the function in <script>...</script>, each tag on its own line.
  - Anything outside the tags is ignored; the caller only extracts what's
    between them.
"""


_USER_PROMPT_TEMPLATE = """\
User rule:
  Name:        {name}
  Description: {nl_description}
  Threshold spec (JSON): {threshold_spec_json}
  Escalate to agent on fire: {escalate}

Window dict fields you may read:
{window_fields}

Write the check function now. Wrap it in <script>...</script>.
"""


_SCRIPT_RE = re.compile(r"<script>\s*(.+?)\s*</script>", re.DOTALL)


@dataclass
class GenerateResult:
    """What POST /api/tokens/agent-alerts/generate returns."""
    script: str | None
    dry_run: SandboxResult | None
    stdout_tail: str
    duration_sec: float
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.script is not None


def _build_prompt(*, name: str, nl_description: str, threshold_spec: dict, escalate: bool) -> str:
    import json as _json
    try:
        spec_json = _json.dumps(threshold_spec, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        # Fall back to a bare description if the spec dict contains something
        # weird — better than 500-erroring the whole /generate call.
        log.warning("threshold_spec not JSON-serializable, using fallback repr: %s", e)
        spec_json = repr(threshold_spec)[:2000]
    return _USER_PROMPT_TEMPLATE.format(
        name=name,
        nl_description=nl_description,
        threshold_spec_json=spec_json,
        escalate="yes" if escalate else "no",
        window_fields=_WINDOW_FIELDS,
    )


_FENCE_RE = re.compile(r"^```(?:python|py)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _extract_script(stdout: str) -> str | None:
    matches = _SCRIPT_RE.findall(stdout)
    if not matches:
        return None
    # Last match wins if the agent emits multiple attempts.
    raw = matches[-1].strip()
    # Some LLM tunings wrap code in triple-backtick fences even inside the
    # <script> tags. Strip them so the sandbox sees clean Python.
    cleaned = _FENCE_RE.sub("", raw).strip()
    return cleaned or None


async def _run_claude(*, prompt: str, timeout_sec: int) -> tuple[int, str, str]:
    """Spawn `claude -p <prompt> --dangerously-skip-permissions` in CSM's own
    scratch cwd.

    We don't need `--add-dir` or repo access — script generation is pure
    reasoning, no file I/O. The cwd is pinned anyway (rather than inherited)
    because the CLI files a transcript under `~/.claude/projects/<encoded
    cwd>/` regardless: sharing the backend's cwd puts that transcript in the
    same project folder as the user's interactive sessions, where CSM's own
    cwd-keyed session lookups can mistake it for one of them. See
    `Settings.internal_agent_cwd`.
    """
    argv = [
        "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--append-system-prompt", _SYSTEM_APPEND,
        "--output-format", "text",
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(settings.resolved_internal_agent_cwd()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
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
        raise RuntimeError(f"claude generation timed out after {timeout_sec}s")
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def generate_check_script(
    *,
    name: str,
    nl_description: str,
    threshold_spec: dict,
    escalate: bool,
    dry_run_window: dict,
    timeout_sec: int = _GENERATE_TIMEOUT_SEC,
) -> GenerateResult:
    """Generate + dry-run a check script. Never raises."""
    t0 = time.monotonic()

    if not nl_description or not nl_description.strip():
        return GenerateResult(
            script=None, dry_run=None, stdout_tail="",
            duration_sec=time.monotonic() - t0,
            error="nl_description is empty",
        )

    prompt = _build_prompt(
        name=name.strip() or "unnamed rule",
        nl_description=nl_description.strip(),
        threshold_spec=threshold_spec or {},
        escalate=escalate,
    )

    log.info("agent-alert-gen: spawning claude (desc=%s...)", nl_description[:80])
    try:
        exit_code, stdout, stderr = await _run_claude(prompt=prompt, timeout_sec=timeout_sec)
    except RuntimeError as e:
        return GenerateResult(
            script=None, dry_run=None, stdout_tail="",
            duration_sec=time.monotonic() - t0,
            error=str(e),
        )

    stdout_tail = stdout[-4096:] if stdout else ""

    if exit_code != 0:
        return GenerateResult(
            script=None, dry_run=None, stdout_tail=stdout_tail,
            duration_sec=time.monotonic() - t0,
            error=f"claude exit code {exit_code}. stderr tail: {stderr[-512:]!r}",
        )

    script = _extract_script(stdout)
    if script is None:
        return GenerateResult(
            script=None, dry_run=None, stdout_tail=stdout_tail,
            duration_sec=time.monotonic() - t0,
            error="agent did not emit a <script>...</script> block. Try a "
                  "simpler description or retry.",
        )

    # Dry-run against provided window snapshot so the user can see what the
    # script would do right now.
    dry = await run_check_script(script=script, window=dry_run_window)

    if not dry.ok:
        return GenerateResult(
            script=script, dry_run=dry, stdout_tail=stdout_tail,
            duration_sec=time.monotonic() - t0,
            error=f"dry-run failed: {dry.error}",
        )

    return GenerateResult(
        script=script,
        dry_run=dry,
        stdout_tail=stdout_tail,
        duration_sec=time.monotonic() - t0,
    )
