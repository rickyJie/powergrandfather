// Mobile-side sessions API client. Types are mobile-owned and mirror the
// BACKEND contract (backend/csm/api/sessions.py) — NOT the desktop frontend.
// The two UIs share the backend API, not TypeScript source. Endpoint calls
// use the mobile `http` instance (adds 25s timeout for DELETE — server blocks
// up to 15s).

import { http, httpLongDelete } from "./client";

/** A per-message idempotency key. Prefers crypto.randomUUID (available in
 *  modern browsers / Android WebView); falls back to a time+random string. */
function newMsgId(): string {
  try {
    if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
      return crypto.randomUUID();
    }
  } catch {
    /* fall through */
  }
  return `m-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Mirrors backend Session serialization. Fields mobile never reads are kept
 *  optional so an evolving backend response still parses. */
export interface SessionRow {
  id: string;
  title: string | null;
  type: string;
  cwd: string;
  status: string;
  pid: number | null;
  started_at: string | null;
  ended_at: string | null;
  exit_code: number | null;
  external_session_id: string | null;
  claude_session_id: string | null;
  last_activity_ts: string | null;
  unread_count: number;
  current_tool: string | null;
  /** Last assistant line — used as the drawer row preview. */
  last_assistant_msg?: string | null;
  session_project_id: string | null;
  // Which CLI adapter this session runs on (multi-agent v2). `backend` is a
  // deprecated alias kept for older responses.
  agent?: string;
  backend?: string;
  /** Claude-only: is the JSONL transcript still on disk? The backend
   *  hardcodes `false` for every other agent, so never test it without first
   *  checking `agent` — see `canResume` in ChatView. */
  jsonl_present?: boolean;
  /** Codex-only: path to this session's `rollout-*.jsonl`, bound post-spawn.
   *  The codex counterpart to `jsonl_present` for resumability. */
  rollout_path?: string | null;
  tags?: string[];
  archived_at?: string | null;
  /** When this session was resumed into a newer one, the id of that successor.
   *  Reads still work (shared JSONL) but writes must target the live head, so
   *  the UI follows this chain. Null on the live head. */
  superseded_by?: string | null;
}

export interface CreateSessionPayload {
  cwd: string;
  type?: string;
  title?: string;
  initial_prompt?: string;
  argv?: string[];
  session_project_id?: string | null;
  /** Explicit agent override; omit to use the user default. */
  agent?: string;
}

export interface SessionListResult {
  count: number;
  page_count: number;
  offset: number;
  has_more: boolean;
  items: SessionRow[];
}

export interface ListParams {
  status?: string;
  type?: string;
  limit?: number;
  offset?: number;
  agent?: string;
}

export const sessionsApi = {
  list: async (params: ListParams = {}) =>
    (await http.get("/api/sessions", { params })).data as SessionListResult,

  get: async (sid: string) =>
    (await http.get(`/api/sessions/${sid}`)).data as SessionRow,

  create: async (body: CreateSessionPayload) =>
    (await http.post("/api/sessions", body)).data as SessionRow,

  patch: async (sid: string, body: Partial<Pick<SessionRow, "title" | "session_project_id">>) =>
    (await http.patch(`/api/sessions/${sid}`, body)).data as SessionRow,

  /** Stop session — server blocks up to 15s (SIGINT → SIGTERM → SIGKILL). */
  stop: async (sid: string) => (await httpLongDelete(`/api/sessions/${sid}`)).data,

  /** Force-kill immediately. */
  kill: async (sid: string) =>
    (await http.post(`/api/sessions/${sid}/kill`)).data,

  /** Resume an ended session in a fresh PTY (`claude --resume <uuid>`). Returns
   *  the new session row. Only valid when external_session_id + jsonl present. */
  resume: async (sid: string) =>
    (await http.post(`/api/sessions/${sid}/resume`)).data as SessionRow,

  /** Write a line of user text to a live claude session's PTY stdin (chat).
   *  Carries a client-generated idempotency key so a tunnel-dropped response
   *  can be safely retried without double-typing into the PTY (the retry is
   *  enabled via the `__idempotent` flag consumed by the client interceptor). */
  sendMessage: async (sid: string, text: string) =>
    (
      await http.post(
        `/api/sessions/${sid}/message`,
        { text, client_msg_id: newMsgId() },
        { __idempotent: true } as Parameters<typeof http.post>[2] & {
          __idempotent: boolean;
        }
      )
    ).data as { sent: string; deduped?: boolean },

  /** Send a RAW key sequence to the PTY (no CRLF framing) — used by the
   *  interactive-choice panel to drive claude's in-terminal pickers
   *  (AskUserQuestion / plan approval): digits, ESC[B (down arrow), bare \r. */
  sendKeys: async (sid: string, text: string) =>
    (await http.post(`/api/sessions/${sid}/message`, { text, raw: true })).data,

  /** Archive / unarchive a session (hides it from the default list). */
  setArchived: async (sid: string, archived: boolean) =>
    (await http.patch(`/api/sessions/${sid}`, { archived })).data as SessionRow,

  /** Purge a session's persisted JSONL transcript + output (destructive). */
  purge: async (sid: string) =>
    (await http.post(`/api/sessions/${sid}/purge`)).data,

  /** Files this session touched (edit counts + tools + timestamps). */
  changes: async (sid: string) =>
    (await http.get(`/api/sessions/${sid}/changes`)).data as {
      sid: string;
      total_edits: number;
      files: {
        path: string;
        edit_count: number;
        tools: string[];
        first_ts: string | null;
        last_ts: string | null;
      }[];
    },

  /** Unified diff for one changed file. */
  changesDiff: async (sid: string, path: string) =>
    (
      await http.get(`/api/sessions/${sid}/changes/diff`, {
        params: { path },
        responseType: "text",
      })
    ).data as string,

  /**
   * Ring-buffer PTY tail (raw bytes, ANSI-laden) for read-only review.
   * Returned as text; callers strip ANSI before display.
   */
  output: async (sid: string) =>
    (
      await http.get(`/api/sessions/${sid}/output`, { responseType: "text" })
    ).data as string,
};

/** Strip ANSI/VT escape sequences so PTY output is readable without a terminal. */
export function stripAnsi(s: string): string {
  // eslint-disable-next-line no-control-regex
  return s.replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, "");
}

// `isTuiSession()` lived here to decide whether to show the degrade card.
// It was never called (the only gate was ChatView's own `isCodex`), and its
// premise — "codex is a TUI, therefore unrenderable" — was wrong twice over:
// interactive claude is a TUI too and chats fine, because what mobile renders
// is the transcript, not the terminal. Chattability is now decided by whether
// route_record() can parse the agent's transcript (ChatView.CHATTABLE_AGENTS,
// mirroring the backend's _CHATTABLE_AGENTS). Removed 2026-08-24.
