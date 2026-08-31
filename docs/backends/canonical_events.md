# Canonical event model — the contract every adapter meets

Every CLIAdapter's `scan_events()` returns `csm.core.events.Event` instances.
This document is the source of truth for what those events mean, what
fields they carry, and how adapter-specific signals may extend them
without breaking the abstraction.

## The rule

Downstream code (SessionManager, TokenAggregator, NotificationBus,
WorkflowOrchestrator, SupervisorAgent, AgentDeck, EventStream) MAY:

- Read `Event.type`, `Event.session_id`, `Event.ts`, `Event.project_path`,
  `Event.payload[<field>]`.
- Read `payload["_<agent>_*"]` adapter-specific extensions when the
  logic legitimately wants that signal (e.g. codex-specific
  reasoning-token accounting in a codex-aware metric).

Downstream code MUST NOT:

- Branch on `agent_name == "..."` or `session.agent == "..."` outside
  `csm/backends/`. That's the abstraction-leak pattern the whole
  refactor exists to kill. Use `Capability` checks instead.

The `scripts/lint-agent-abstraction.sh` script enforces this at CI.

## Event types every adapter must be able to emit

Not every adapter emits every type — some CLIs simply don't have the
concept. The table is what's expressible:

| EventType                | Semantic                                | Claude source                         | Codex source                              |
|--------------------------|-----------------------------------------|---------------------------------------|-------------------------------------------|
| SESSION_STARTED          | New session began                       | First-seen JSONL + ≥1 record parsed   | `session_meta` record                     |
| MESSAGE_USER_SENT        | User turn submitted                     | `message.role=="user"`                | `event_msg` where `payload.type=="user_message"` |
| MESSAGE_ASSISTANT_DONE   | Assistant turn concluded                | `message.role=="assistant"` + `stop_reason=="end_turn"` | `event_msg` where `payload.type=="task_complete"` |
| TOOL_INVOKED             | Assistant called a tool                 | assistant message w/ `tool_use` block | (not yet mapped)                          |
| TOOL_COMPLETED           | Tool result observed                    | user message w/ `tool_result` block   | (not yet mapped)                          |
| USAGE_RECORDED           | Token accounting update                 | `message.usage` on assistant          | `event_msg` where `payload.type=="token_count"` |
| API_ERROR                | API returned an error                   | `isApiErrorMessage: true`             | (not yet mapped)                          |
| RATE_LIMIT_HIT           | 429 / hit-limit signal                  | error text "hit your limit"           | (not yet mapped)                          |
| SESSION_IDLE             | Session hasn't produced output for N min| watchdog derives from mtime           | watchdog derives from mtime               |
| SESSION_ENDED            | Session exited cleanly                  | SessionManager (from PID)             | SessionManager (from PID)                 |
| SESSION_CRASHED          | Session died unexpectedly               | SessionManager (from PID)             | SessionManager (from PID)                 |

Adapter authors: **you don't have to emit every type**. Emit what the CLI
can honestly report. Downstream consumers are already defensive about
"never saw this event" cases (a session that only emits SESSION_STARTED
+ SESSION_ENDED is a valid session, just uninteresting).

## The `_<agent>_*` payload extension namespace

When your CLI reports a signal that has no canonical equivalent, put it
in `payload` under a key prefixed with `_<agent>_`. Examples that are
already in the tree:

```python
# CodexAdapter.derive_events, USAGE_RECORDED event
payload = {
    "input_tokens": ...,
    "output_tokens": ...,
    # Canonical fields above.
    # Codex-only:
    "_codex_reasoning_output_tokens": last.get("reasoning_output_tokens", 0),
    "_codex_rate_limits": payload.get("rate_limits"),
}
```

Downstream code MAY read these fields. It MUST NOT rely on them existing.
When both claude and codex share a concept (e.g. cache-read tokens), give
it a **canonical** name in the payload, don't use the `_<agent>_*` prefix.

## Anti-patterns

**Bad — leaks adapter identity into domain code:**

```python
# WRONG
if session.agent == "codex":
    charge_reasoning_tokens(...)
```

**Good — capability check + payload extension:**

```python
# RIGHT
reasoning = event.payload.get("_codex_reasoning_output_tokens", 0)
if reasoning:
    charge_reasoning_tokens(reasoning)
```

The second version works uniformly for any future adapter that reports
reasoning tokens under the same key convention.

## Ordering guarantees

- Within a single session, events from the same adapter arrive in
  timestamp order.
- Across adapters, ordering is NOT guaranteed. EventStream fans out
  `scan_events()` calls concurrently and emits results as they land.
  If your consumer needs global ordering (rare), sort by `ts` yourself.

## Testing your adapter's canonical events

`tests/unit/backends/test_<name>_adapter.py::test_scan_events_*` is the
place. Assert that every mapping listed above (that your adapter
implements) fires an Event with the correct `type` + expected payload
keys. See `test_codex_adapter.py::test_scan_events_derives_user_message_and_task_complete`
for a template.
