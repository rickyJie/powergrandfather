"""AgentConversation orchestration — spawn / end, single-instance enforcement.

A "conversation" is the runtime pairing of an AgentDefinition with a freshly
spawned claude PTY (a Session row of type CHAT_AGENT). The SessionManager owns
the PTY; this module owns the AgentConversation row + the single-instance rule.

Single-instance rule (per agent):
  - Before spawning, we check for an existing AgentConversation with
    `ended_at IS NULL` for the same agent_def_id.
  - If one exists AND its Session is still alive (status RUNNING / IDLE /
    WAITING_INPUT / STARTING), `spawn` returns the existing row instead of
    creating a new one. Callers should treat both code paths identically:
    "here is the conversation you should chat with".
  - If the existing AgentConversation's Session has already exited / crashed,
    we mark the AgentConversation ended and proceed to spawn fresh.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import AgentConversation, AgentDefinition, Session
from csm.models.session import SessionStatus, SessionType
from csm.modules.session_manager.manager import SessionManager
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)

_LIVE_STATUSES = {
    SessionStatus.STARTING,
    SessionStatus.RUNNING,
    SessionStatus.IDLE,
    SessionStatus.WAITING_INPUT,
}

# Adapter name constants — the string literals are OK here (this file is
# adapter-aware by design; the alternative is exposing a
# `Capability.SYSTEM_PROMPT_FLAG` and threading that through, which is
# more machinery than the two-branch decision warrants right now).
_CLAUDE_AGENT_NAME = "claude"


class AgentConversationError(Exception):
    pass


@dataclass
class SpawnResult:
    conversation: AgentConversation
    session: Session
    reused: bool  # True if we returned an existing live conversation


class AgentConversationManager:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        session_manager: SessionManager,
    ):
        self._sm = sessionmaker
        self._sessman = session_manager

    async def _resolve_default_agent(self) -> str:
        """Look up UserPreference.default_agent for v2 multi-agent dispatch.

        Falls back to "claude" if the row is missing (fresh DB / test
        without the seed migration) — matches the SessionManager's own
        default when no adapter_registry is wired.
        """
        try:
            from csm.models import UserPreference
            async with self._sm() as db:
                pref = await db.get(UserPreference, 1)
                if pref is not None:
                    return pref.default_agent
        except Exception:
            # DB read failure — prefer to spawn than to block.
            pass
        return "claude"

    async def spawn(self, agent_def_id: str) -> SpawnResult:
        """Spawn (or reuse) a chat conversation for the given agent.

        Raises:
          AgentConversationError if the agent does not exist.
        """
        async with self._sm() as db:
            agent: AgentDefinition | None = await db.get(AgentDefinition, agent_def_id)
            if agent is None:
                raise AgentConversationError(f"agent {agent_def_id!r} not found")

            existing = await self._find_live_conversation(db, agent_def_id)
            if existing is not None:
                conv, sess = existing
                return SpawnResult(conversation=conv, session=sess, reused=True)

        # Multi-agent v2: resolve which CLI-adapter to spawn against.
        # AgentDefinition doesn't yet carry an `agent` field (that's a v3
        # concern), so fall back to UserPreference.default_agent.
        resolved_agent = await self._resolve_default_agent()

        # Build extra args for the claude CLI:
        #   * agent prompt via --append-system-prompt so claude treats it
        #     as configuration, not a user message
        #   * --disallowedTools Skill if the agent is skill-disabled
        # Both flags are gated to argv[0]==claude by SessionManager, so
        # passing them for codex/other is safe (silently dropped).
        extra_args: list[str] = []
        if resolved_agent == _CLAUDE_AGENT_NAME:
            if agent.prompt_cached:
                extra_args += ["--append-system-prompt", agent.prompt_cached]
            if agent.disable_skills:
                extra_args += ["--disallowedTools", "Skill"]

        # For adapters that don't have a --append-system-prompt equivalent
        # (codex, gemini, ...), inject the agent's system prompt as an
        # initial user message. Less clean than a system prompt (the CLI
        # treats it as a real conversation turn), but far better than
        # spawning a bare shell with no persona.
        initial_prompt: str | None = None
        if resolved_agent != _CLAUDE_AGENT_NAME and agent.prompt_cached:
            initial_prompt = agent.prompt_cached

        session_row = await self._sessman.create_session(
            cwd=agent.cwd,
            type=SessionType.CHAT_AGENT,
            title=agent.display_name,
            initial_prompt=initial_prompt,
            extra_claude_args=extra_args or None,
            agent=resolved_agent,
        )
        async with self._sm() as db:
            conv = AgentConversation(
                agent_def_id=agent_def_id,
                session_id=session_row.id,
                title=None,
                last_activity_ts=now_utc_naive(),
            )
            db.add(conv)
            await db.commit()
            await db.refresh(conv)
            sess = await db.get(Session, session_row.id)
        return SpawnResult(conversation=conv, session=sess, reused=False)

    async def end(self, conversation_id: str) -> bool:
        """End a conversation by killing its session and marking the row ended.

        Returns False if the conversation does not exist or was already ended.
        """
        async with self._sm() as db:
            conv: AgentConversation | None = await db.get(AgentConversation, conversation_id)
            if conv is None:
                return False
            if conv.ended_at is not None:
                return False
            sess: Session | None = await db.get(Session, conv.session_id)
            session_id = sess.id if sess is not None else None
            conv.ended_at = now_utc_naive()
            await db.commit()
        if session_id is not None:
            try:
                await self._sessman.stop_session(session_id, graceful=True)
            except Exception:
                log.exception("stop_session failed for %s", session_id)
        return True

    async def get(self, conversation_id: str) -> tuple[AgentConversation, Session] | None:
        async with self._sm() as db:
            conv = await db.get(AgentConversation, conversation_id)
            if conv is None:
                return None
            sess = await db.get(Session, conv.session_id)
            return conv, sess

    async def list_for_agent(
        self, agent_def_id: str, include_ended: bool = False
    ) -> list[AgentConversation]:
        async with self._sm() as db:
            stmt = select(AgentConversation).where(
                AgentConversation.agent_def_id == agent_def_id
            )
            if not include_ended:
                stmt = stmt.where(AgentConversation.ended_at.is_(None))
            stmt = stmt.order_by(AgentConversation.created_at.desc())
            res = await db.execute(stmt)
            return list(res.scalars().all())

    async def active_conversation_for_agent(
        self, agent_def_id: str
    ) -> AgentConversation | None:
        """Return the single live conversation for an agent, or None.

        Used by the UI to compute the card status dot (idle vs running) and
        to route the card-click action.
        """
        async with self._sm() as db:
            res = await self._find_live_conversation(db, agent_def_id)
            return res[0] if res else None

    async def send_user_message(self, conversation_id: str, text: str) -> bool:
        """Write `text` into the conversation's PTY. Bumps last_activity_ts.

        Returns False if the conversation does not exist, is ended, or its
        session is no longer live.

        Framing is per-CLI (`SessionManager.frame_prose_sequence`), not a
        hard-coded CRLF: codex's TUI discards a burst that carries the text and
        the Enter together, and claude reads a long burst as a paste and never
        acts on the trailing CR. Either way an Agent Deck chat would accept
        messages and never run them, so this uses the same framing path as the
        session-message endpoint rather than its own.

        A short write now reports False. It used to return True for any write
        that moved at least one byte — but the submit CR is the LAST thing in
        the sequence, so a truncated write is precisely the case where the
        message doesn't run.
        """
        async with self._sm() as db:
            conv = await db.get(AgentConversation, conversation_id)
            if conv is None or conv.ended_at is not None:
                return False
            session_id = conv.session_id
            sess = await db.get(Session, session_id)
            agent = sess.agent if sess is not None else None
            conv.last_activity_ts = now_utc_naive()
            if conv.title is None and text.strip():
                conv.title = text.strip()[:200]
            await db.commit()
        chunks = self._sessman.frame_prose_sequence(agent, text.rstrip("\n"))
        written, total = await self._sessman.write_input_sequence(
            session_id, chunks
        )
        return written >= total > 0

    # ---- helpers ----
    async def _find_live_conversation(
        self, db, agent_def_id: str
    ) -> tuple[AgentConversation, Session] | None:
        """Return (conv, session) if a live conversation exists for this agent,
        otherwise None. Also reaps stale (session-dead) AgentConversation rows
        by setting their ended_at.
        """
        res = await db.execute(
            select(AgentConversation)
            .where(
                AgentConversation.agent_def_id == agent_def_id,
                AgentConversation.ended_at.is_(None),
            )
            .order_by(AgentConversation.created_at.desc())
        )
        for conv in res.scalars().all():
            sess = await db.get(Session, conv.session_id)
            if sess is not None and sess.status in _LIVE_STATUSES:
                return conv, sess
            # Reap: session gone but row still marked open.
            conv.ended_at = now_utc_naive()
        await db.commit()
        return None
