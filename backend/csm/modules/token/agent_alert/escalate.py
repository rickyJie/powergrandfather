"""Agent escalation — call `claude -p` with a rich context blob and parse a
short root-cause + recommendations blurb for the notification body.

Contract:
  * Never raises — on any failure returns `None` so the caller falls back to
    the plain "threshold crossed" notification.
  * Timeout is short (60s) since the response is text-only reasoning over a
    pre-computed blob.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from csm.config import settings
from csm.modules.token.agent_alert.context_builder import ContextBlob, build_context

log = logging.getLogger(__name__)

_ESCALATE_TIMEOUT_SEC = 60


_ESCALATE_SYSTEM_APPEND = """\
You diagnose a token-usage alert that just fired, for a Claude Code user.

Your input: the name and description of the rule that fired, the check
script's payload, and structured context (top sessions, model mix, cache hit
rate, the last 30 minutes of token spend).

Produce (keep it short, direct and actionable):
  1. A one-line title, <= 12 words, saying plainly what the problem is.
  2. A body of <= 40 words, plain text — no markdown headings, lists or code
     blocks. Format: "<one sentence on the root cause> · <the single most
     important recommendation>".
     If you cannot determine the cause, say "root cause unclear" and give one
     direction to investigate.
  3. Do not invent anything: never name a session id, project or tool that
     doesn't appear in the context.

**Write all output in English.**

Output format (strict):

<summary>
<title>...</title>
<body>
...
</body>
</summary>
"""


_USER_PROMPT_TEMPLATE = """\
Rule that fired:
  Name: {rule_name}
  User's description: {nl_description}

Values from the check script:
  metric   = {metric}
  actual   = {actual}
  threshold = {threshold}

Context blob:

{context_markdown}

Now analyze and produce the <summary> block.
"""


_SUMMARY_RE = re.compile(r"<summary>\s*(.+?)\s*</summary>", re.DOTALL)
_TITLE_RE = re.compile(r"<title>\s*(.+?)\s*</title>", re.DOTALL)
_BODY_RE = re.compile(r"<body>\s*(.+?)\s*</body>", re.DOTALL)


@dataclass
class EscalationResult:
    title: str
    body: str
    context: ContextBlob
    duration_sec: float


async def _run_claude(prompt: str, timeout_sec: int) -> tuple[int, str, str]:
    argv = [
        "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--append-system-prompt", _ESCALATE_SYSTEM_APPEND,
        "--output-format", "text",
    ]
    # Pinned to a dedicated cwd, NOT the backend's. This spawn writes a real
    # transcript under `~/.claude/projects/<encoded cwd>/`; inheriting the
    # backend's cwd put it in the same project folder as the user's own
    # sessions in this repo, where the cwd-fallback rebind adopted it onto a
    # live session row and this escalation's answer surfaced inside an
    # unrelated chat. See `Settings.internal_agent_cwd`.
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
        raise RuntimeError(f"claude escalation timed out after {timeout_sec}s")
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _extract(stdout: str) -> tuple[str, str] | None:
    """Pull (title, body) out of a well-formed <summary> block.

    Returns None on any failure; caller logs at WARNING with the specific
    reason so users can see whether the agent replied garbled vs empty vs
    partial. Common failure modes:
      * No <summary> block at all — agent didn't follow the instruction.
      * <summary> with only <title> — agent skipped the body.
      * <summary> with empty strings — model refused / hedged.
    """
    m = _SUMMARY_RE.search(stdout)
    if not m:
        log.warning("agent-alert escalate: no <summary> block in stdout tail=%r",
                    stdout[-256:])
        return None
    body_block = m.group(1)
    t = _TITLE_RE.search(body_block)
    b = _BODY_RE.search(body_block)
    if not t and not b:
        log.warning("agent-alert escalate: <summary> block has neither <title> nor <body>")
        return None
    if not t:
        log.warning("agent-alert escalate: <summary> missing <title>")
        return None
    if not b:
        log.warning("agent-alert escalate: <summary> missing <body>")
        return None
    title = t.group(1).strip().splitlines()[0][:80]
    body = b.group(1).strip()[:300]
    if not title or not body:
        log.warning("agent-alert escalate: title or body empty after strip "
                    "(title=%r body=%r)", title[:80], body[:80])
        return None
    return title, body


def _sanitize_for_prompt(s: str, max_len: int = 400) -> str:
    """Prevent newlines / XML tags in user-supplied rule fields from
    corrupting the <summary>...</summary> XML we ask the agent to produce.
    """
    if not s:
        return ""
    # Collapse newlines so the agent's response tags stay on their own lines,
    # and neutralize obvious XML that could confuse `_extract()`.
    cleaned = s.replace("\n", " ").replace("\r", " ")
    cleaned = cleaned.replace("<", "‹").replace(">", "›")
    return cleaned[:max_len]


async def escalate(
    *,
    sessionmaker,
    rule,
    window_snapshot: dict[str, Any],
    script_payload: dict[str, Any],
    timeout_sec: int = _ESCALATE_TIMEOUT_SEC,
) -> EscalationResult | None:
    """Build context + call agent + return summary. None on any failure."""
    t0 = time.monotonic()

    try:
        ctx = await build_context(sessionmaker, window_snapshot=window_snapshot)
    except Exception as e:
        log.warning("agent-alert escalate: context build failed: %s", e)
        return None

    prompt = _USER_PROMPT_TEMPLATE.format(
        rule_name=_sanitize_for_prompt(rule.name, 200),
        nl_description=_sanitize_for_prompt(rule.nl_description, 500),
        metric=script_payload.get("metric", "?"),
        actual=script_payload.get("actual", "?"),
        threshold=script_payload.get("threshold", "?"),
        context_markdown=ctx.to_markdown(),
    )

    try:
        exit_code, stdout, stderr = await _run_claude(prompt, timeout_sec)
    except RuntimeError as e:
        log.warning("agent-alert escalate: %s", e)
        return None

    if exit_code != 0:
        log.warning("agent-alert escalate: claude exit=%d stderr=%s",
                    exit_code, stderr[-256:])
        return None

    extracted = _extract(stdout)
    if extracted is None:
        log.warning("agent-alert escalate: no <summary> block; stdout tail=%s",
                    stdout[-256:])
        return None

    title, body = extracted
    return EscalationResult(
        title=title,
        body=body,
        context=ctx,
        duration_sec=time.monotonic() - t0,
    )
