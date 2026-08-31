import { describe, it, expect, beforeEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { useChatStore } from "../../../../frontend/src/stores/chat";
import type { WSEvent } from "../../../../frontend/src/api/ws-events";

const CID = "sess-1";
// `ts` is overridable because the store's idempotency guard keys on
// `type:ts:text` (see `dupKey`). Two events built with the SAME ts are byte
// identical, which is exactly what an orphan socket replaying a frame looks
// like — collapsing them is the guard working, not a bug. A genuine re-send
// happens at a different moment, so it must be written that way.
const userMsg = (text: string, ts = "2026-08-19T00:00:00Z"): WSEvent => ({
  type: "user_message",
  ts,
  text,
});
const asstMsg = (text: string): WSEvent => ({
  type: "assistant_text",
  ts: "2026-08-19T00:00:01Z",
  text,
});

function texts(store: ReturnType<typeof useChatStore>, cid = CID): string[] {
  return (store.transcripts[cid] ?? []).map(
    (e) => (e as { text?: string }).text ?? ""
  );
}

describe("chat store — optimistic echo", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("inserts the user's message immediately", () => {
    const s = useChatStore();
    s.addOptimisticUser(CID, "hello");
    expect(texts(s)).toEqual(["hello"]);
  });

  it("reconciles the real JSONL echo without duplicating the bubble", () => {
    const s = useChatStore();
    s.addOptimisticUser(CID, "hello");
    s.ingest(CID, userMsg("hello")); // real echo of the same text
    expect(texts(s)).toEqual(["hello"]); // deduped, not doubled
    // A SECOND real user_message of the same text is genuine (pending drained).
    // Later ts — see the helper: same-ts would be a replay, not a re-send.
    s.ingest(CID, userMsg("hello", "2026-08-19T00:00:05Z"));
    expect(texts(s)).toEqual(["hello", "hello"]);
  });

  it("handles two identical-text sends in flight", () => {
    const s = useChatStore();
    s.addOptimisticUser(CID, "dup");
    s.addOptimisticUser(CID, "dup");
    expect(texts(s)).toEqual(["dup", "dup"]);
    s.ingest(CID, userMsg("dup"));
    s.ingest(CID, userMsg("dup"));
    expect(texts(s)).toEqual(["dup", "dup"]); // both reconciled, none added
  });

  it("rolls back the optimistic bubble when the send fails", () => {
    const s = useChatStore();
    const id = s.addOptimisticUser(CID, "oops");
    s.removeOptimisticUser(CID, id);
    expect(texts(s)).toEqual([]);
    // pending was cleared too: a later real echo appends normally, not skipped
    s.ingest(CID, userMsg("oops"));
    expect(texts(s)).toEqual(["oops"]);
  });

  it("preserves a still-pending optimistic across a history replay", () => {
    const s = useChatStore();
    s.addOptimisticUser(CID, "pending");
    const history: WSEvent = {
      type: "history",
      events: [userMsg("earlier") as never],
    };
    s.ingest(CID, history);
    expect(texts(s)).toEqual(["earlier", "pending"]); // pending survived
  });

  it("drops the optimistic once the history replay contains it", () => {
    const s = useChatStore();
    s.addOptimisticUser(CID, "settled");
    const history: WSEvent = {
      type: "history",
      events: [userMsg("settled") as never],
    };
    s.ingest(CID, history);
    expect(texts(s)).toEqual(["settled"]); // exactly one, no dupe
    // pending drained: a subsequent real send of the same text now appends
    s.ingest(CID, userMsg("settled", "2026-08-19T00:00:05Z"));
    expect(texts(s)).toEqual(["settled", "settled"]);
  });

  it("clear() wipes pending so it can't leak into a reused cid", () => {
    const s = useChatStore();
    s.addOptimisticUser(CID, "gone");
    s.clear(CID);
    expect(texts(s)).toEqual([]);
    s.ingest(CID, userMsg("gone")); // no stale pending → appends normally
    expect(texts(s)).toEqual(["gone"]);
  });

  it("evicts the least-recently-viewed transcripts past the resident cap", () => {
    // Without this the store kept every conversation the user ever opened for
    // the life of the page: `clear()` existed but had zero call sites outside
    // this file. On a phone that is unbounded growth over deeply-reactive tool
    // payloads.
    const s = useChatStore();
    for (let i = 0; i < 7; i++) {
      s.setActive(`c${i}`);
      s.ingest(`c${i}`, asstMsg(`msg ${i}`));
    }
    // 5 resident: c2..c6. c0 and c1 are gone.
    expect(texts(s, "c0")).toEqual([]);
    expect(texts(s, "c1")).toEqual([]);
    for (const i of [2, 3, 4, 5, 6]) {
      expect(texts(s, `c${i}`), `c${i} should still be resident`).toEqual([`msg ${i}`]);
    }
  });

  it("an eviction keeps the unread badge — only the transcript is released", () => {
    // `clear()` drops unread too, so reusing it for eviction would silently
    // wipe the one signal telling the user that conversation needs attention.
    const s = useChatStore();
    s.setActive("old");
    s.setActive(null); // not viewing it, so its output counts as unread
    s.ingest("old", asstMsg("you have mail"));
    expect(s.unread["old"]).toBe(1);
    for (let i = 0; i < 6; i++) s.setActive(`other-${i}`); // push "old" out
    expect(texts(s, "old")).toEqual([]); // transcript released
    expect(s.unread["old"]).toBe(1); // badge survived
  });

  it("bumps unread for assistant output but not for the user's own echo", () => {
    const s = useChatStore();
    s.setActive(null); // nothing is being viewed
    s.addOptimisticUser(CID, "hi");
    expect(s.unread[CID] ?? 0).toBe(0); // optimistic user msg: no bump
    s.ingest(CID, asstMsg("reply"));
    expect(s.unread[CID]).toBe(1); // assistant output: bump
  });

  it("a background task finishing does not raise a badge", () => {
    // A `<task-notification>` is filed under role "user" in the JSONL, so
    // while the router mis-attributed it as a user_message it was excluded
    // from unread by accident. Collapsing it to a system_note handed it a
    // badge: five subagents finishing in parallel put 5 unread on a session
    // that was never opened. A system note is a background event, not
    // something the agent said to me.
    const s = useChatStore();
    s.setActive(null);
    s.ingest(CID, {
      type: "system_note",
      ts: "2026-08-25T00:00:00Z",
      text: 'Agent "Brain 代码架构设计" finished',
    } as never);
    expect(s.unread[CID] ?? 0).toBe(0);

    // ...and real agent output still does.
    s.ingest(CID, asstMsg("reply"));
    expect(s.unread[CID]).toBe(1);
  });
});
