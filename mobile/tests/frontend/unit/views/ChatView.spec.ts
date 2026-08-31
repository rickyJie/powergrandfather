import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import Vant from "vant";

import ChatView from "../../../../frontend/src/views/ChatView.vue";
import { sessionsApi, type SessionRow } from "../../../../frontend/src/api/sessions";
import { useSessionsStore } from "../../../../frontend/src/stores/sessions";
import { useChatStore } from "../../../../frontend/src/stores/chat";
import { useUiStore } from "../../../../frontend/src/stores/ui";
import { notificationsApi } from "../../../../frontend/src/api/notifications";

// Capture the socket's callbacks so a test can deliver (or withhold) the
// transcript the way a real tunnel does — late.
let socketOpts: Record<string, (arg?: unknown) => void> = {};
const socketClose = vi.fn();
vi.mock("../../../../frontend/src/api/ws", () => ({
  useSessionMessageSocket: (_sid: string, opts: Record<string, () => void>) => {
    socketOpts = opts;
    return { close: socketClose, send: vi.fn() };
  },
}));

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div/>" } }],
});

function row(over: Partial<SessionRow> = {}): SessionRow {
  return {
    id: "s1",
    title: "the one I tapped",
    type: "interactive",
    cwd: "/repo",
    status: "running",
    pid: 1,
    started_at: null,
    ended_at: null,
    exit_code: null,
    external_session_id: null,
    claude_session_id: null,
    last_activity_ts: null,
    unread_count: 0,
    current_tool: null,
    session_project_id: null,
    agent: "claude",
    superseded_by: null,
    ...over,
  } as SessionRow;
}

async function chatView() {
  const w = mount(ChatView, { global: { plugins: [Vant, router] } });
  await flushPromises();
  return w;
}

const title = (w: { find: (s: string) => { text: () => string } }) =>
  w.find(".title-text").text();

beforeEach(() => {
  setActivePinia(createPinia());
  socketOpts = {};
  socketClose.mockClear();
  // Opening a chat fires a read receipt; let it resolve instead of letting
  // jsdom attempt a real XHR and spray AggregateErrors over the output.
  vi.spyOn(notificationsApi, "markSessionRead").mockResolvedValue(undefined as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ChatView — switching sessions", () => {
  it("shows the tapped session's title before the meta GET answers", async () => {
    // The drawer's list already carries title/cwd/agent. Waiting a full
    // /api/sessions/{sid} round trip to use them is what made the header look
    // like it updated slowly — and for that whole round trip it was showing
    // the session the user had just LEFT.
    useSessionsStore().items = [row()];
    let resolveGet: (r: SessionRow) => void = () => {};
    vi.spyOn(sessionsApi, "get").mockReturnValue(
      new Promise<SessionRow>((res) => { resolveGet = res; }) as never,
    );
    useUiStore().setActive("s1");

    const w = await chatView();

    expect(title(w)).toBe("the one I tapped");
    expect(sessionsApi.get).toHaveBeenCalled(); // still in flight

    resolveGet(row({ title: "renamed on the server" }));
    await flushPromises();
    expect(title(w)).toBe("renamed on the server");
  });

  it("never leaves the previous session's title up when the new one is uncached", async () => {
    // Cold deep link: nothing to seed from. The sid is a worse title but it is
    // at least about the right session; the old one is just wrong.
    const store = useSessionsStore();
    store.items = [row()];
    vi.spyOn(sessionsApi, "get").mockResolvedValue(row() as never);
    useUiStore().setActive("s1");
    const w = await chatView();
    expect(title(w)).toBe("the one I tapped");

    store.items = []; // uncached target
    vi.spyOn(sessionsApi, "get").mockReturnValue(new Promise<SessionRow>(() => {}) as never);
    useUiStore().setActive("s2-uncached");
    await flushPromises();

    expect(title(w)).not.toBe("the one I tapped");
    expect(title(w)).toBe("s2-uncac"); // sid.slice(0, 8)
  });

  it("opens the transcript socket off the cached row, not after the GET", async () => {
    useSessionsStore().items = [row()];
    vi.spyOn(sessionsApi, "get").mockReturnValue(new Promise<SessionRow>(() => {}) as never);
    useUiStore().setActive("s1");

    await chatView();

    // The GET is still pending; the socket must already be up, because that
    // round trip was pure dead time before the first message could arrive.
    expect(socketOpts.onEvent).toBeTypeOf("function");
  });

  it('does not claim "No messages yet" while the transcript is still coming', async () => {
    useSessionsStore().items = [row()];
    vi.spyOn(sessionsApi, "get").mockResolvedValue(row() as never);
    useUiStore().setActive("s1");

    const w = await chatView();

    // Meta resolved, socket open, history not delivered yet.
    expect(w.find(".empty-hint").exists()).toBe(false);
    expect(w.find(".loading-hint").exists()).toBe(true);

    socketOpts.onEvent?.({ type: "history", events: [] } as never);
    await flushPromises();

    expect(w.find(".loading-hint").exists()).toBe(false);
  });

  it("shows a cached transcript immediately, with no spinner over it", async () => {
    // Switching back and forth is most of what switching feels like.
    const chat = useChatStore();
    chat.ingest("s1", {
      type: "history",
      events: [{ type: "user_message", ts: "2026-08-26T00:00:00", text: "hi" }],
    } as never);
    useSessionsStore().items = [row()];
    vi.spyOn(sessionsApi, "get").mockResolvedValue(row() as never);
    useUiStore().setActive("s1");

    const w = await chatView();

    expect(w.find(".loading-hint").exists()).toBe(false);
    expect(w.text()).toContain("hi");
  });

  it("keeps a session that is only unreachable, and drops one that is really gone", async () => {
    useSessionsStore().items = [row()];
    const store = useUiStore();

    // Transport blip: the row we can already render must survive it.
    vi.spyOn(sessionsApi, "get").mockRejectedValue(
      Object.assign(new Error("timeout"), { code: "ECONNABORTED" }),
    );
    store.setActive("s1");
    const w = await chatView();
    expect(title(w)).toBe("the one I tapped");

    // 404 is the one answer that means gone.
    vi.spyOn(sessionsApi, "get").mockRejectedValue(
      Object.assign(new Error("not found"), { response: { status: 404 } }),
    );
    store.setActive("s1-gone");
    await flushPromises();
    expect(title(w)).not.toBe("the one I tapped");
  });

  it("does not carry the previous session's banners into the new one", async () => {
    useSessionsStore().items = [row(), row({ id: "s2", title: "second" })];
    vi.spyOn(sessionsApi, "get").mockImplementation(
      (async (sid: string) => row({ id: sid })) as never,
    );
    useUiStore().setActive("s1");
    const w = await chatView();

    socketOpts.onEvent?.({ type: "session_status", status: "empty" } as never);
    await flushPromises();
    expect(w.text()).toContain("No messages yet — send one to start.");

    useUiStore().setActive("s2");
    await flushPromises();

    // s2 is a different session; nothing has said it is empty.
    expect(w.text()).not.toContain("No messages yet — send one to start.");
  });
});
