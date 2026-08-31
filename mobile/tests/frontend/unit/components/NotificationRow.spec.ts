import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Vant from "vant";
import NotificationRow from "../../../../frontend/src/components/notification/NotificationRow.vue";

function make(type: string, over = {}) {
  return {
    id: "n1",
    type,
    session_id: null,
    title: "hi",
    body: "some body text",
    ts: new Date().toISOString(),
    read: false,
    dismissed: false,
    ...over,
  };
}

function mountRow(item: object) {
  return mount(NotificationRow, {
    props: { item },
    global: { plugins: [Vant] },
  });
}

describe("NotificationRow", () => {
  it("maps a known type to a human label (not the raw enum)", () => {
    const w = mountRow(make("auto_needs_review"));
    expect(w.text()).toContain("Needs review");
    expect(w.text()).not.toContain("auto_needs_review");
  });

  it("falls back to the raw type for unknown kinds", () => {
    const w = mountRow(make("weird_new_type"));
    expect(w.text()).toContain("weird_new_type");
  });

  it("colours a crash as danger and a mission-done as success", () => {
    const crash = mountRow(make("session_crashed"));
    expect(crash.find(".van-tag--danger").exists()).toBe(true);
    const done = mountRow(make("mission_done"));
    expect(done.find(".van-tag--success").exists()).toBe(true);
  });

  it("shows the unread dot when not read", () => {
    const unread = mountRow(make("new_message", { read: false }));
    expect(unread.find(".dot").exists()).toBe(true);
    const read = mountRow(make("new_message", { read: true }));
    expect(read.find(".dot").exists()).toBe(false);
  });
});
