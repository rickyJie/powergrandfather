import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Vant from "vant";

import MessageStream from "../../../../frontend/src/components/message-stream/MessageStream.vue";
import type { TranscriptEvent } from "../../../../frontend/src/api/ws-events";
import { RAIL_PAGE, RAIL_WINDOW } from "../../../../frontend/src/lib/rail";

describe("MessageStream", () => {
  it("renders user and assistant bubbles", () => {
    const events: TranscriptEvent[] = [
      { type: "user_message", ts: "2026-08-14T00:00:00Z", text: "hi" },
      { type: "assistant_text", ts: "2026-08-14T00:00:01Z", text: "hello" },
    ];
    const wrapper = mount(MessageStream, {
      props: { events },
      global: { plugins: [Vant] },
    });
    const rows = wrapper.findAll(".msg-row");
    expect(rows).toHaveLength(2);
    // 1st is the user (compact right-aligned card)
    expect(rows[0].classes()).toContain("is-user");
    expect(rows[1].classes()).toContain("is-assistant");
  });

  it("renders a machine-injected role-user record as system, not as mine", () => {
    // A headless `claude -p` prompt (CSM's own token-alert helper, a cron
    // skill) is filed under role "user". Rendered in the sage right-aligned
    // card it read as something the user had just sent — 2026-08-30.
    const events: TranscriptEvent[] = [
      {
        type: "user_message",
        ts: "2026-08-14T00:00:00Z",
        text: "Rule that fired: …",
        injected: true,
      },
      { type: "user_message", ts: "2026-08-14T00:00:01Z", text: "hi" },
    ];
    const wrapper = mount(MessageStream, {
      props: { events },
      global: { plugins: [Vant] },
    });
    const rows = wrapper.findAll(".msg-row");
    expect(rows[0].classes()).toContain("is-system");
    expect(rows[0].classes()).not.toContain("is-user");
    // The message the user actually typed is untouched.
    expect(rows[1].classes()).toContain("is-user");
  });

  it("pairs tool_use_start with its tool_use_result", () => {
    const events: TranscriptEvent[] = [
      {
        type: "tool_use_start",
        ts: "2026-08-14T00:00:00Z",
        tool: "bash",
        tool_id: "t1",
        input: { command: "ls" },
      },
      {
        type: "tool_use_result",
        ts: "2026-08-14T00:00:01Z",
        tool_id: "t1",
        ok: true,
        preview: "file1\nfile2",
      },
    ];
    const wrapper = mount(MessageStream, {
      props: { events },
      global: { plugins: [Vant] },
    });
    // `.tool-card` is ToolUseBubble (mobile). `.tool-block` is the DESKTOP
    // ToolUseBlock — this spec was copied from there and kept the old
    // selector, so it silently asserted nothing for as long as it existed.
    const toolBlocks = wrapper.findAll(".tool-card");
    // Two events collapse into one paired tool block
    expect(toolBlocks).toHaveLength(1);
    // Tool name visible in header
    expect(toolBlocks[0].text()).toContain("bash");
  });

  it("shows empty hint when no events", () => {
    const wrapper = mount(MessageStream, {
      props: { events: [] },
      global: { plugins: [Vant] },
    });
    expect(wrapper.find(".empty-hint").exists()).toBe(true);
  });

  it("hides content and shows loading when loading prop true", () => {
    const wrapper = mount(MessageStream, {
      props: { events: [], loading: true },
      global: { plugins: [Vant] },
    });
    expect(wrapper.find(".loading-hint").exists()).toBe(true);
  });
});

describe("MessageStream jump rail", () => {
  const user = (text: string, injected?: boolean) => ({
    type: "user_message" as const,
    ts: "2026-08-25T00:00:00Z",
    text,
    ...(injected ? { injected: true } : {}),
  });
  const assistant = (text: string) => ({
    type: "assistant_text" as const,
    ts: "2026-08-25T00:00:01Z",
    text,
  });

  /** What the server puts in the history frame's `nodes`: every human-typed
   *  message across the WHOLE transcript, machine-injected ones already
   *  dropped. Mirrors `_user_message_index` in `api/sessions.py`. */
  const serverNodes = (events: any[]) =>
    events
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => e.type === "user_message" && !e.injected)
      .map(({ e, i }) => ({ i, text: e.text, ts: e.ts }));

  const mountRail = (events: unknown[], over: Record<string, unknown> = {}) =>
    mount(MessageStream, {
      props: {
        events: events as never,
        nodes: serverNodes(events as any[]) as never,
        offset: 0,
        total: (events as unknown[]).length,
        ...over,
      },
      global: { plugins: [Vant] },
    });

  const railDots = (events: unknown[]) => mountRail(events).findAll(".rail-node");

  it("indexes only what I typed", () => {
    // Rail needs >1 node to render at all.
    const dots = railDots([
      user("first question"),
      assistant("a long reply"),
      user("second question"),
      assistant("another reply"),
    ]);
    expect(dots).toHaveLength(2);
  });

  it("skips machine-injected role-user records", () => {
    // Claude files a skill preamble / compaction recap / auto-continue nudge
    // under role "user"; without the flag the rail points at text the user
    // never wrote.
    const dots = railDots([
      user("real question one"),
      user("Base directory for this skill: /x", true),
      user("This session is being continued…", true),
      user("real question two"),
    ]);
    expect(dots).toHaveLength(2);
  });

  it("skips slash commands", () => {
    const dots = railDots([
      user("real question one"),
      user("/compact"),
      user("real question two"),
    ]);
    expect(dots).toHaveLength(2);
  });

  it("still counts a path that merely starts with a slash", () => {
    const dots = railDots([
      user("/workspace/PowerGrandFather 看一下"),
      user("real question"),
    ]);
    expect(dots).toHaveLength(2);
  });

  it("gives a system note no node at all", () => {
    // A `<task-notification>` (a subagent reporting back) used to arrive as a
    // role-"user" record and put a dot on the rail; the router now collapses it
    // to a system note, so the rail skips it on role alone.
    const dots = railDots([
      user("real question one"),
      { type: "system_note", ts: "2026-08-25T00:00:02Z",
        text: 'Agent "審閱" finished' },
      user("real question two"),
    ]);
    expect(dots).toHaveLength(2);
  });

  // ── the whole session, not the loaded window ────────────────────────────
  it("indexes messages that have not been paged in yet", () => {
    // The regression: history ships only the last 400 events, so on a busy
    // session (~80 events/turn) the rail covered the last handful of turns
    // while its topmost dot still sat at the top as if it were the start.
    const loaded = [user("recent one"), assistant("reply"), user("recent two")];
    const w = mountRail(loaded, {
      // Three older messages the client has never seen.
      nodes: [
        { i: 5, text: "ancient one" },
        { i: 40, text: "ancient two" },
        { i: 90, text: "ancient three" },
        { i: 200, text: "recent one" },
        { i: 202, text: "recent two" },
      ],
      offset: 200,
      total: 203,
    });
    expect(w.findAll(".rail-node")).toHaveLength(5);
  });

  it("asks for older history when an unloaded node is tapped", async () => {
    const w = mountRail([user("recent one"), user("recent two")], {
      nodes: [
        { i: 5, text: "ancient" },
        { i: 200, text: "recent one" },
        { i: 201, text: "recent two" },
      ],
      offset: 200,
      total: 202,
      canLoadOlder: true,
    });
    await w.findAll(".rail-node")[0].trigger("click");

    expect(w.emitted("loadUntil")).toBeTruthy();
    expect(w.emitted("loadUntil")![0]).toEqual([5]);
    // And it says so, rather than looking like a dot that does nothing.
    expect(w.findAll(".rail-node")[0].classes()).toContain("pending");
  });

  it("keeps paging until the target arrives, then stops asking", async () => {
    const w = mountRail([user("recent")], {
      nodes: [{ i: 5, text: "ancient" }, { i: 200, text: "recent" }],
      offset: 200,
      total: 201,
      canLoadOlder: true,
    });
    await w.findAll(".rail-node")[0].trigger("click");
    expect(w.emitted("loadUntil")).toHaveLength(1);

    // A page lands but not far enough back — ask again.
    await w.setProps({ events: [user("older"), user("recent")] as never, offset: 100 });
    expect(w.emitted("loadUntil")).toHaveLength(2);

    // Now the target is in the window: no further requests.
    await w.setProps({
      events: [user("ancient"), user("older"), user("recent")] as never,
      offset: 5,
    });
    expect(w.emitted("loadUntil")).toHaveLength(2);
    expect(w.findAll(".rail-node")[0].classes()).not.toContain("pending");
  });

  it("stops asking when the server says there is nothing older", async () => {
    // Otherwise an unreachable node would loop on every page that arrives.
    const w = mountRail([user("recent")], {
      nodes: [{ i: 5, text: "ancient" }, { i: 200, text: "recent" }],
      offset: 200,
      total: 201,
      canLoadOlder: false,
    });
    await w.findAll(".rail-node")[0].trigger("click");
    await w.setProps({ events: [user("x"), user("recent")] as never, offset: 199 });
    expect(w.emitted("loadUntil")).toHaveLength(1);
  });

  // ── the rail is a window, not the whole list ────────────────────────────
  it("shows at most a windowful of dots however long the session is", () => {
    // Every message getting a dot was correct and unusable: dozens of dots
    // down a phone's right edge is a dotted line, not a control.
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("only one loaded")], {
      nodes,
      offset: 39,
      total: 40,
    });
    expect(w.findAll(".rail-node")).toHaveLength(RAIL_WINDOW);
  });

  it("opens on the newest messages", () => {
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("q39")], { nodes, offset: 39, total: 40 });
    const labels = w.findAll(".rail-node").map((d) => d.attributes("aria-label"));
    expect(labels[labels.length - 1]).toContain("message 40 of 40");
  });

  it("spaces the slots evenly, and they do not move", () => {
    // Fixed positions are what make the dots hittable without looking.
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("q39")], { nodes, offset: 39, total: 40 });
    const tops = w.findAll(".rail-node").map((d) => {
      // `top: calc(24px + (100% - 48px) * <fraction>)` — the chevron zones own
      // the ends of the strip, so the slot track is inset.
      const m = /calc\(24px \+ ([\d.]+) \*/.exec(d.attributes("style") ?? "");
      return parseFloat(m![1]);
    });
    const gaps = tops.slice(1).map((v, k) => v - tops[k]);
    for (const g of gaps) expect(g).toBeCloseTo(gaps[0], 4);
    expect(tops[0]).toBeCloseTo(0, 6);
    expect(tops[tops.length - 1]).toBeCloseTo(1, 6);
  });

  it("does NOT move the window when you tap a dot", async () => {
    // The bug: tapping re-centred the window, so pressing the top dot slid it
    // to the middle and three older dots appeared above — the thing under the
    // finger walked away. A dot moves the transcript; only a chevron moves the
    // rail.
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("q39")], {
      nodes,
      offset: 39,
      total: 40,
      canLoadOlder: true,
    });
    const labels = () => w.findAll(".rail-node").map((d) => d.attributes("aria-label"));
    const before = labels();
    expect(before[0]).toContain(`message ${40 - RAIL_WINDOW + 1} of 40`);

    await w.findAll(".rail-node")[0].trigger("click");
    expect(labels()).toEqual(before);

    // And it stays put once the jump settles — the follow rule must not sneak
    // the window across at the end of the animation either.
    await new Promise((r) => setTimeout(r, 500));
    await w.vm.$nextTick();
    expect(labels()).toEqual(before);
  });

  it("moves the window on a chevron tap, without touching the transcript", async () => {
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("q39")], { nodes, offset: 39, total: 40 });
    const first = () => w.findAll(".rail-node")[0].attributes("aria-label");
    expect(first()).toContain(`message ${40 - RAIL_WINDOW + 1} of 40`);

    await w.find(".rail-page-up").trigger("click");

    expect(first()).toContain(`message ${40 - RAIL_WINDOW + 1 - RAIL_PAGE} of 40`);
    // A chevron never jumps: no scroll request goes out.
    expect(w.emitted("loadUntil")).toBeFalsy();
  });

  it("disables the chevron at the end of the list", () => {
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("q39")], { nodes, offset: 39, total: 40 });
    // Opened at the newest end: nothing newer to page toward.
    expect(w.find(".rail-page-down").attributes("disabled")).toBeDefined();
    expect(w.find(".rail-page-up").attributes("disabled")).toBeUndefined();
  });

  it("charts the whole session in the side bar", () => {
    const nodes = Array.from({ length: 40 }, (_, k) => ({ i: k, text: `q${k}` }));
    const w = mountRail([user("q39")], { nodes, offset: 39, total: 40 });
    const thumb = w.find(".rail-bar-thumb");
    expect(thumb.exists()).toBe(true);
    expect(thumb.attributes("style")).toContain(`top: ${((40 - RAIL_WINDOW) / 40) * 100}%`);
  });

  // ── the tapped dot stays lit ────────────────────────────────────────────
  it("keeps the tapped node highlighted after the jump settles", async () => {
    // The dot you pressed went dark the instant the scroll finished, and the
    // PREVIOUS one lit up: the landing centred the message ~45% down the
    // viewport, and "which node am I on" asks whether a message has passed the
    // fold near the top — so the answer became the turn before it. Deterministic,
    // every single tap.
    const w = mountRail([
      user("first"),
      assistant("reply one"),
      user("second"),
      assistant("reply two"),
      user("third"),
    ]);
    // Let mount's deferred scroll-to-bottom land first; on a device it happens
    // long before a tap, and it legitimately cancels a pending jump.
    await new Promise((r) => setTimeout(r, 0));
    const dots = () => w.findAll(".rail-node");
    await dots()[0].trigger("click");
    expect(dots()[0].classes()).toContain("active");

    // Past the settle timer and the correction pass.
    await new Promise((r) => setTimeout(r, 500));
    await w.vm.$nextTick();
    expect(dots()[0].classes()).toContain("active");
    expect(dots()[1].classes()).not.toContain("active");
  });

  it("hands the highlight back once the reader scrolls themselves", async () => {
    // Pinning is "you asked to be here", not a lock — a real gesture returns
    // control to the geometric rule.
    const w = mountRail([user("first"), assistant("r"), user("second")]);
    await new Promise((r) => setTimeout(r, 0));
    await w.findAll(".rail-node")[0].trigger("click");
    expect(w.findAll(".rail-node")[0].classes()).toContain("active");

    await w.find(".stream").trigger("touchmove");
    await new Promise((r) => requestAnimationFrame(r));
    await w.vm.$nextTick();
    // jsdom reports every rect as zero, so the geometric rule sees "at bottom"
    // and answers with the last node — the point is that it answered at all.
    expect(w.findAll(".rail-node")[1].classes()).toContain("active");
  });

  it("shows no window chrome at all when the whole list fits", () => {
    const w = mountRail([user("a"), assistant("r"), user("b")]);
    expect(w.findAll(".rail-node")).toHaveLength(2);
    expect(w.find(".rail-page-up").exists()).toBe(false);
    expect(w.find(".rail-bar").exists()).toBe(false);
  });
});

describe("MessageStream system notes", () => {
  const row = (events: unknown[]) =>
    mount(MessageStream, {
      props: { events: events as never },
      global: { plugins: [Vant] },
    }).find(".msg-row");

  const note = (text: string, level?: "warning") => ({
    type: "system_note" as const,
    ts: "2026-08-25T00:00:00Z",
    text,
    ...(level ? { level } : {}),
  });

  it("renders as system, not as something the user said", () => {
    const r = row([note('Agent "Brain 代码架构设计" finished')]);
    expect(r.classes()).toContain("is-system");
    expect(r.classes()).not.toContain("is-user");
  });

  it("stays quiet when the task completed normally", () => {
    // 96% of these are routine. Amber on all of them is what made the one that
    // failed impossible to spot.
    expect(row([note('Agent "X" finished')]).classes()).not.toContain("is-warning");
  });

  it("flags a task that did not complete", () => {
    // Class only: the body renders markdown asynchronously, so it is empty at
    // mount. That the text itself carries "[failed]" is the router's contract
    // and is pinned in tests/unit/test_message_router.py.
    const r = row([note('Agent "X" finished [failed]', "warning")]);
    expect(r.classes()).toContain("is-warning");
  });
});
