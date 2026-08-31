"""AgentAlertRule subsystem — agent-authored Python check scripts.

Module map:
  * `sandbox.py`   — subprocess runner that executes a check script against a
                     JSON window snapshot; returns (fired, payload) or an error.
  * `generator.py` — spawns `claude -p` to convert user's NL rule spec into a
                     `check(window)` function, then dry-runs it in the sandbox.
"""
from csm.modules.token.agent_alert.evaluator import AgentAlertEvaluator
from csm.modules.token.agent_alert.generator import GenerateResult, generate_check_script
from csm.modules.token.agent_alert.presets import PRESETS, preset_catalog
from csm.modules.token.agent_alert.sandbox import SandboxResult, run_check_script

__all__ = [
    "AgentAlertEvaluator",
    "GenerateResult",
    "generate_check_script",
    "PRESETS",
    "preset_catalog",
    "SandboxResult",
    "run_check_script",
]
