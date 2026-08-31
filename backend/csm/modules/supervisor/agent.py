"""SupervisorAgent — LLM-driven post-run review for AUTO sessions (F2).

After every SESSION_ENDED for an AUTO session, ask Claude whether the run needs
human review. If yes → emit AUTO_NEEDS_REVIEW notification.

Uses Anthropic API with prompt caching on the (static) system prompt so we don't
pay full tokens on every call.

Activation:
  - `ANTHROPIC_API_KEY` env var present → agent enabled
  - `CSM_SUPERVISOR_MODEL` (default: claude-haiku-4-5) — cheap fast model
  - `CSM_SUPERVISOR_DISABLED=1` to forcibly disable even with API key set
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.config import Settings
from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Run, Session
from csm.models.session import SessionType

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Supervisor Agent for the Claude Session Manager (CSM).

For each completed AUTO session you receive a structured summary of its outcome:
- the task name + prompt
- exit_code
- which `output_globs` produced files (and last-line previews)
- the tail of the PTY buffer (last ~2 KB)

Your job is to decide ONE thing: should a human review this run before the next
scheduled iteration fires?

Return STRICT JSON with exactly these keys:
  {
    "needs_review": true | false,
    "reason": "<one short sentence>",
    "category": "ok" | "error" | "ambiguous" | "stuck" | "incomplete"
  }

Reviewing should be RARE — only when something clearly looks wrong:
- exit_code != 0 AND the failure isn't a known clean shutdown (SIGINT/130/SIGTERM/143)
- expected output files missing
- output preview shows "TODO", "FIXME", "STUB", "MOCK", "WIP", "??"
- PTY tail contains stack traces, "Error:", "Traceback", "panic", "FATAL"
- task explicitly asks for a decision and there's no clear answer in outputs

If the run produced expected outputs and exited cleanly → needs_review=false, category=ok.

Output JSON ONLY. No prose, no markdown fences."""


class SupervisorAgent:
    """Subscribes to SESSION_ENDED; for AUTO sessions, asks Claude if review needed."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        event_stream: EventStream,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._es = event_stream
        # Instantiate fresh so tests/monkeypatch see current env (module-level
        # `settings` in csm.config is frozen at import time).
        cfg = Settings()
        # Model resolution: explicit arg > settings.supervisor_model (CSM_SUPERVISOR_MODEL).
        self._model = model or cfg.supervisor_model
        self._sub_id: str | None = None
        # ANTHROPIC_API_KEY is a cross-CLI convention (OS-level), not a CSM_* var —
        # deliberately not migrated into Settings.
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._disabled = cfg.supervisor_disabled
        self._client: Any = None
        if self.enabled:
            try:
                import anthropic  # type: ignore
                self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            except ImportError:
                log.warning("anthropic SDK not installed; SupervisorAgent disabled")
                self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and not self._disabled

    async def start(self) -> None:
        if not self.enabled or self._client is None:
            return
        if self._sub_id is None:
            self._sub_id = self._es.subscribe([EventType.SESSION_ENDED], self._on_session_ended)

    async def stop(self) -> None:
        if self._sub_id is not None:
            self._es.unsubscribe(self._sub_id)
            self._sub_id = None

    async def _on_session_ended(self, event: Event) -> None:
        csm_sid = (event.payload or {}).get("csm_session_id")
        if not csm_sid:
            return
        async with self._sm() as db:
            sess = await db.get(Session, csm_sid)
            if sess is None or sess.type != SessionType.AUTO:
                return
            run = None
            if sess.associated_run_id:
                run = await db.get(Run, sess.associated_run_id)
        exit_code = (event.payload or {}).get("exit_code")
        context = await self._build_context(sess, run, exit_code)
        try:
            verdict = await self._ask_claude(context)
        except Exception:
            log.exception("supervisor LLM call failed for session %s", csm_sid)
            return
        if verdict.get("needs_review"):
            await self._emit_review_notification(sess, verdict)

    async def _build_context(self, sess, run, exit_code) -> str:
        task_name = sess.title or "(unknown)"
        run_id = run.id if run else None
        params = run.parameters if run else {}
        outputs_lines: list[str] = []
        if run:
            from csm.models import Output
            async with self._sm() as db:
                outs = (await db.execute(
                    select(Output).where(Output.run_id == run.id).limit(20)
                )).scalars().all()
            for o in outs:
                preview = (o.preview or "").strip().splitlines()[-3:]
                outputs_lines.append(f"- {o.path}\n    {' | '.join(preview)}")
        outputs_block = "\n".join(outputs_lines) if outputs_lines else "(no outputs discovered)"
        return (
            f"task: {task_name}\n"
            f"run_id: {run_id}\n"
            f"parameters: {json.dumps(params, default=str)}\n"
            f"exit_code: {exit_code}\n"
            f"outputs:\n{outputs_block}\n"
        )

    async def _ask_claude(self, context: str) -> dict[str, Any]:
        # Use prompt caching on the system prompt — paid once per ~5 min window.
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=300,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": context}],
        )
        # Concatenate text blocks
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        # Tolerate optional ```json fences just in case
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json\n").strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            log.warning("supervisor: LLM returned non-JSON: %s", text[:200])
            return {}

    async def _emit_review_notification(self, sess, verdict: dict[str, Any]) -> None:
        """Emit SUPERVISOR_REVIEW_REQUESTED; NotificationBus routes it to
        an AUTO_NEEDS_REVIEW notification.

        `mission_id` is looked up via the session's associated_run so the
        notification frontend can jump straight into the mission detail
        modal on click. Falls back to None for legacy sessions with no
        associated run — the click just no-ops in that case."""
        mission_id: str | None = None
        if sess.associated_run_id:
            async with self._sm() as db:
                run = await db.get(Run, sess.associated_run_id)
                if run is not None:
                    mission_id = getattr(run, "mission_id", None)
        await self._es.emit(Event(
            type=EventType.SUPERVISOR_REVIEW_REQUESTED,
            ts=datetime.now(UTC),
            session_id=None,
            project_path=sess.cwd,
            payload={
                "csm_session_id": sess.id,
                "mission_id": mission_id,
                "reason": verdict.get("reason", "(no reason)"),
                "category": verdict.get("category", "unknown"),
                "session_title": sess.title,
            },
        ))
