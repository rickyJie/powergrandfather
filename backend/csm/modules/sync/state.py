"""SyncOrchestrator state helpers: input payload build + redact + hash.

The SyncAgent sees FULL content on the wire (agent bodies, CSM
instruction bodies, skill body_md, mcp env values) so it can reason
about adopt / propagate / conflict decisions. But the persisted
`sync_agent_run.input_snapshot_json` audit record is REDACTED
(design v3 §3.2.1) — full bodies become `<sha256:...>` fingerprints and
mcp `env_json` values become `<redacted>`.

Rationale: the input snapshot exists for "what did the agent see,
shape-wise" debugging. Reproducing the exact body isn't its job — the
agent's raw response captures the decision. Redacting eliminates
secret / PII exposure in the audit log without losing the ability to
detect "did the input state change" (via `_compute_input_state_hash`
on the redacted payload, which is stable so long as the body sha
fingerprints stay stable).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def _sha16(text: str) -> str:
    """First 16 hex chars of sha256(text) — enough to distinguish, short
    enough to keep the audit snapshot readable."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _fingerprint(body: str) -> str:
    return f"<sha256:{_sha16(body)}>"


def redact_for_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied `state` with body-text fields replaced by
    sha256 fingerprints and mcp env values by `<redacted>`.

    Never raises on missing keys — best-effort walk that preserves
    unknown-shape data as-is. Safe to call on any dict-shaped payload.
    """
    s = copy.deepcopy(state)

    # ---- agent-side bodies ------------------------------------------------
    for _, ad in (s.get("agents") or {}).items():
        if not isinstance(ad, dict):
            continue
        # memory_full: full text of that agent's memory scope
        mem = ad.get("memory_full")
        if isinstance(mem, str) and mem:
            ad["memory_full"] = _fingerprint(mem)
        # skills: list of {name, body_md, ...}
        for sk in ad.get("skills") or []:
            if isinstance(sk, dict) and isinstance(sk.get("body_md"), str):
                sk["body_md"] = _fingerprint(sk["body_md"])
        # mcp_servers: list of {name, transport, env, ...}
        for mcp in ad.get("mcp_servers") or []:
            if isinstance(mcp, dict) and isinstance(mcp.get("env"), dict):
                mcp["env"] = {k: "<redacted>" for k in mcp["env"]}

    # ---- CSM-side bodies --------------------------------------------------
    csm = s.get("csm_resources") or {}

    for i in csm.get("instructions") or []:
        if isinstance(i, dict) and isinstance(i.get("body"), str):
            i["body"] = _fingerprint(i["body"])

    for m in csm.get("mcp_servers") or []:
        if isinstance(m, dict) and isinstance(m.get("env_json"), dict):
            m["env_json"] = {k: "<redacted>" for k in m["env_json"]}

    for sk in csm.get("skills") or []:
        if isinstance(sk, dict) and isinstance(sk.get("body_md"), str):
            sk["body_md"] = _fingerprint(sk["body_md"])

    return s


def compute_input_state_hash(payload: dict[str, Any]) -> str:
    """Deterministic hash of the (redacted) input payload.

    Used by the orchestrator to short-circuit when nothing has changed
    between ticks (design v3 §14 B13). Call after redaction so the hash
    is stable regardless of body content — it fingerprints the SHAPE.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_input_payload(
    csm_resources: dict[str, Any],
    agent_states: dict[str, Any],
    pending_recent: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full (un-redacted) payload the SyncAgent receives.

    The orchestrator passes this raw to `SyncAgent.decide()`. The audit
    layer separately calls `redact_for_snapshot()` before storing.
    """
    return {
        "csm_resources": csm_resources,
        "agents": agent_states,
        "pending_decisions_recent": pending_recent,
        "meta": meta or {},
    }


__all__ = [
    "redact_for_snapshot",
    "compute_input_state_hash",
    "build_input_payload",
]
