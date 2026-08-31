"""Pydantic v2 schema for the SyncAgent's decision output.

The SyncAgent (see `agent.py`) is a decision-only LLM: on each tick it
receives a snapshot of CSM DB + every enrolled agent's live state and
returns a `SyncDecisionsPayload`. This module owns the strict Pydantic
contract that every LLM response is parsed against — schema violation
aborts the entire tick (see design v4 §5.4 rejection matrix).

Layout:

- `_BodyValidatorMixin`     — shared 200 KB cap + marker-injection guard
- `InstructionCandidate`, `McpCandidate`, `SkillCandidate` — typed
  candidate models the agent may adopt into CSM. Skill body_md
  auto-prepends minimal YAML frontmatter when missing (v4 NEW-P0-2).
- `AdoptToCsm`, `PropagateToAgent`, `ProposeConflict`, `Skip` — the
  four allowed action shapes.
- `Decision`                — `Annotated[Union[...], discriminator="action"]`.
  Any other `action` string fails parse.
- `SyncDecisionsPayload`    — full run output; `decisions` is capped at
  500 (Pydantic-level), the orchestrator further truncates to 30
  non-skip by `_ACTION_PRIORITY` when the agent violates its output cap.
- `_ACTION_PRIORITY`         — apply-order weights (v7 §4). adopt=0 is
  most important, skip=3 least.

The rule layer (see `orchestrator.py`) re-checks agent-side state at
apply time; nothing here validates against DB state (that lives in the
orchestrator's stale-read guard).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NAME_RE = r"^[a-z0-9][a-z0-9-]{0,79}$"

_BODY_MAX_BYTES = 200_000

_MARKER_INJECTION_TOKENS = ("csm:start", "csm:end")


_ACTION_PRIORITY: dict[str, int] = {
    # v7 §4: apply-order + truncation weights (lower = more important).
    "adopt_to_csm": 0,       # new content into CSM — always highest value
    "propose_conflict": 1,   # user must adjudicate — surface early
    "propagate_to_agent": 2,
    "skip": 3,               # no-ops last
}


# ---------------------------------------------------------------------------
# Body validation mixin (used by all candidate types)
# ---------------------------------------------------------------------------


class _BodyValidatorMixin:
    """Shared body-text validation: size cap + marker-injection refusal.

    Not a Pydantic model itself — just a namespace for a staticmethod that
    the candidate classes reuse in their field_validator hooks.
    """

    @staticmethod
    def _validate_body(v: str, field_name: str = "body") -> str:
        # Byte-count, not char-count: JSON payloads are utf-8 encoded and
        # we care about the wire size hitting Anthropic + our audit blob.
        if len(v.encode("utf-8")) > _BODY_MAX_BYTES:
            raise ValueError(
                f"{field_name} exceeds {_BODY_MAX_BYTES // 1000}KB byte cap",
            )
        low = v.lower()
        for tok in _MARKER_INJECTION_TOKENS:
            if tok in low:
                raise ValueError(
                    f"{field_name} must not contain csm marker syntax "
                    f"({tok!r} found)",
                )
        return v


# ---------------------------------------------------------------------------
# Candidate types
# ---------------------------------------------------------------------------


class InstructionCandidate(BaseModel):
    name: str = Field(..., pattern=_NAME_RE)
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1)

    @field_validator("body")
    @classmethod
    def _body_check(cls, v: str) -> str:
        return _BodyValidatorMixin._validate_body(v, "body")


class McpCandidate(BaseModel):
    name: str = Field(..., pattern=_NAME_RE)
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args_json: list[str] = Field(default_factory=list)
    url: str | None = None
    env_json: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> McpCandidate:
        # stdio requires command; http/sse require url; not both.
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires `command`")
            if self.url:
                raise ValueError("stdio transport forbids `url`")
        else:  # http | sse
            if not self.url:
                raise ValueError(f"{self.transport} transport requires `url`")
            if self.command:
                raise ValueError(
                    f"{self.transport} transport forbids `command`"
                )
        return self


class SkillCandidate(BaseModel):
    name: str = Field(..., pattern=_NAME_RE)
    description: str = Field(..., min_length=1, max_length=1000)
    body_md: str = Field(..., min_length=1)

    @field_validator("body_md")
    @classmethod
    def _body_check(cls, v: str, info: ValidationInfo) -> str:
        v = _BodyValidatorMixin._validate_body(v, "body_md")
        # v4 NEW-P0-2: auto-prepend minimal YAML frontmatter if missing,
        # rather than rejecting outright. The agent's rationale may be
        # correct even if it forgot the header.
        if not v.lstrip().startswith("---"):
            data = info.data or {}
            name = data.get("name", "unnamed")
            description = data.get("description", "")
            v = (
                f"---\nname: {name}\ndescription: {description}\n---\n\n{v}"
            )
        return v


# ---------------------------------------------------------------------------
# Action models
# ---------------------------------------------------------------------------


class AdoptToCsm(BaseModel):
    """Pull an agent-side resource into CSM DB.

    Two shapes by resource_type:
    - **skill**: REFERENCE style. The agent gives `resource_name` only; the
      body is read from `source_agent`'s skills dir at apply time. The agent
      decides on the content HASH (what it sees in the input state) and never
      carries the body — so a 500KB skill costs a few bytes of decision, not a
      full echo. Scales to any number/size of skills.
    - **instruction / mcp_server**: CANDIDATE style. These are small and (for
      mcp) not cleanly disk-readable, so the agent still hands over the full
      `candidate`. Kept as-is.
    """

    action: Literal["adopt_to_csm"]
    resource_type: Literal["instruction", "mcp_server", "skill"]
    source_agent: str = Field(..., min_length=1)
    recommended_scope: list[str] = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1, max_length=4000)
    # skill → reference; body read from source_agent disk at apply time.
    resource_name: str | None = Field(default=None, pattern=_NAME_RE)
    # instruction / mcp_server → candidate carries the content.
    candidate: InstructionCandidate | McpCandidate | None = None

    @model_validator(mode="after")
    def _shape(self) -> AdoptToCsm:
        if self.resource_type == "skill":
            if not self.resource_name:
                raise ValueError(
                    "skill adopt is reference-style: give `resource_name` "
                    "(the body is read from source_agent's disk), not a candidate",
                )
            if self.candidate is not None:
                raise ValueError(
                    "skill adopt must NOT carry a candidate body — use "
                    "`resource_name` only",
                )
            return self
        # instruction / mcp_server → candidate required, matching the type.
        type_map: dict[str, type] = {
            "instruction": InstructionCandidate,
            "mcp_server": McpCandidate,
        }
        expected = type_map[self.resource_type]
        if not isinstance(self.candidate, expected):
            raise ValueError(
                f"{self.resource_type} adopt requires a matching `candidate` "
                f"(got {type(self.candidate).__name__})",
            )
        if self.resource_name is not None:
            raise ValueError(
                f"{self.resource_type} adopt uses `candidate`, not `resource_name`",
            )
        return self


class PropagateToAgent(BaseModel):
    """Push a CSM row out to a specific agent (id already in CSM DB)."""

    action: Literal["propagate_to_agent"]
    resource_type: Literal["instruction", "mcp_server", "skill"]
    resource_id: int = Field(..., gt=0)
    target_agent: str = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1, max_length=4000)


class ProposeConflict(BaseModel):
    """Surface a two-sided divergence for user resolution.

    Two shapes, matching AdoptToCsm:
    - **skill**: REFERENCE style — `resource_name` + `conflict_agents` (the
      agents whose hash diverges). The agent only ever saw hashes, so the
      service fetches each side's body from disk/DB when it builds the pending
      row for the user diff.
    - **instruction / mcp_server**: `candidates` maps agent name → body text
      (the agent carries them).

    resource_id may be None for pure agent-only conflicts (no CSM row yet).
    """

    action: Literal["propose_conflict"]
    resource_type: Literal["instruction", "mcp_server", "skill"]
    resource_id: int | None = None
    rationale: str = Field(..., min_length=1, max_length=4000)
    # skill → reference
    resource_name: str | None = Field(default=None, pattern=_NAME_RE)
    conflict_agents: list[str] | None = None
    # instruction / mcp → bodies carried
    candidates: dict[str, str] | None = None

    @field_validator("candidates")
    @classmethod
    def _validate_candidate_map(
        cls, v: dict[str, str] | None,
    ) -> dict[str, str] | None:
        if v is None:
            return v
        for agent, body in v.items():
            if not agent or not agent.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"invalid agent key: {agent!r}")
            _BodyValidatorMixin._validate_body(body, f"candidates[{agent}]")
        return v

    @model_validator(mode="after")
    def _shape(self) -> ProposeConflict:
        if self.resource_type == "skill":
            if not self.resource_name:
                raise ValueError(
                    "skill conflict is reference-style: give `resource_name`",
                )
            if not self.conflict_agents:
                raise ValueError(
                    "skill conflict requires `conflict_agents` (the diverging "
                    "agents); the service fetches their bodies",
                )
            if self.candidates:
                raise ValueError(
                    "skill conflict must NOT carry candidate bodies",
                )
        else:
            if not self.candidates or len(self.candidates) < 2:
                raise ValueError(
                    f"{self.resource_type} conflict requires `candidates` "
                    "with >= 2 sides",
                )
            if self.resource_name or self.conflict_agents:
                raise ValueError(
                    f"{self.resource_type} conflict uses `candidates`, not "
                    "the skill reference fields",
                )
        return self


class Skip(BaseModel):
    """No-op decision — recorded for audit, no side effects."""

    action: Literal["skip"]
    rationale: str = Field(..., min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Discriminated union + payload wrapper
# ---------------------------------------------------------------------------


Decision = Annotated[
    AdoptToCsm | PropagateToAgent | ProposeConflict | Skip,
    Field(discriminator="action"),
]


class SyncDecisionsPayload(BaseModel):
    """Top-level output shape the SyncAgent MUST return.

    `max_length=500` catches egregious spam; the orchestrator further
    truncates non-skip decisions to 30 by `_ACTION_PRIORITY` when the
    agent has ignored its per-tick output cap.
    """

    decisions: list[Decision] = Field(..., max_length=500)
    summary: str = Field(..., max_length=1000)


def sort_decisions_by_priority(
    decisions: list[Decision],
) -> list[Decision]:
    """Stable-sort by `_ACTION_PRIORITY`; preserves original order within
    each priority band. Used by the orchestrator's truncation guard
    (v7 §4 P2-V6-1)."""
    return [
        d
        for _, d in sorted(
            enumerate(decisions),
            key=lambda t: (_ACTION_PRIORITY.get(_action_of(t[1]), 99), t[0]),
        )
    ]


def _action_of(d: Any) -> str:
    """Return the `action` string of a Decision (works for Pydantic
    instances or plain dicts)."""
    if hasattr(d, "action"):
        return str(d.action)
    if isinstance(d, dict):
        return str(d.get("action", ""))
    return ""


__all__ = [
    # Constants
    "_NAME_RE",
    "_BODY_MAX_BYTES",
    "_ACTION_PRIORITY",
    # Candidate models
    "_BodyValidatorMixin",
    "InstructionCandidate",
    "McpCandidate",
    "SkillCandidate",
    # Action models
    "AdoptToCsm",
    "PropagateToAgent",
    "ProposeConflict",
    "Skip",
    # Union + payload
    "Decision",
    "SyncDecisionsPayload",
    # Helper
    "sort_decisions_by_priority",
]
