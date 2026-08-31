"""Preset alert-rule templates — hardcoded check scripts for one-click enable.

Presets skip the `claude -p` generation path: the script is authored by us
(reviewer-approved), the user only picks numeric thresholds. This gives:
  * Instant enable (no 5-10s agent spawn).
  * Zero token burn per enable.
  * Deterministic behavior across users.

Users can still author custom rules via POST /agent-alerts/generate — presets
are a fast on-ramp, not a replacement.

Contract for a preset:
  * `id`: stable identifier used by /from-preset.
  * `title` / `description` / `notify_example`: user-facing copy.
  * `params`: list of numeric knobs the user tunes; each has default + range.
  * `escalate_default`: whether we default the rule's escalate flag to True.
  * `build(params: dict) -> tuple[nl_description, threshold_spec, check_script]`:
    returns the full triple the AgentAlertRule row wants persisted.

Check scripts consume the window dict shape from `TrendQueries.current_window()`
which includes (as of 2026-07-10): msg_count, input/cache_creation/cache_read/
output_tokens, total_tokens, estimated_cost_usd, top_session_id,
top_session_tokens, top_session_share, cache_hit_ratio.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str          # user-facing, e.g. "Alert at this many messages"
    unit: str           # e.g. "messages", "$", "%", "M tokens"
    default: float
    min_value: float
    max_value: float
    step: float = 1.0
    is_int: bool = True


@dataclass(frozen=True)
class PresetDef:
    id: str
    title: str                     # e.g. "⏰ Message count nearing the cap"
    description: str               # 1-line user-facing purpose
    notify_example: str            # sample notification body preview
    escalate_default: bool
    params: list[ParamSpec]
    build: Callable[[dict[str, float]], tuple[str, dict[str, Any], str]] = field(repr=False)
    # Cadence defaults — separate from FromPresetRequest defaults so agent-
    # escalated presets can ship with more conservative numbers (avoid
    # burning claude tokens on every tick when the check keeps firing).
    poll_default_sec: int = 60
    cooldown_default_sec: int = 300

    def resolve_params(self, user_params: dict[str, Any]) -> dict[str, float]:
        # NOTE: min_value/max_value are UI hints only (surfaced as <input min/max>
        # on the frontend). We don't reject out-of-range values here — the user
        # explicitly asked to remove the guard so power users can dial thresholds
        # past the "sensible" bounds (e.g. ratio_pct=90 to be extra sensitive).
        out: dict[str, float] = {}
        for p in self.params:
            raw = user_params.get(p.key, p.default)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"param {p.key!r} must be numeric, got {raw!r}")
            out[p.key] = int(v) if p.is_int else v
        return out


# ------------------------------------------------------------------
# 1. Message count nearing the cap — msg_count >= threshold
# ------------------------------------------------------------------


def _build_msg_count(p: dict[str, float]) -> tuple[str, dict[str, Any], str]:
    threshold = int(p["threshold"])
    nl = f"Alert when the last 5 hours reach {threshold} messages (rate-limit guard)"
    spec = {"metric": "msg_count", "op": ">=", "value": threshold}
    script = f"""def check(window: dict) -> tuple[bool, dict]:
    actual = int(window.get("msg_count", 0) or 0)
    threshold = {threshold}
    fired = actual >= threshold
    return fired, {{
        "metric": "msg_count",
        "actual": actual,
        "threshold": threshold,
        "unit": "messages",
    }}
"""
    return nl, spec, script


PRESET_MSG_COUNT = PresetDef(
    id="msg_count_warn",
    title="⏰ Message count nearing the cap",
    description="Alerts when the last 5 hours approach the rate limit — keeps a Pro/Max plan off the line",
    notify_example="5h messages 1620 / 1500",
    escalate_default=False,
    params=[
        ParamSpec(key="threshold", label="Alert at this many messages",
                  unit="messages", default=1500, min_value=100, max_value=10000, step=100),
    ],
    build=_build_msg_count,
    poll_default_sec=60,        # cheap check — once a minute is fine
    cooldown_default_sec=900,   # don't re-alert within 15 minutes
)


# ------------------------------------------------------------------
# 2. One session burning tokens — top_session_share > pct AND top_session_tokens > threshold
# ------------------------------------------------------------------


def _build_session_burn(p: dict[str, float]) -> tuple[str, dict[str, Any], str]:
    share_pct = int(p["share_pct"])
    tokens_m = int(p["tokens_million"])
    tokens = tokens_m * 1_000_000
    nl = (
        f"Alert when one session takes more than {share_pct}% of the last 5 hours' "
        f"tokens AND more than {tokens_m}M in absolute terms (possible stuck loop)"
    )
    spec = {
        "metric": "top_session_share_and_tokens",
        "share_pct_gte": share_pct,
        "tokens_gte": tokens,
    }
    script = f"""def check(window: dict) -> tuple[bool, dict]:
    share = float(window.get("top_session_share", 0.0) or 0.0)
    tokens = int(window.get("top_session_tokens", 0) or 0)
    sid = window.get("top_session_id") or "(unknown)"
    share_pct_gte = {share_pct}
    tokens_gte = {tokens}
    fired = (share * 100.0) >= share_pct_gte and tokens >= tokens_gte
    return fired, {{
        "metric": "session_burn",
        "session_id": sid,
        "actual_share_pct": round(share * 100.0, 1),
        "actual_tokens": tokens,
        "threshold_share_pct": share_pct_gte,
        "threshold_tokens": tokens_gte,
    }}
"""
    return nl, spec, script


PRESET_SESSION_BURN = PresetDef(
    id="session_burn",
    title="🔥 One session is burning tokens",
    description="One session is eating too many tokens, possibly stuck in a loop. The agent names the session and the tool driving it.",
    notify_example="session ef8f105f at 88% (280M) — 42 Bash calls in a loop; consider killing it",
    escalate_default=True,
    params=[
        ParamSpec(key="share_pct", label="Share of total spend above",
                  unit="%", default=85, min_value=20, max_value=95, step=5),
        ParamSpec(key="tokens_million", label="and absolute spend above",
                  unit="M tokens", default=100, min_value=10, max_value=2000, step=10),
    ],
    build=_build_session_burn,
    poll_default_sec=10800,      # 3 hours
    cooldown_default_sec=10800,  # 3 hours — agent-escalated, so rate-limit it hard
)


# ------------------------------------------------------------------
# 3. Cache efficiency dropped — cache_hit_ratio < pct AND total_tokens > threshold
# ------------------------------------------------------------------


def _build_cache_drop(p: dict[str, float]) -> tuple[str, dict[str, Any], str]:
    ratio_pct = int(p["ratio_pct"])
    tokens_m = int(p["tokens_million"])
    tokens = tokens_m * 1_000_000
    nl = (
        f"Alert when Claude's cache hit rate over the last 5 hours falls below "
        f"{ratio_pct}% AND Claude's total spend exceeds {tokens_m}M "
        f"(non-Claude models such as GLM write no cache and are excluded)"
    )
    spec = {
        "metric": "cache_hit_ratio_drop",
        "ratio_pct_lt": ratio_pct,
        "total_tokens_gte": tokens,
        "scope": "claude_only",
    }
    # Use `cache_hit_ratio_claude` (LIKE 'claude%') instead of the mixed
    # aggregate — GLM/others don't emit cache tokens and would drag the raw
    # ratio toward zero even when Claude sessions are caching fine.
    script = f"""def check(window: dict) -> tuple[bool, dict]:
    ratio = float(window.get("cache_hit_ratio_claude", 0.0) or 0.0)
    total = int(window.get("claude_total_tokens", 0) or 0)
    ratio_pct_lt = {ratio_pct}
    total_tokens_gte = {tokens}
    fired = (ratio * 100.0) < ratio_pct_lt and total >= total_tokens_gte
    return fired, {{
        "metric": "cache_hit_ratio_claude",
        "actual_ratio_pct": round(ratio * 100.0, 1),
        "actual_claude_tokens": total,
        "threshold_ratio_pct": ratio_pct_lt,
        "threshold_total_tokens": total_tokens_gte,
        "scope": "claude_only",
    }}
"""
    return nl, spec, script


PRESET_CACHE_DROP = PresetDef(
    id="cache_hit_drop",
    title="🗄 Cache efficiency dropped",
    description="A low Claude cache hit rate means paying twice for the same context. The agent diagnoses which sessions broke their cache (models with no cache, like GLM, are excluded automatically).",
    notify_example="Claude cache hit rate 22% (normally 90%+) — recent sessions keep --resume-ing into new conversations…",
    escalate_default=True,
    params=[
        ParamSpec(key="ratio_pct", label="Claude hit rate below",
                  unit="%", default=30, min_value=5, max_value=80, step=5),
        ParamSpec(key="tokens_million", label="and Claude spend above",
                  unit="M tokens", default=50, min_value=5, max_value=1000, step=5),
    ],
    build=_build_cache_drop,
    poll_default_sec=10800,      # 3 hours
    cooldown_default_sec=10800,  # 3 hours — agent-escalated, so rate-limit it hard
)


# ------------------------------------------------------------------
# 4. 5h spend too high — total_tokens >= threshold
# ------------------------------------------------------------------


def _build_total_tokens(p: dict[str, float]) -> tuple[str, dict[str, Any], str]:
    tokens_m = int(p["tokens_million"])
    tokens = tokens_m * 1_000_000
    nl = (
        f"Alert when total token spend over the last 5 hours exceeds {tokens_m}M "
        f"(complements msg_count — catches a hot cache with few messages)"
    )
    spec = {"metric": "total_tokens", "op": ">=", "value": tokens}
    script = f"""def check(window: dict) -> tuple[bool, dict]:
    actual = int(window.get("total_tokens", 0) or 0)
    threshold = {tokens}
    fired = actual >= threshold
    return fired, {{
        "metric": "total_tokens",
        "actual": actual,
        "threshold": threshold,
    }}
"""
    return nl, spec, script


PRESET_TOTAL_TOKENS = PresetDef(
    id="total_tokens_warn",
    title="📊 5h spend too high",
    description="Alerts when total tokens over the last 5 hours pass a threshold. The agent points at the main consumer.",
    notify_example="5h spend 480M — session 9425ad60 at 78% (mostly cache_read)",
    escalate_default=True,
    params=[
        ParamSpec(key="tokens_million", label="Above this many million tokens",
                  unit="M", default=400, min_value=50, max_value=5000, step=50),
    ],
    build=_build_total_tokens,
    poll_default_sec=10800,      # 3 hours
    cooldown_default_sec=10800,  # 3 hours — agent-escalated, so rate-limit it hard
)


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------


PRESETS: dict[str, PresetDef] = {
    p.id: p for p in [
        PRESET_MSG_COUNT,
        PRESET_TOTAL_TOKENS,
        PRESET_SESSION_BURN,
        PRESET_CACHE_DROP,
    ]
}


def preset_catalog() -> list[dict[str, Any]]:
    """Serialize preset defs for GET /agent-alerts/presets."""
    return [
        {
            "id": p.id,
            "title": p.title,
            "description": p.description,
            "notify_example": p.notify_example,
            "escalate_default": p.escalate_default,
            "poll_default_sec": p.poll_default_sec,
            "cooldown_default_sec": p.cooldown_default_sec,
            "params": [
                {
                    "key": ps.key,
                    "label": ps.label,
                    "unit": ps.unit,
                    "default": ps.default,
                    "min": ps.min_value,
                    "max": ps.max_value,
                    "step": ps.step,
                    "is_int": ps.is_int,
                }
                for ps in p.params
            ],
        }
        for p in PRESETS.values()
    ]
