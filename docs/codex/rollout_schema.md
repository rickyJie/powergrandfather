# Codex rollout JSONL — observed schema

**Codex version:** `codex-cli 0.145.0`
**Source sample:** `~/.codex/sessions/2026/07/25/rollout-2026-07-25T13-45-45-<sid>.jsonl` (real user session, read-only inspection).
**Layout:** one JSON object per line. All records share top-level `{timestamp, type, payload}`.

## Top-level `type` values seen

| type | payload.type | Purpose |
|---|---|---|
| `session_meta` | — | Once at file open. Session identity + config. |
| `event_msg` | `task_started` | Start of a turn. Has `turn_id`, `started_at`, `model_context_window`. |
| `event_msg` | `user_message` | User input for this turn. |
| `event_msg` | `agent_message` | Model output. Streams? Sample shows one per turn with `phase=final_answer`. |
| `event_msg` | `token_count` | Usage snapshot AFTER assistant reply. Fields under `info.total_token_usage` and `info.last_token_usage`. |
| `event_msg` | `task_complete` | End of turn. Carries `last_agent_message`, `duration_ms`, `time_to_first_token_ms`. |
| `response_item` | `message` | Structured message record (role: user/assistant/system/developer, `content[]`). Overlaps event_msg. |
| `turn_context` | — | Per-turn sandbox/permission snapshot. Not needed for events. |
| `world_state` | — | Filesystem sandbox description. Not needed. |

## Mapping → CSM `EventType`

| CSM event | Source | Fields |
|---|---|---|
| `SESSION_STARTED` | `session_meta` | `payload.session_id`, `payload.cwd`, `payload.cli_version`, `payload.model_provider` |
| `MESSAGE_USER` | `event_msg` type=`user_message` | `payload.message` |
| `MESSAGE_ASSISTANT_DONE` | `event_msg` type=`task_complete` | `payload.last_agent_message`, `payload.duration_ms` |
| `USAGE_RECORDED` | `event_msg` type=`token_count` | `payload.info.last_token_usage.{input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}` |
| `SESSION_ACTIVITY` | any | derived from `timestamp` on every record |
| `SESSION_ENDED` | *not present in-file* | Must be inferred: process exit hook, or timeout of no new records. |

## Notable differences vs Claude JSONL

- **Session id format:** UUID v7 (`019f97ce-...`) — Codex format. Claude uses UUID v4.
- **No `session_id` on every record.** Only on `session_meta` (first line). CSM's `EventStream` currently assumes every record self-identifies — the tailer must remember the sid from line 0 for the whole file.
- **Rollout path is date-stamped:** `~/.codex/sessions/YYYY/MM/DD/rollout-<iso-ts>-<sid>.jsonl`. Claude uses `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`. **Cannot** reverse-map cwd from rollout path (unlike Claude).
- **Token usage is post-hoc, delta per turn (`last_token_usage`) + running total (`total_token_usage`).** Claude JSONL emits usage inline on assistant message records.
- **Codex `input_tokens` is inclusive.** `cached_input_tokens` and
  `cache_write_input_tokens` are detail counters inside `input_tokens`, not
  additional tokens. The adapter subtracts both before writing CSM's disjoint
  `input_tokens` / `cache_read_tokens` / `cache_creation_tokens` buckets.
- **`agent_message` may appear multiple times per turn** with `phase` field (`final_answer` and others). For CSM we care about `phase=final_answer` → `MESSAGE_ASSISTANT_DONE`. Non-final ones can be ignored or wired to a future streaming event.
- **No tool call records in this sample.** Longer session with tool use will show `response_item` with `payload.type=function_call` / `function_call_output` (per Codex TypeScript source), but need a separate sample to confirm exact keys.

## Session-id ↔ CSM row binding

Codex has no `--session-id` flag (confirmed in `codex --help`). Post-hoc binding options:

1. **Spawn-time PID → wait for the newest `rollout-*.jsonl` matching PID's cwd/timestamp window.** Racy but simple.
2. **Read `session_meta.payload.session_id` on first line, then update `Session.codex_session_id` after the fact.** Slight delay window.
3. **Use `codex resume --session-id <sid>` on subsequent invocations to control the id.** Only helps for resumes, not first spawn.

Recommended: option 2. Tailer discovers a new rollout file, reads line 0, and updates the `Session` row it can match by `cwd` + PID start time.

## Referenced by

- Future `backend/csm/adapters/jsonl_tail.py::CodexRolloutTailer`
- Future `backend/csm/core/event_stream.py::_derive_events_codex`
