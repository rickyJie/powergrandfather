import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { setActivePinia, createPinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import Vant from "vant";

import SessionDrawer from "../../../../frontend/src/components/SessionDrawer.vue";
import { useSessionsStore } from "../../../../frontend/src/stores/sessions";
import { useNotificationsStore } from "../../../../frontend/src/stores/notifications";
import { useChatStore } from "../../../../frontend/src/stores/chat";
import { sessionsApi, type SessionRow } from "../../../../frontend/src/api/sessions";

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div/>" } }],
});

function row(over: Partial<SessionRow> = {}): SessionRow {
  return {
    id: "s1",
    type: "interactive",
    status: "running",
    cwd: "/repo",
    title: "work",
    agent: "claude",
    unread_count: 0,
    last_activity_ts: "2026-08-26T00:00:00",
    last_assistant_msg: null,
    current_tool: null,
    archived_at: null,
    superseded_by: null,
    ...over,
  } as SessionRow;
}

// The drawer refreshes on mount, so the rows have to come from the API mock —
// seeding the store directly would just be overwritten.
async function drawer(rows: SessionRow[] = []) {
  vi.spyOn(sessionsApi, "list").mockResolvedValue({
    count: rows.length,
    items: rows,
    has_more: false,
  } as never);
  const w = mount(SessionDrawer, {
    global: { plugins: [Vant, router] },
  });
  await flush();
  return w;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("SessionDrawer", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("has no bell", async () => {
    const w = await drawer([row()]);

    // A global unread counter says "something, somewhere" and then makes you
    // hunt for it. Removed deliberately — assert it stays removed.
    expect(w.find('[aria-label="Notifications"]').exists()).toBe(false);
    expect(w.html()).not.toContain("van-icon-bell");
  });

  it("shows new messages on the session they arrived in", async () => {
    useNotificationsStore().items = [
      {
        id: "n1",
        type: "new_message",
        session_id: "s1",
        title: "1 new message",
        body: "done",
        ts: "2026-08-26T00:00:00",
        read: false,
        dismissed: false,
      },
    ];
    const w = await drawer([row()]);

    expect(w.find(".badge").text()).toBe("1");
  });

  it("does not let an opened-then-left chat mask later messages", async () => {
    // chat.unread[sid] is zeroed on open and never deleted. Under the old
    // `chat.unread[id] ?? row.unread_count` it therefore masked the row
    // forever: every message that arrived after you left that session was
    // invisible. The badge takes the max of its sources for this reason.
    useChatStore().setActive("s1");
    useChatStore().setActive(null);

    const w = await drawer([row({ unread_count: 3 })]);

    expect(w.find(".badge").text()).toBe("3");
  });
});
