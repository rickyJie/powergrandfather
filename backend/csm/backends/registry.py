"""Runtime registry of CLI adapters.

Design notes:

- The registry is an **instance**, not a module-level global. It gets
  constructed in `lifespan()` and published on `app.state.adapter_registry`.
  Module-level dicts would leak between tests (mock adapter registered in
  test A visible in test B), which is exactly the anti-pattern the
  backend-review flagged as P1.

- Adapters register **themselves at instance construction time** — you
  pass a list of CLIAdapter instances to `AdapterRegistry(...)` or call
  `.register()` afterwards. `csm/backends/__init__.py` provides the
  canonical `build_default_registry()` factory that instantiates each
  known adapter class; lifespan calls that.

- `enabled()` treats registered adapters as enabled by default. An explicit
  false `CSM_ENABLE_<AGENT>` env flag turns one off without uninstalling it.
  This matches the session-create defaults: an adapter that the API allows
  users to spawn must also have its event stream consumed.

- No caching. `enabled()` re-reads env vars each call; n<=~5 adapters,
  cost is trivial and hot-swap (test toggles env inside a test) works.

- No thread lock. Registration happens at lifespan start (single-threaded);
  reads at request time are ok because dict reads are atomic in CPython
  and we never mutate the set after startup.
"""
from __future__ import annotations

import os

from csm.backends.base import AdapterStatus, CLIAdapter
from csm.backends.errors import AgentUnavailableError, UnknownAgentError


class AdapterRegistry:
    """Container that holds all registered CLIAdapters.

    Typical lifecycle:
        registry = AdapterRegistry([ClaudeAdapter(), CodexAdapter()])
        app.state.adapter_registry = registry
        # ...
        adapter = registry.get("claude")
    """

    def __init__(self, adapters: list[CLIAdapter] | None = None):
        self._by_name: dict[str, CLIAdapter] = {}
        for a in adapters or []:
            self.register(a)

    def register(self, adapter: CLIAdapter) -> None:
        """Add an adapter. Duplicate names raise ValueError.

        Registration is idempotent on identity — registering the SAME
        instance twice is a no-op. Registering a different instance with
        the same name is an error (helps catch double-registration bugs).
        """
        existing = self._by_name.get(adapter.name)
        if existing is adapter:
            return
        if existing is not None:
            raise ValueError(
                f"adapter {adapter.name!r} already registered as {type(existing).__name__}; "
                f"cannot register {type(adapter).__name__}"
            )
        self._by_name[adapter.name] = adapter

    # ---- lookups ----

    def get(self, name: str) -> CLIAdapter:
        """Return adapter by name. Raises UnknownAgentError if not registered.

        The API layer catches UnknownAgentError and returns 400. Domain
        code should not swallow it; if the name came from user input it's
        a client error, if it came from a Session row it's a data
        corruption bug and we want to fail loud.
        """
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownAgentError(name, known=list(self._by_name.keys())) from None

    def get_available(self, name: str) -> CLIAdapter:
        """Like `get()` but also runs `probe()` and raises
        AgentUnavailableError if the adapter is registered but unusable.
        API maps to 503."""
        adapter = self.get(name)
        status = adapter.probe()
        if not status.usable:
            reason = status.error or (
                "not installed" if not status.installed
                else "not authenticated"
            )
            raise AgentUnavailableError(name, reason)
        return adapter

    def all(self) -> list[CLIAdapter]:
        """Every registered adapter, regardless of env-enable state.
        Order is registration order (dict preserves insertion since 3.7)."""
        return list(self._by_name.values())

    def enabled(self) -> list[CLIAdapter]:
        """Registered adapters not explicitly disabled by environment.

        Missing `CSM_ENABLE_<NAME>` means enabled. Explicit false values
        (`0`, `false`, `no`, `off`) disable the adapter; explicit true values
        keep it enabled. This is intentionally opt-out: the old opt-in
        behaviour allowed SessionManager to spawn Codex while EventStream
        silently skipped every Codex rollout, leaving status stuck at
        ``running`` and token/notification ingestion dark.

        Note: this is a *runtime feature switch* — separate from whether
        the adapter is `usable()`. An enabled adapter may still be
        unavailable (binary missing); UI shows both states.
        """
        return [a for a in self._by_name.values() if is_agent_enabled(a.name)]

    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def statuses(self) -> list[AdapterStatus]:
        """Probe every registered adapter and return their statuses.
        Convenience for `GET /api/backends`."""
        return [a.probe() for a in self._by_name.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


def is_agent_enabled(agent_name: str) -> bool:
    """Is `CSM_ENABLE_<AGENT>` off?

    Missing means enabled. Truthy: ``1/true/yes/on``; falsy:
    ``0/false/no/off`` (case-insensitive). An explicitly present but
    unrecognised value is treated as false so a typo fails closed.

    The process environment wins, but an absent variable falls back to the
    matching `Settings.enable_<agent>` field. That fallback is what makes the
    flag mean the same thing everywhere: `Settings` also loads `.env`, so
    without it a flag set THERE applied only where the code happened to read
    `settings` — session creation refused to spawn codex while EventStream,
    reading os.environ alone, cheerfully kept tailing rollouts and billing
    tokens. Half-disabled is worse than either state.

    Imported lazily: `csm.config` pulls in `csm.core.paths`, and this module
    is imported during adapter construction.
    """
    raw = os.environ.get(f"CSM_ENABLE_{agent_name.upper()}")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from csm.config import settings

        configured = getattr(settings, f"enable_{agent_name.lower()}", None)
    except Exception:  # pragma: no cover — settings must never gate adapters
        return True
    return configured if isinstance(configured, bool) else True


__all__ = ["AdapterRegistry", "is_agent_enabled"]
