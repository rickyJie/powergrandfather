"""Server-side workflow authoring — two-round claude spawn:

Round 1 (`clarify_workflow`): spawns a short claude to skim the repo
and emit boundary clarification questions the user should answer.

Round 2 (`generate_workflow`): spawns the full authoring claude that
writes the YAML file + review report, optionally spliced with user
answers to round-1 questions.

Cache: `ClarificationCache` — in-memory TTL for round-1 → round-2 handoff.
"""
from csm.modules.workflow.authoring.cache import (
    ClarificationCache,
    ClarificationEntry,
)
from csm.modules.workflow.authoring.generator import (
    ClarifyResult,
    GenerationError,
    GenerationResult,
    clarify_workflow,
    edit_workflow,
    generate_workflow,
)

__all__ = [
    "GenerationResult",
    "GenerationError",
    "ClarifyResult",
    "ClarificationCache",
    "ClarificationEntry",
    "generate_workflow",
    "clarify_workflow",
    "edit_workflow",
]
