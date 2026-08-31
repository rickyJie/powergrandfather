import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import MockAdapter from "axios-mock-adapter";
import { useNotificationsStore } from "../../../../frontend/src/stores/notifications";
import { useChatStore } from "../../../../frontend/src/stores/chat";
import { normalizeNotification } from "../../../../frontend/src/api/notifications";
import { http } from "../../../../frontend/src/api/client";

// Backend wire shape: read_at / dismissed_at / created_at (nullable ISO),
// NOT read / dismissed / ts booleans. These tests pin the REAL contract.
function raw(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    type: "mission",
    session_id: null,
    title: "t",
    body: "b",
    created_at: new Date().toISOString(),
    read_at: null,
    dismissed_at: null,
    metadata: {},
    ...over,
  };
}

describe("notifications store", () => {
  let mock: MockAdapter;

  beforeEach(() => {
    setActivePinia(createPinia());
    mock = new MockAdapter(http);
  });

  it("normalizeNotification maps the backend wire shape", () => {
    const n = normalizeNotification(
      raw("n1", { read_at: "2026-01-01T00:00:00", created_at: "2026-01-02T00:00:00" })
    );
    expect(n.read).toBe(true);
    expect(n.dismissed).toBe(false);
    expect(n.ts).toBe("2026-01-02T00:00:00");
  });

  it("refresh normalizes items; unreadCount derives from items", async () => {
    mock.onGet("/api/notifications").reply(200, {
      count: 2,
      items: [raw("n1"), raw("n2", { read_at: new Date().toISOString() })],
    });

    const store = useNotificationsStore();
    await store.refresh();
    expect(store.items).toHaveLength(2);
    expect(store.items[0].read).toBe(false);
    expect(store.items[1].read).toBe(true);
    expect(store.unreadCount).toBe(1);
    expect(store.unread).toHaveLength(1);
    expect(store.readOnly).toHaveLength(1);
  });

  it("markRead flips flag and decrements unread count", async () => {
    mock.onGet("/api/notifications").reply(200, { count: 1, items: [raw("n1")] });
    mock.onPost("/api/notifications/n1/read").reply(200, {});

    const store = useNotificationsStore();
    await store.refresh();
    expect(store.unreadCount).toBe(1);
    await store.markRead("n1");
    expect(store.unreadCount).toBe(0);
    expect(store.items[0].read).toBe(true);
  });

  it("dismiss removes item and decrements unread", async () => {
    mock.onGet("/api/notifications").reply(200, { count: 1, items: [raw("n1")] });
    mock.onPost("/api/notifications/n1/dismiss").reply(200, {});

    const store = useNotificationsStore();
    await store.refresh();
    await store.dismiss("n1");
    expect(store.items).toHaveLength(0);
    expect(store.unreadCount).toBe(0);
  });

  it("markRead rolls back on server error", async () => {
    mock.onGet("/api/notifications").reply(200, { count: 1, items: [raw("n1")] });
    mock.onPost("/api/notifications/n1/read").reply(500);

    const store = useNotificationsStore();
    await store.refresh();
    await store.markRead("n1");
    expect(store.items[0].read).toBe(false);
    expect(store.unreadCount).toBe(1);
  });
});

// The bell is gone; `unreadBySession` is what replaced it. It has to carry the
// same new_message signal, per session, or removing the bell would leave the
// phone with no new-message notification at all.
describe("notifications store — the per-session badge that replaced the bell", () => {
  let mock: MockAdapter;
  let visibility = "visible";

  const msg = (id: string, sid: string, over: Record<string, unknown> = {}) =>
    raw(id, { type: "new_message", session_id: sid, ...over });

  beforeEach(() => {
    setActivePinia(createPinia());
    mock = new MockAdapter(http);
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility,
    });
  });

  afterEach(() => {
    mock.restore();
  });

  it("groups unread new_message rows by session", async () => {
    mock.onGet("/api/notifications").reply(200, {
      count: 5,
      items: [
        msg("a1", "s1"),
        msg("a2", "s1"),
        msg("b1", "s2"),
        // Read, dismissed, and non-message rows must not reach a session badge.
        msg("a3", "s1", { read_at: new Date().toISOString() }),
        msg("b2", "s2", { dismissed_at: new Date().toISOString() }),
        raw("m1", { type: "mission", session_id: "s1" }),
      ],
    });

    const store = useNotificationsStore();
    await store.refresh();

    expect(store.unreadBySession).toEqual({ s1: 2, s2: 1 });
  });

  it("markSessionRead clears that session only, and tells the server", async () => {
    mock.onGet("/api/notifications").reply(200, {
      count: 2,
      items: [msg("a1", "s1"), msg("b1", "s2")],
    });
    const post = vi.fn().mockReturnValue([200, {}]);
    mock.onPost("/api/notifications/mark-session-read/s1").reply(post);

    const store = useNotificationsStore();
    await store.refresh();
    await store.markSessionRead("s1");

    expect(store.unreadBySession).toEqual({ s2: 1 });
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("survives the 404 the server returns for a purged session", async () => {
    mock.onGet("/api/notifications").reply(200, { count: 1, items: [msg("a1", "s1")] });
    mock.onPost("/api/notifications/mark-session-read/s1").reply(404);

    const store = useNotificationsStore();
    await store.refresh();
    await expect(store.markSessionRead("s1")).resolves.toBeUndefined();
    // Local clear stands: a session the server has dropped has nothing to show.
    expect(store.unreadBySession).toEqual({});
  });

  it("badges a push for a session you are not looking at", () => {
    const store = useNotificationsStore();
    useChatStore().setActive("s1");

    store.ingestPush(msg("b1", "s2"));

    expect(store.unreadBySession).toEqual({ s2: 1 });
  });

  it("never marks anything read from an incoming push", async () => {
    // The Android app raises its tray notification from whatever is still
    // unread (`/api/notifications?only_unread=true`, polled every 20s). A push
    // handler that marks rows read races that poller and silently eats the
    // alert on a backgrounded phone — which is the whole point of the phone.
    // Read receipts come from user gestures only (ChatView.sendReadReceipt).
    const post = vi.fn().mockReturnValue([200, {}]);
    mock.onPost(/mark-session-read/).reply(post);
    const store = useNotificationsStore();
    useChatStore().setActive("s1");
    visibility = "visible"; // even with the chat demonstrably on screen

    store.ingestPush(msg("a1", "s1"));
    await flush();

    expect(post).not.toHaveBeenCalled();
    expect(store.unreadBySession).toEqual({ s1: 1 });
  });
});

const flush = () => new Promise((r) => setTimeout(r, 0));
