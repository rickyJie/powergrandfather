"""SyncAgent — LLM decision engine for multi-agent config sync (v2).

On each tick, receives:
- `input_state`: the raw (un-redacted) payload built by
  `state.build_input_payload()` — full CSM DB rows + full agent-side
  memory / mcp / skills for reasoning.
- `policy_prompt`: the prompt loaded from `sync_policy(id=1)`.

Returns:
- `SyncDecisionsPayload` (Pydantic-validated) OR `None` on parse failure.
- A `meta` dict with token usage, duration, prompt_hash, and (on
  parse failure) `parse_error` text.

Execution path (default): the decision runs as a **real AUTO session**
spawned through `SessionManager` under the user's *default agent*
(`UserPreference.default_agent` — claude / codex / …), exactly like an
automation stage. The session reads `sync_input.md` (policy + state) from a
scratch dir and writes its decisions to `decisions.json`; CSM harvests the
file. This is what lets sync run on whatever CLI the user is signed into —
**no `ANTHROPIC_API_KEY` required** — and works for codex too (it's just a
session spawn, not a headless `-p` call that codex lacks). File-based I/O
dodges both the 2000-char `assistant_text` truncation and argv length limits.

Fallback path: if no `session_manager`/`event_stream` was wired in (e.g. a
bare unit test) but `ANTHROPIC_API_KEY` is set, decide() falls back to a
direct `AsyncAnthropic` call (the original v1 behavior).

Guarded by:
- session path available (session_manager + event_stream) OR api key → enabled
- `CSM_SYNC_DISABLED=1` → forcibly off regardless
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.config import Settings
from csm.core.events import EventType
from csm.models.sync_policy import SyncPolicy
from csm.modules.sync.schema import SyncDecisionsPayload

if TYPE_CHECKING:
    from csm.core.event_stream import EventStream
    from csm.core.events import Event
    from csm.modules.session_manager.manager import SessionManager

log = logging.getLogger(__name__)


_DEFAULT_MODEL = "claude-haiku-4-5"

# The short instruction handed to the spawned agent as its initial prompt.
# The bulk (policy + state) lives in sync_input.md so we never risk an argv
# too-long (E2BIG / MAX_ARG_STRLEN) on large state and the agent reads it
# with its normal file tools.
_SESSION_PROMPT = (
    "You are an automated config-sync decision step — behave like a PURE "
    "FUNCTION, not an exploring agent. Read the file `sync_input.md` in your "
    "current working directory with the Read tool (page through it with the "
    "offset argument if it is long). It contains a sync policy followed by the "
    "current multi-agent config state as JSON. EVERYTHING you need is in that "
    "file and the policy it states. Do NOT run shell commands, do NOT parse the "
    "file with scripts, do NOT search the filesystem, do NOT look elsewhere for "
    "a schema definition — the policy fully describes the output shape. Decide "
    "the sync actions per the policy, then use the Write tool to save a SINGLE "
    "JSON object to a file named `decisions.json` in this same directory. "
    "Output ONLY that file — write no prose, add no commentary. End your turn "
    "as soon as the file is written."
)


class SyncAgent:
    """Decision-only front-end for the SyncOrchestrator.

    All persistent state (adapters, DB, ledger) is external — this class
    holds only the execution plumbing (session manager / event stream for the
    primary path, or an Anthropic client for the fallback path) + the policy
    loader.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        api_key: str | None = None,
        model: str | None = None,
        session_manager: SessionManager | None = None,
        event_stream: EventStream | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._session_manager = session_manager
        self._event_stream = event_stream
        # Model resolution: explicit arg > CSM_SYNC_MODEL > default haiku.
        # Only consumed by the fallback Anthropic path (the session path uses
        # whatever model the spawned CLI is configured for).
        cfg_model_env = os.environ.get("CSM_SYNC_MODEL", "").strip()
        self._model = model or cfg_model_env or _DEFAULT_MODEL

        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._disabled = os.environ.get("CSM_SYNC_DISABLED") == "1"
        self._cfg = Settings()

        # Anthropic client is only built for the fallback path.
        self._client: Any = None
        if not self._disabled and self._session_manager is None and self._api_key:
            try:
                import anthropic  # type: ignore
                self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            except ImportError:
                log.warning("anthropic SDK not installed; SyncAgent api fallback off")
                self._client = None

    @property
    def _session_path_ready(self) -> bool:
        return self._session_manager is not None and self._event_stream is not None

    @property
    def enabled(self) -> bool:
        if self._disabled:
            return False
        # Primary: spawn a session under the default agent (no key needed).
        if self._session_path_ready:
            return True
        # Fallback: direct Anthropic API (needs a working client).
        return self._client is not None

    # ---- policy loader ---------------------------------------------------

    async def load_policy_prompt(self) -> tuple[str, str]:
        """Return `(prompt_text, prompt_hash_hex)` from sync_policy(id=1).

        Raises RuntimeError if the singleton row is missing — that's an
        indication migrations weren't applied.
        """
        async with self._sm() as db:
            row = await db.get(SyncPolicy, 1)
            if row is None:
                raise RuntimeError(
                    "sync_policy(id=1) row missing; run alembic upgrade head",
                )
            prompt = row.prompt
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return prompt, h

    async def _resolve_default_agent(self) -> str:
        """The agent whose CLI executes the decision — the user's default.

        Reads `UserPreference.default_agent` (same source workflow/session
        creation use); falls back to 'claude' when unset. This is what makes
        sync honor the user's chosen agent instead of hard-coding claude.
        """
        try:
            from csm.models import UserPreference
            async with self._sm() as db:
                pref = await db.get(UserPreference, 1)
            if pref is not None and pref.default_agent:
                return str(pref.default_agent)
        except Exception:
            log.exception("sync: default_agent lookup failed; using claude")
        return "claude"

    def _ensure_claude_trust(self, cwd: str) -> None:
        """Pre-accept Claude Code's per-directory trust prompt for `cwd`.

        A never-seen directory makes an AUTO `claude` session hang on the
        "Is this a project you trust?" dialog (NOT bypassed by
        `--dangerously-skip-permissions`). Claude records trust per-path in
        `~/.claude.json`; we seed `hasTrustDialogAccepted=true` for `cwd`
        BEFORE spawning so the first run doesn't hang. Idempotent: skips the
        write once the entry exists, so steady state does no I/O and the
        (rare) race with a concurrent claude rewriting the file self-heals on
        the next tick. Best-effort — a failure just falls back to the old
        hang, which the decide wall-clock timeout surfaces as an error.
        """
        try:
            cfg_path = Path.home() / ".claude.json"
            if not cfg_path.is_file():
                return
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            projects = data.setdefault("projects", {})
            entry = projects.get(cwd)
            if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
                return  # already trusted → no write, no race
            entry = entry if isinstance(entry, dict) else {}
            entry["hasTrustDialogAccepted"] = True
            entry.setdefault("hasCompletedProjectOnboarding", True)
            entry.setdefault("projectOnboardingSeenCount", 1)
            projects[cwd] = entry
            # Atomic replace so we never leave a half-written config.
            tmp = cfg_path.with_name(".claude.json.csm-tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, cfg_path)
        except Exception:
            log.exception("sync: could not pre-seed claude trust for %s", cwd)

    # ---- decide ---------------------------------------------------------

    async def decide(
        self, input_state: dict[str, Any],
    ) -> tuple[SyncDecisionsPayload | None, dict[str, Any]]:
        """Ask the SyncAgent for decisions on `input_state`.

        Returns `(payload, meta)` where `payload` is `None` on execution error
        OR schema-violation parse failure (see `meta["error"]` /
        `meta["parse_error"]`).
        """
        if not self.enabled:
            return None, {
                "error": "sync_agent_disabled",
                "hint": (
                    "no session manager wired and no usable ANTHROPIC_API_KEY, "
                    "or CSM_SYNC_DISABLED=1"
                ),
            }

        prompt, prompt_hash = await self.load_policy_prompt()

        if self._session_path_ready:
            # Pretty-print for the session path so the agent can read the state
            # line-by-line with the Read tool instead of resorting to shell/
            # python to crack a single 500KB line.
            user_text = self._build_user_text(input_state, pretty=True)
            raw_text, meta = await self._decide_via_session(
                policy=prompt, user_text=user_text, prompt_hash=prompt_hash,
            )
        else:
            user_text = self._build_user_text(input_state)
            raw_text, meta = await self._decide_via_api(
                policy=prompt, user_text=user_text, prompt_hash=prompt_hash,
            )

        if raw_text is None:
            return None, meta
        return self._parse_decisions(raw_text, meta)

    def _build_user_text(self, input_state: dict[str, Any], pretty: bool = False) -> str:
        """Deterministic JSON of the state + optional per-tick user intent.

        `pretty=True` indents the JSON (for the session path, so the agent can
        read it with the Read tool); the API path keeps it compact for cache.
        """
        user_text = json.dumps(
            input_state, sort_keys=True, ensure_ascii=False, default=str,
            indent=2 if pretty else None,
        )
        meta = input_state.get("meta") if isinstance(input_state, dict) else None
        intent = (meta or {}).get("user_intent") if isinstance(meta, dict) else None
        if isinstance(intent, str) and intent.strip():
            user_text += (
                "\n\n# User intent for THIS tick\n"
                f"{intent.strip()}\n"
                "Bias your decisions toward satisfying this intent, but DO NOT "
                "override the safety rules: secrets stay human-gated, and any "
                "genuine cross-agent conflict still escalates to a pending "
                "decision — never auto-resolve one just to fulfil the intent."
            )
        return user_text

    # ---- primary path: spawn a session under the default agent ----------

    async def _decide_via_session(
        self, policy: str, user_text: str, prompt_hash: str,
    ) -> tuple[str | None, dict[str, Any]]:
        """Run the decision as an AUTO session and harvest `decisions.json`.

        Returns `(raw_text, meta)` — `raw_text` is the file contents, or
        `None` with `meta["error"]` set on spawn / timeout / missing-file.
        """
        agent_name = await self._resolve_default_agent()
        base_meta: dict[str, Any] = {
            "prompt_hash": prompt_hash,
            "model": f"session:{agent_name}",
            "token_usage": None,
        }

        # Stable scratch dir (NOT per-run). Claude Code shows a one-time
        # "trust this folder?" prompt for any unseen directory — which
        # `--dangerously-skip-permissions` does NOT bypass — and an AUTO
        # session hangs on it forever. A fixed dir + a pre-seeded trust flag
        # means the very first run doesn't hang and later runs never re-prompt.
        # Ticks are serialized (single tick lock) so reusing one dir is safe.
        # Same lesson the usage probe learned — see token/usage_polling.py.
        workdir = Path(self._cfg.sync_decide_cwd)
        decisions_path = workdir / "decisions.json"
        t0 = time.monotonic()
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            if agent_name == "claude":
                self._ensure_claude_trust(str(workdir))
            # Clear any stale decision file so we never harvest a prior run's.
            decisions_path.unlink(missing_ok=True)
            (workdir / "sync_input.md").write_text(
                f"{policy}\n\n"
                "# Current multi-agent config state\n\n"
                "```json\n"
                f"{user_text}\n"
                "```\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("sync: could not stage decide workdir")
            base_meta["error"] = f"stage_error: {type(exc).__name__}: {exc}"
            base_meta["duration_ms"] = int((time.monotonic() - t0) * 1000)
            return None, base_meta
        sid: str | None = None
        done = asyncio.Event()

        async def _handler(ev: Event) -> None:
            # payload csm_session_id is enriched by EventStream after the
            # session row commits, so filter our own session's events only.
            if sid is None or ev.payload.get("csm_session_id") != sid:
                return
            if ev.type in (
                EventType.MESSAGE_ASSISTANT_DONE,
                EventType.SESSION_ENDED,
                EventType.SESSION_CRASHED,
            ):
                done.set()

        sub_id = self._event_stream.subscribe(
            [
                EventType.MESSAGE_ASSISTANT_DONE,
                EventType.SESSION_ENDED,
                EventType.SESSION_CRASHED,
            ],
            _handler,
        )
        try:
            from csm.models.session import SessionType
            try:
                session = await self._session_manager.create_session(
                    cwd=str(workdir),
                    type=SessionType.AUTO,
                    title="sync-decide",
                    initial_prompt=_SESSION_PROMPT,
                    agent=agent_name,
                    # NOTE: do NOT pass `--disallowedTools` here. That flag is
                    # variadic (`<tools...>`) and greedily swallows every
                    # following argv token — including the prompt, which the
                    # manager appends last. The agent then gets an EMPTY prompt,
                    # sits idle, writes nothing, and the tick times out. Keeping
                    # the agent on-task is done via the prompt + the (now small,
                    # pretty-printed) input instead.
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("sync: decide session spawn failed agent=%s", agent_name)
                base_meta["error"] = f"spawn_error: {type(exc).__name__}: {exc}"
                base_meta["duration_ms"] = int((time.monotonic() - t0) * 1000)
                return None, base_meta
            sid = session.id

            timed_out = False
            try:
                await asyncio.wait_for(
                    done.wait(), timeout=float(self._cfg.sync_decide_timeout_sec),
                )
            except TimeoutError:
                timed_out = True
        finally:
            self._event_stream.unsubscribe(sub_id)
            if sid is not None:
                try:
                    await self._session_manager.stop_session(sid, graceful=True)
                except Exception:
                    log.exception("sync: stop decide session failed sid=%s", sid)

        base_meta["duration_ms"] = int((time.monotonic() - t0) * 1000)

        # Harvest the file regardless of timeout — the agent may have written
        # it just before the wall-clock ceiling.
        raw_text: str | None = None
        try:
            if decisions_path.is_file():
                raw_text = decisions_path.read_text(encoding="utf-8").strip()
        except Exception:
            log.exception("sync: reading decisions.json failed")

        if not raw_text:
            base_meta["error"] = (
                "decide_timeout: no decisions.json within "
                f"{self._cfg.sync_decide_timeout_sec}s"
                if timed_out
                else "no_decisions_file: agent ended turn without writing decisions.json"
            )
            return None, base_meta

        base_meta["raw_text"] = raw_text
        return raw_text, base_meta

    # ---- fallback path: direct Anthropic API ----------------------------

    async def _decide_via_api(
        self, policy: str, user_text: str, prompt_hash: str,
    ) -> tuple[str | None, dict[str, Any]]:
        t0 = time.monotonic()
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=8192,
                temperature=0,
                system=[{
                    "type": "text",
                    "text": policy,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.monotonic() - t0) * 1000)
            log.exception("SyncAgent Anthropic call failed")
            return None, {
                "error": f"anthropic_error: {type(exc).__name__}: {exc}",
                "duration_ms": duration_ms,
                "prompt_hash": prompt_hash,
                "model": self._model,
            }
        duration_ms = int((time.monotonic() - t0) * 1000)

        raw_text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        ).strip()

        token_usage: dict[str, Any] | None = None
        usage = getattr(resp, "usage", None)
        if usage is not None:
            token_usage = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "cache_creation_input_tokens": getattr(
                    usage, "cache_creation_input_tokens", None,
                ),
                "cache_read_input_tokens": getattr(
                    usage, "cache_read_input_tokens", None,
                ),
            }

        meta: dict[str, Any] = {
            "prompt_hash": prompt_hash,
            "model": self._model,
            "duration_ms": duration_ms,
            "raw_text": raw_text,
            "token_usage": token_usage,
        }
        return raw_text, meta

    # ---- shared parse ---------------------------------------------------

    def _parse_decisions(
        self, raw_text: str, meta: dict[str, Any],
    ) -> tuple[SyncDecisionsPayload | None, dict[str, Any]]:
        """Peel an optional ```json fence, json.loads, pydantic-validate."""
        if raw_text.startswith("```"):
            body = raw_text.split("```", 2)[1]
            if body.startswith("json\n"):
                body = body[len("json\n"):]
            elif body.startswith("json"):
                body = body[len("json"):].lstrip()
            raw_text_for_parse = body.strip()
        else:
            raw_text_for_parse = raw_text

        try:
            parsed_obj = json.loads(raw_text_for_parse)
        except json.JSONDecodeError as exc:
            meta["parse_error"] = f"json_decode: {exc}"
            return None, meta

        # Safety net: a verbose model (e.g. Opus xhigh) can write a rationale /
        # summary past the schema caps, which would fail the WHOLE tick over a
        # non-critical explanation field. Clamp them before validation.
        self._clamp_text_fields(parsed_obj)

        try:
            payload = SyncDecisionsPayload.model_validate(parsed_obj)
        except ValidationError as exc:
            meta["parse_error"] = f"pydantic: {exc.errors()[:5]}"
            return None, meta

        return payload, meta

    @staticmethod
    def _clamp_text_fields(obj: Any) -> None:
        """Truncate over-long rationale/summary in-place so one verbose field
        can't sink a whole tick. Best-effort; ignores unexpected shapes."""
        if not isinstance(obj, dict):
            return
        summ = obj.get("summary")
        if isinstance(summ, str) and len(summ) > 1000:
            obj["summary"] = summ[:1000]
        for d in obj.get("decisions") or []:
            if isinstance(d, dict):
                r = d.get("rationale")
                if isinstance(r, str) and len(r) > 4000:
                    d["rationale"] = r[:4000]


__all__ = ["SyncAgent"]
