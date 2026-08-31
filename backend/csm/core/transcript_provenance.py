"""Who actually authored this transcript record — and who spawned this
transcript?

Claude Code files a lot of machine-generated text under role "user", and CSM
itself spawns headless `claude -p` helpers whose transcripts land in the same
`~/.claude/projects/` tree as the user's own sessions. Both facts break the
naive readings ("role user means the human typed it", "a transcript in this
cwd belongs to a session in this cwd"), and both broke visibly on 2026-08-30:
a token-alert escalation's answer surfaced inside an unrelated mobile chat,
and subagent completions were emitted as if the user had spoken.

Two predicates, both STRUCTURAL — string-matching the English rots on the next
CLI release:

  * `is_injected_user_record` — record level. Was this role-"user" record typed
    by the human, or filed under their role by the CLI (skill preamble,
    post-compaction recap, subagent task-notification, SDK-driven prompt)?
  * `is_headless_transcript` — file level. Did a `-p` invocation write this
    transcript, or an interactive TTY session? Only the latter can be a
    rotation of a live PTY session row.

Both are shared rather than duplicated because the same judgement is made in
four places (message router, EventStream, the claude adapter's event
derivation, NotificationBus's rebind guard) and they must not drift apart.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# `origin.kind` and `promptSource` state provenance outright. A
# `<task-notification>` — a subagent reporting back when it finishes — is
# kind="task-notification" / promptSource="system"; an SDK-driven prompt is
# promptSource="sdk". The human's are "human" / "typed", plus "queued", which
# is text typed while a turn was still running.
#
# BOTH fields are checked because neither implies the other, and which one you
# get depends on the CLI release that wrote the record: 2.1.112 tags a
# task-notification with `origin` alone (no promptSource at all), while 2.1.233
# emits promptSource without `origin` for SDK prompts and for cron-injected
# unattended prompts.
HUMAN_ORIGIN_KINDS = frozenset({"human"})
HUMAN_PROMPT_SOURCES = frozenset({"typed", "queued"})

# Esc-interrupt marker. The current CLI tags the record with the id of the
# assistant message it cut; pre-2.1.233 records carry nothing, so the two
# literals it has ever emitted are the legacy fallback. Full-string equality,
# not a prefix — a real message that merely quotes the marker still counts as
# the human's.
INTERRUPT_LITERALS = frozenset({
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
})

# `entrypoint` has exactly two values across the local corpus (1,684 records,
# 120 transcripts): "cli" for an interactive TTY session — what SessionManager
# forks — and "sdk-cli" for a `claude -p` one-shot, which is what CSM's own
# helpers (agent-alert escalation / check-script generation, workflow
# authoring) and cron-driven skills spawn.
HEADLESS_ENTRYPOINTS = frozenset({"sdk-cli"})

# How much of a transcript's head to read looking for `entrypoint`. The field
# is on every message-bearing record, so the first one appears within the first
# few lines; the cap just bounds the read on a pathological file.
_HEAD_BYTES = 64 * 1024


def is_injected_user_record(obj: dict[str, Any], text: str = "") -> bool:
    """True when this role-"user" record is NOT something the human typed.

    The test is "present and not human", NOT "absent or not typed": 2.1.112
    wrote no provenance whatsoever on ordinary typed messages (24k records), so
    demanding a positive promptSource=="typed" would classify a whole release's
    history as injected.
    """
    if obj.get("isMeta") or obj.get("isCompactSummary"):
        return True
    # Default-deny on origin: any kind that isn't "human" is machine-filed.
    # 2.1.112 shipped an `origin` with no `promptSource` beside it, so a kind
    # that doesn't exist yet would otherwise read as human.
    origin = obj.get("origin")
    if isinstance(origin, dict) and "kind" in origin:
        if origin["kind"] not in HUMAN_ORIGIN_KINDS:
            return True
    prompt_source = obj.get("promptSource")
    if prompt_source is not None and prompt_source not in HUMAN_PROMPT_SOURCES:
        return True
    # Pressing Esc is an action, not a message.
    if obj.get("interruptedMessageId") or text in INTERRUPT_LITERALS:
        return True
    return False


def is_headless_transcript(path: str | Path) -> bool:
    """True when this transcript was written by a `claude -p` one-shot.

    CONSERVATIVE: returns True only when a headless `entrypoint` is positively
    read. An unreadable / empty / entrypoint-less file returns False, which
    leaves every caller on its pre-existing behaviour — these callers use the
    answer to REFUSE an action, so "don't know" must not refuse.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_HEAD_BYTES)
    except OSError:
        return False
    for raw in head.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            # A truncated final line in the window — nothing after it is
            # readable either, so stop rather than keep scanning garbage.
            break
        if not isinstance(obj, dict):
            continue
        entrypoint = obj.get("entrypoint")
        if isinstance(entrypoint, str) and entrypoint:
            return entrypoint in HEADLESS_ENTRYPOINTS
    return False


def is_headless_session(projects_root: str | Path, cwd: str, external_id: str) -> bool:
    """`is_headless_transcript` addressed by (cwd, external session id).

    Resolves through the same path convention the fast-tail uses, so a caller
    holding only ids doesn't have to know how transcripts are laid out.
    """
    if not external_id:
        return False
    # Imported lazily: `jsonl_fast_tail` pulls in the message router, and this
    # module is imported by the router itself.
    from csm.modules.agent.jsonl_fast_tail import conversation_jsonl_path

    try:
        path = conversation_jsonl_path(Path(projects_root), cwd, external_id)
    except Exception:
        return False
    if not os.path.isfile(path):
        return False
    return is_headless_transcript(path)
