"""AgentDeck module — preconfigured Claude chat agents with card-based UI.

P1 layer: AgentDefinition CRUD. Higher layers (spawn, message pipeline) live
in conversation.py / jsonl_fast_tail.py / message_router.py and arrive in P2/P3.
"""
from csm.modules.agent.agent_store import AgentStore, AgentStoreError

__all__ = ["AgentStore", "AgentStoreError"]
