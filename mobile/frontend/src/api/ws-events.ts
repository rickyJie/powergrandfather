// Discriminated union of every message the backend emits on the agent
// conversation WebSocket. Source of truth:
//   - backend/csm/api/agents.py::conversation_stream (WS lifecycle frames)
//   - backend/csm/modules/agent/message_router.py::route_record (per-line events)
//
// Keep this file in sync when the backend adds a new event type.
// Consumers should always narrow via the `type` discriminant.

/** WS envelope frames — produced by the WS handler itself, not the JSONL. */

export interface SessionStatusEvent {
  type: "session_status";
  status: string; // Session.status.value (running, ended, ...)
  external_session_id: string;
  /** Deprecated alias for external_session_id — kept for one release. */
  claude_session_id?: string;
  jsonl_path: string;
}

export interface HistoryEvent {
  type: "history";
  events: TranscriptEvent[]; // the TAIL of the JSONL history (most recent)
  /** Index of the first event in `events` within the server's full history. */
  offset?: number;
  /** Total event count on the server (events.length when not truncated). */
  total?: number;
  /** True when older events exist before `offset` (load via load_history). */
  truncated?: boolean;
  /**
   * Every human-typed message in the WHOLE transcript, not just the shipped
   * tail — the jump rail is built from this. Without it the rail could only
   * index the loaded window, which on a busy session is the last handful of
   * turns while the topmost dot still looked like the start of the session.
   */
  nodes?: SessionNode[];
}

/** One jump-rail entry. `i` indexes the same array `HistoryEvent.offset` does. */
export interface SessionNode {
  i: number;
  text: string;
  ts?: string;
}

/** An older page of history, prepended in front of what's already shown. */
export interface HistoryPageEvent {
  type: "history_page";
  events: TranscriptEvent[];
  /** Index of the first event in this page within the full history. */
  offset: number;
}

export interface ErrorEvent {
  type: "error";
  detail: string;
}

/** Per-line events derived from the JSONL by message_router.route_record. */

export interface UserMessageEvent {
  type: "user_message";
  ts: string;
  text: string;
  /** Claude files machine-injected text under role "user" — a skill preamble,
   *  the post-compaction recap, the auto-continue nudge, an SDK- or
   *  cron-driven prompt, the Esc-interrupt marker. The user's role, not the
   *  user's words. Set by message_router from `isMeta` / `isCompactSummary`,
   *  from the `origin.kind` / `promptSource` provenance fields, and from
   *  `interruptedMessageId`. Anything indexing "what I said" — the jump rail,
   *  the composer's resend candidate — must skip these. */
  injected?: boolean;
}

export interface AssistantTextEvent {
  type: "assistant_text";
  ts: string;
  text: string;
}

export interface ToolUseStartEvent {
  type: "tool_use_start";
  ts: string;
  tool: string;
  tool_id: string;
  input: unknown;
}

export interface ToolUseResultEvent {
  type: "tool_use_result";
  ts: string;
  tool_id: string;
  ok: boolean;
  preview: string;
}

export interface SystemNoteEvent {
  type: "system_note";
  ts: string;
  text: string;
  /**
   * Set only when the event being reported did NOT succeed (a subagent that
   * was killed, a background command that failed). Absent is the routine case
   * and must stay visually quiet — 96% of these notes are routine, so painting
   * them all in the warning colour is what makes the rare real one invisible.
   */
  level?: "warning";
}

/** Fallback text emitted in edge cases by message_router. */
export interface TextEvent {
  type: "text";
  text: string;
}

export type TranscriptEvent =
  | UserMessageEvent
  | AssistantTextEvent
  | ToolUseStartEvent
  | ToolUseResultEvent
  | SystemNoteEvent
  | TextEvent;

export type WSEvent =
  | SessionStatusEvent
  | HistoryEvent
  | HistoryPageEvent
  | ErrorEvent
  | TranscriptEvent;

/** Type guards — cheap runtime narrowing. */
export const isTranscript = (e: WSEvent): e is TranscriptEvent =>
  e.type === "user_message" ||
  e.type === "assistant_text" ||
  e.type === "tool_use_start" ||
  e.type === "tool_use_result" ||
  e.type === "system_note" ||
  e.type === "text";

export const isSessionStatus = (e: WSEvent): e is SessionStatusEvent =>
  e.type === "session_status";

export const isHistory = (e: WSEvent): e is HistoryEvent =>
  e.type === "history";

export const isHistoryPage = (e: WSEvent): e is HistoryPageEvent =>
  e.type === "history_page";

export const isError = (e: WSEvent): e is ErrorEvent => e.type === "error";
