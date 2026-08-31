import { defineStore } from "pinia";
import { ref } from "vue";
import type { SessionNode, TranscriptEvent, WSEvent } from "@/api/ws-events";
import {
  isHistory,
  isHistoryPage,
  isSessionStatus,
  isError,
  isTranscript,
} from "@/api/ws-events";

// Pinia store holding transcript state per conversation id. Views mount
// the WS via `useAgentConversationSocket` and route events through
// `ingest(cid, ev)` here. Multiple views (e.g. detail + list preview) can
// share state without duplicating websockets.

// A locally-inserted user echo carries a frontend-only marker so we can
// reconcile it against the same message when it later comes back from the JSONL
// tail (which, over a cellular tunnel, is a full round-trip away).
type OptimisticUser = TranscriptEvent & { _optimistic: string };

// A pending echo is only tracked for reconciliation for a bounded window. After
// this, we stop trying to match it: the message WAS sent (POST returned 200 →
// PTY write succeeded), the optimistic bubble simply stays as a normal bubble,
// and — crucially — the pending entry is dropped so a later history replay can't
// keep re-appending it forever if the JSONL echo text isn't byte-identical to
// what was typed (backend/claude may trim/normalize). Bounds the leak the
// text-equality match would otherwise cause.
// Only governs how long a just-sent message is re-preserved across a RECONNECT
// (the live-duplicate reconciliation is now bubble-based, not time-boxed — see
// ingest). Generous so a slow echo on a busy session / laggy tunnel isn't
// dropped from the reconnect-preserve path.
const PENDING_EXPIRE_MS = 60_000;

function randomId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `opt-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  }
}

// Compare loosely: the JSONL echo may differ from the typed text by trailing
// whitespace / CRLF. Trim before matching to cut spurious duplicates.
const sameText = (a: string, b: string) => a.trim() === b.trim();

// Identity key for a transcript event, used to drop exact duplicates that a
// stray/orphaned second WebSocket can ingest for the same session (the root
// cause of "suddenly N copies of every message"). Tool events key by their
// unique tool_id; text events by (type, ts, text).
function dupKey(ev: TranscriptEvent): string {
  if (ev.type === "tool_use_start") return `tus:${ev.tool_id}`;
  if (ev.type === "tool_use_result") return `tur:${ev.tool_id}`;
  const ts = "ts" in ev ? ev.ts : "";
  return `${ev.type}:${ts}:${ev.text ?? ""}`;
}
// How many trailing events to scan for a duplicate. Orphan-socket dupes arrive
// within a few frames of the original, so a small window is enough and O(1).
const DEDUP_WINDOW = 40;
/** How many conversations keep their transcript in memory.
 *
 * `clear()` shipped with the store and was never called from anywhere except
 * its own test, so every conversation the user opened stayed resident for the
 * life of the page — on a phone, holding deeply-reactive tool payloads, that
 * grows without a ceiling. `setActive` now evicts past this many. */
const MAX_RESIDENT_CONVERSATIONS = 5;

export const useChatStore = defineStore("chat", () => {
  const transcripts = ref<Record<string, TranscriptEvent[]>>({});
  const status = ref<Record<string, string>>({});
  const lastError = ref<Record<string, string | null>>({});
  const unread = ref<Record<string, number>>({});
  const activeCid = ref<string | null>(null);
  // Pagination cursor per cid: `offset` = index of the earliest loaded event in
  // the server's full history; `total` = server-side event count. Lets the UI
  // know whether older history exists and what to request next.
  const historyMeta = ref<Record<string, { offset: number; total: number }>>({});
  // Jump-rail index per cid: every human-typed message in the FULL transcript,
  // including the part that hasn't been paged in. Served once with the history
  // frame and left alone afterwards — live messages append to it below.
  const sessionNodes = ref<Record<string, SessionNode[]>>({});
  // Pending optimistic echoes awaiting their JSONL round-trip, per cid.
  const pendingUser = ref<
    Record<string, { id: string; text: string; ts: number }[]>
  >({});

  function ensure(cid: string) {
    if (!transcripts.value[cid]) transcripts.value[cid] = [];
    if (unread.value[cid] === undefined) unread.value[cid] = 0;
  }

  /**
   * Jump-rail nodes for `cid`: the server's whole-transcript index, plus any
   * message that has landed since it was built.
   *
   * Derived rather than maintained, because the tail is exactly where the
   * bookkeeping would go wrong — a message you just sent must get its dot
   * immediately (optimistic echo), keep it when the JSONL echo replaces the
   * echo in place, and not sprout a second one. Recomputing from the array
   * makes all three the same code path.
   *
   * `i` is an index into the SERVER's event array, which the loaded array
   * mirrors from `offset` onward.
   */
  function railNodes(cid: string): SessionNode[] {
    const base = sessionNodes.value[cid] ?? [];
    const meta = historyMeta.value[cid];
    const arr = transcripts.value[cid] ?? [];
    if (!meta) return base;
    const maxKnown = base.length ? base[base.length - 1].i : meta.offset - 1;
    const extra: SessionNode[] = [];
    for (let p = 0; p < arr.length; p++) {
      const e = arr[p];
      if (e.type !== "user_message" || e.injected) continue;
      const i = meta.offset + p;
      if (i > maxKnown) extra.push({ i, text: (e.text ?? "").slice(0, 90), ts: e.ts });
    }
    return extra.length ? [...base, ...extra] : base;
  }

  /** Event count to measure rail positions against — grows with the live tail. */
  function railTotal(cid: string): number {
    const meta = historyMeta.value[cid];
    if (!meta) return (transcripts.value[cid] ?? []).length;
    return Math.max(meta.total, meta.offset + (transcripts.value[cid] ?? []).length);
  }

  /** Drop pending entries past their reconciliation window so text-mismatch can
   *  never accumulate an unbounded backlog that a history replay re-appends. */
  function prunePending(cid: string) {
    const pend = pendingUser.value[cid];
    if (!pend || !pend.length) return;
    const cutoff = Date.now() - PENDING_EXPIRE_MS;
    const kept = pend.filter((p) => p.ts >= cutoff);
    if (kept.length !== pend.length) pendingUser.value[cid] = kept;
  }

  /** Insert the user's message immediately so send feels instant instead of
   *  waiting a full tunnel round-trip for the JSONL to echo it back. Returns
   *  the marker id; call removeOptimisticUser(id) if the send then fails. */
  function addOptimisticUser(cid: string, text: string): string {
    ensure(cid);
    const id = randomId();
    const ev = {
      type: "user_message",
      ts: new Date().toISOString(),
      text,
      _optimistic: id,
    } as OptimisticUser;
    transcripts.value[cid].push(ev); // see the note on the append in ingest()
    (pendingUser.value[cid] ??= []).push({ id, text, ts: Date.now() });
    return id;
  }

  function removeOptimisticUser(cid: string, id: string) {
    const pend = pendingUser.value[cid];
    if (pend) pendingUser.value[cid] = pend.filter((p) => p.id !== id);
    transcripts.value[cid] = (transcripts.value[cid] ?? []).filter(
      (e) => (e as OptimisticUser)._optimistic !== id
    );
  }

  function ingest(cid: string, ev: WSEvent) {
    ensure(cid);
    if (isSessionStatus(ev)) {
      status.value[cid] = ev.status;
      return;
    }
    if (isHistory(ev)) {
      // Full replay replaces the transcript. Preserve any optimistic echo that
      // the JSONL hasn't caught up to yet, so a mid-send reconnect doesn't make
      // the user's just-sent message vanish. Prune expired first so a stale
      // pending entry (text the JSONL never echoed byte-identical) can't be
      // re-appended on every reconnect forever.
      prunePending(cid);
      const events = [...ev.events];
      const pend = pendingUser.value[cid] ?? [];
      const stillPending = pend.filter(
        (p) =>
          !events.some((e) => e.type === "user_message" && sameText(e.text, p.text))
      );
      for (const p of stillPending) {
        events.push({
          type: "user_message",
          ts: new Date().toISOString(),
          text: p.text,
          _optimistic: p.id,
        } as OptimisticUser);
      }
      pendingUser.value[cid] = stillPending;
      transcripts.value[cid] = events;
      historyMeta.value[cid] = {
        offset: ev.offset ?? 0,
        total: ev.total ?? ev.events.length,
      };
      // The rail index covers the whole transcript, so a replay REPLACES it
      // rather than merging — anything we appended locally is already in the
      // server's copy by now.
      if (ev.nodes) sessionNodes.value[cid] = [...ev.nodes];
      return;
    }
    if (isHistoryPage(ev)) {
      // Older page fetched on scroll-to-top — prepend in front of what's shown.
      transcripts.value[cid] = [...ev.events, ...(transcripts.value[cid] ?? [])];
      const meta = historyMeta.value[cid];
      historyMeta.value[cid] = {
        offset: ev.offset,
        total: meta?.total ?? transcripts.value[cid].length,
      };
      return;
    }
    if (isError(ev)) {
      lastError.value[cid] = ev.detail;
      return;
    }
    if (isTranscript(ev)) {
      // Reconcile the real user_message with its optimistic echo. Match against
      // the on-screen optimistic BUBBLE (not a 15s pending window): a slow JSONL
      // echo — busy session or laggy tunnel — used to arrive after the pending
      // entry had been pruned, so it appended a SECOND copy ("I sent hi, web
      // showed hi, then hi appeared again on mobile"). Promote the first still-
      // optimistic bubble with the same text in place and drop the echo. No
      // optimistic match → we didn't insert it (e.g. it was sent from web) →
      // fall through and append it.
      if (ev.type === "user_message") {
        prunePending(cid); // hygiene only; dedup no longer depends on it
        const arr = transcripts.value[cid];
        const oi = arr.findIndex(
          (e) =>
            e.type === "user_message" &&
            (e as OptimisticUser)._optimistic &&
            sameText(e.text, ev.text)
        );
        if (oi >= 0) {
          transcripts.value[cid] = [
            ...arr.slice(0, oi),
            { type: "user_message", ts: ev.ts, text: ev.text },
            ...arr.slice(oi + 1),
          ];
          const pend = pendingUser.value[cid];
          const pi = pend ? pend.findIndex((p) => sameText(p.text, ev.text)) : -1;
          if (pend && pi >= 0) pend.splice(pi, 1);
          return;
        }
      }
      // Idempotency guard: a duplicated stream (orphan socket) must not double
      // every message. Skip if this exact event already sits in the recent tail.
      const key = dupKey(ev);
      const recent = transcripts.value[cid];
      const from = Math.max(0, recent.length - DEDUP_WINDOW);
      for (let i = recent.length - 1; i >= from; i--) {
        if (dupKey(recent[i]) === key) return;
      }
      // push, not `[...recent, ev]`: the spread reallocated and re-proxied the
      // whole transcript on EVERY inbound event, so a long session's ingest
      // cost grew with its own length. `transcripts` is a deep `ref`, so a
      // push is reactive just the same.
      recent.push(ev);
      // Unread bump when this conversation is not currently viewed. Neither my
      // own messages nor system notes count: a system_note is a background
      // event (a subagent finishing, a turn aborting, a codex session opening),
      // not something the agent said to me. Without this, five subagents
      // finishing in parallel put five unread on a session I never opened —
      // and since a `<task-notification>` is filed under role "user" upstream,
      // this only started biting once the router stopped mis-attributing them.
      if (
        activeCid.value !== cid &&
        ev.type !== "user_message" &&
        ev.type !== "system_note"
      ) {
        unread.value[cid] = (unread.value[cid] ?? 0) + 1;
      }
    }
  }

  function markRead(cid: string) {
    unread.value[cid] = 0;
  }

  /** Conversations whose transcript stays resident, most recent first. Plain
   *  array on purpose — internal bookkeeping, nothing renders it. */
  const residentCids: string[] = [];

  function setActive(cid: string | null) {
    activeCid.value = cid;
    if (!cid) return;
    markRead(cid);
    const at = residentCids.indexOf(cid);
    if (at >= 0) residentCids.splice(at, 1);
    residentCids.unshift(cid);
    for (const stale of residentCids.splice(MAX_RESIDENT_CONVERSATIONS)) {
      if (stale !== cid) evictTranscript(stale);
    }
  }

  /** Release the memory-heavy half of a conversation while keeping the small
   *  signals the UI still shows for it — the unread badge above all, which
   *  `clear()` would also drop and which must survive an eviction.
   *
   *  Safe because the server replays the entire transcript on connect
   *  (`agents.py` `on_replay`), so reopening an evicted conversation
   *  repopulates it; the cost is one replay frame, not lost data. */
  function evictTranscript(cid: string) {
    delete transcripts.value[cid];
    delete pendingUser.value[cid];
    delete historyMeta.value[cid];
  }

  function clear(cid: string) {
    delete transcripts.value[cid];
    delete status.value[cid];
    delete lastError.value[cid];
    delete unread.value[cid];
    delete pendingUser.value[cid];
    delete historyMeta.value[cid];
  }

  /** Whether older history exists before what's currently loaded. */
  function canLoadOlder(cid: string): boolean {
    return (historyMeta.value[cid]?.offset ?? 0) > 0;
  }

  return {
    transcripts,
    status,
    lastError,
    unread,
    activeCid,
    historyMeta,
    sessionNodes,
    railNodes,
    railTotal,
    canLoadOlder,
    ingest,
    addOptimisticUser,
    removeOptimisticUser,
    markRead,
    setActive,
    clear,
  };
});
