"""Minimal stub adapter for tests that need a CLIAdapter without pulling in
the real Claude/Codex machinery. Keeps M0 tests independent of M1 code."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from csm.backends.base import (
    AdapterArgvResult,
    AdapterStatus,
    Capability,
    CLIAdapter,
    FlagDescriptor,
    MarkerSyntax,
)
from csm.core.events import Event


class FakeAdapter:
    """Bare-bones CLIAdapter for M0 tests.

    Doesn't do anything useful — just satisfies the Protocol shape so
    registry / resolver code has real objects to operate on.
    """

    def __init__(
        self,
        name: str = "fake",
        display_name: str = "Fake CLI",
        installed: bool = True,
        authenticated: bool = True,
        capabilities: frozenset[Capability] | None = None,
        color: str = "#888888",
        flags: list[FlagDescriptor] | None = None,
        default_argv: str | None = None,
    ):
        self.name = name
        self.display_name = display_name
        self.icon = name[0].upper() if name else "?"
        self.color = color
        self.capabilities: frozenset[Capability] = capabilities or frozenset()
        self._installed = installed
        self._authenticated = authenticated
        self._flags = flags or []
        self._default_argv = default_argv if default_argv is not None else name

    def home_dir(self) -> Path:
        return Path(f"/tmp/fake-{self.name}")

    def default_home_name(self) -> str:
        return f".{self.name}"

    def auth_file(self) -> Path | None:
        return None

    def probe(self) -> AdapterStatus:
        return AdapterStatus(
            name=self.name,
            installed=self._installed,
            authenticated=self._authenticated,
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

    def build_argv(
        self,
        base_argv: list[str],
        cwd: str,
        *,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        extra_args: list[str] | None = None,
        resume_from: str | None = None,
    ) -> AdapterArgvResult:
        return AdapterArgvResult(argv=list(base_argv))

    def artifact_root(self) -> Path:
        return self.home_dir() / "sessions"

    def artifact_glob(self) -> str:
        return str(self.artifact_root() / "**" / "*.jsonl")

    def scan_events(self) -> list[Event]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, snap: dict[str, Any]) -> None:  # noqa: ARG002
        pass

    def take_newly_seen(self) -> set[str]:
        return set()

    def tail_states(self) -> list[dict[str, Any]]:
        return []

    def install_hooks(self, project_root: Path, callback_url: str) -> None:  # noqa: ARG002
        pass

    def default_argv(self) -> str:
        return self._default_argv

    def flags_schema(self) -> list[FlagDescriptor]:
        return list(self._flags)

    # ---- multi-agent config sync (P0 v3 · stub) ----------------------
    # Safe-default stubs so isinstance(FakeAdapter(), CLIAdapter) keeps
    # passing after the Protocol was extended. Tests that want to
    # exercise sync flows should override on a subclass or use mock.

    def memory_paths(self, scope: str) -> list[Path]:  # noqa: ARG002
        return []

    def read_memory(self, path: Path) -> str:  # noqa: ARG002
        return ""

    def read_memory_full(self, scope: str) -> str | None:  # noqa: ARG002
        return None

    def write_memory_marker_block(
        self, path: Path, marker_id: str, body: str,  # noqa: ARG002
    ) -> None:
        raise NotImplementedError("FakeAdapter has no sync support.")

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
        raise NotImplementedError("FakeAdapter has no sync support.")

    async def mcp_remove(self, name: str) -> Any:  # noqa: ARG002
        raise NotImplementedError("FakeAdapter has no sync support.")

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

    def read_skill_bundle(self, name: str) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    def write_simple_skill(self, spec: dict[str, Any]) -> None:  # noqa: ARG002
        raise NotImplementedError("FakeAdapter has no sync support.")

    def write_skill_bundle(self, spec: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise NotImplementedError("FakeAdapter has no sync support.")

    def remove_skill(self, name: str) -> None:  # noqa: ARG002
        raise NotImplementedError("FakeAdapter has no sync support.")

    def marker_syntax(self) -> MarkerSyntax:
        return MarkerSyntax.html_comment()

    async def probe_sync_capabilities(self) -> frozenset[Capability]:
        return frozenset()


def assert_conforms(adapter: object) -> None:
    """Runtime check that adapter implements the CLIAdapter protocol.

    isinstance against `@runtime_checkable` Protocol only checks method
    NAMES, not signatures. Good enough for a smoke check that we didn't
    typo something.
    """
    assert isinstance(adapter, CLIAdapter), (
        f"{type(adapter).__name__} does not conform to CLIAdapter"
    )
