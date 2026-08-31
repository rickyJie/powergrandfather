"""Mock GeminiAdapter — a fake third-CLI adapter used to validate the
schema-driven frontend renders new adapters with ZERO code changes.

Does NOT spawn a real process; if the API layer ever calls this
adapter's spawn methods it raises loudly so we notice.

Registered by `build_default_registry()` iff the env flag
`CSM_ENABLE_GEMINI_MOCK=1` is set. Never active in production.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from csm.backends.base import (
    AdapterArgvResult,
    AdapterStatus,
    Capability,
    CheckboxFlag,
    FlagDescriptor,
    InfoBlock,
    MarkerSyntax,
    SelectChoice,
    SelectFlag,
)
from csm.core.events import Event


class GeminiMockAdapter:
    name = "gemini"
    display_name = "Gemini (mock)"
    icon = "G"
    color = "#4285F4"            # Google-ish blue
    capabilities = frozenset({Capability.INTERACTIVE_STREAM})

    def default_argv(self) -> str:
        return "gemini chat --model gemini-1.5-pro"

    def flags_schema(self) -> list[FlagDescriptor]:
        return [
            SelectFlag(
                kind="select",
                name="model",
                label="Model",
                argv_flag="--model",
                choices=(
                    SelectChoice(value="", label="default"),
                    SelectChoice(value="gemini-1.5-pro", label="1.5 Pro"),
                    SelectChoice(value="gemini-1.5-flash", label="1.5 Flash"),
                    SelectChoice(value="gemini-2.0-flash", label="2.0 Flash"),
                ),
            ),
            CheckboxFlag(
                kind="checkbox",
                name="grounding",
                label="Search grounding",
                argv_flag="--use-grounding",
                hint="Enable Google Search grounding.",
            ),
            InfoBlock(
                kind="info",
                text=(
                    "Gemini CLI mock — this adapter is a smoke test for the "
                    "schema-driven frontend abstraction; it doesn't spawn a "
                    "real process. If you can see this blob rendered in the "
                    "New Session dialog, the frontend correctly picks up "
                    "backend-declared UI schema for arbitrary adapters."
                ),
            ),
        ]

    # ---- environment (fake) ----
    def home_dir(self) -> Path:
        return Path("/tmp/fake-gemini-home")

    def default_home_name(self) -> str:
        return ".gemini"

    def auth_file(self) -> Path | None:
        return None

    def probe(self) -> AdapterStatus:
        # Report as "installed + authenticated" so the frontend selector
        # lists it as usable — makes the smoke test easier to see.
        return AdapterStatus(
            name=self.name,
            installed=True,
            authenticated=True,
            version="mock-0.0.1",
            error=None,
            capabilities=self.capabilities,
        )

    def pre_spawn_session_id(self, cwd: str) -> str | None:
        return None

    def post_spawn_bind(self, session_row_id: str, cwd: str) -> str | None:
        return None

    def frame_pty_input(self, text: str) -> bytes:
        return (text + "\r\n").encode("utf-8", errors="replace")

    def frame_pty_input_sequence(self, text: str) -> list[bytes]:
        return [self.frame_pty_input(text)]

    def build_argv(self, base_argv: list[str], cwd: str, **_kw) -> AdapterArgvResult:
        raise NotImplementedError(
            "GeminiMockAdapter is a smoke-test-only adapter; it does not spawn."
        )

    def artifact_root(self) -> Path:
        return self.home_dir() / "sessions"

    def artifact_glob(self) -> str:
        return str(self.artifact_root() / "**" / "*.jsonl")

    def scan_events(self) -> list[Event]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, snap: dict[str, Any]) -> None:
        pass

    def take_newly_seen(self) -> set[str]:
        return set()

    def tail_states(self) -> list[dict[str, Any]]:
        return []

    def install_hooks(self, project_root: Path, callback_url: str) -> None:
        pass

    # ---- multi-agent config sync (P0 v3 · stub) ----------------------
    # Mock adapter never claims SYNC_* caps; stubs exist only to keep
    # isinstance(GeminiMockAdapter(), CLIAdapter) passing after the
    # Protocol was extended.

    def memory_paths(self, scope: str) -> list[Path]:
        return []

    def read_memory(self, path: Path) -> str:
        return ""

    def read_memory_full(self, scope: str) -> str | None:
        return None

    def write_memory_marker_block(
        self, path: Path, marker_id: str, body: str,
    ) -> None:
        raise NotImplementedError("GeminiMockAdapter has no sync support.")

    async def mcp_add(
        self,
        name: str,
        *,
        transport: str,
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        raise NotImplementedError("GeminiMockAdapter has no sync support.")

    async def mcp_remove(self, name: str) -> Any:
        raise NotImplementedError("GeminiMockAdapter has no sync support.")

    async def mcp_list(self) -> list[dict[str, Any]]:
        return []

    async def list_mcp_servers_full(self) -> list[dict[str, Any]]:
        return []

    def lookup_external_title(self, external_id: str) -> str | None:
        return None

    def skills_dir(self) -> Path | None:
        return None

    def list_skills(self) -> list[dict[str, Any]]:
        return []

    def list_skills_full(self) -> list[dict[str, Any]]:
        return []

    def read_skill_bundle(self, name: str) -> dict[str, Any] | None:
        return None

    def write_simple_skill(self, spec: dict[str, Any]) -> None:
        raise NotImplementedError("GeminiMockAdapter has no sync support.")

    def write_skill_bundle(self, spec: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("GeminiMockAdapter has no sync support.")

    def remove_skill(self, name: str) -> None:
        raise NotImplementedError("GeminiMockAdapter has no sync support.")

    def marker_syntax(self) -> MarkerSyntax:
        return MarkerSyntax.html_comment()

    async def probe_sync_capabilities(self) -> frozenset[Capability]:
        return frozenset()
