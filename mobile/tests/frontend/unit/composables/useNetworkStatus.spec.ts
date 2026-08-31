import { describe, it, expect, beforeEach, vi } from "vitest";
import { defineComponent, h } from "vue";
import { mount, flushPromises } from "@vue/test-utils";
import MockAdapter from "axios-mock-adapter";
import { useNetworkStatus } from "../../../../frontend/src/composables/useNetworkStatus";
import { http } from "../../../../frontend/src/api/client";

let captured: ReturnType<typeof useNetworkStatus> | null = null;
function makeHarness() {
  return defineComponent({
    setup() {
      const s = useNetworkStatus();
      captured = s;
      // Expose reactive refs so the test can peek at them
      return () => h("div", `${s.online.value}|${s.tunnelAlive.value}`);
    },
  });
}

describe("useNetworkStatus", () => {
  let mock: MockAdapter;
  beforeEach(() => {
    mock = new MockAdapter(http);
    // Ensure default = online
    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });

  it("marks tunnel alive after successful /api/health probe", async () => {
    mock.onGet("/api/health").reply(200, { status: "ok" });
    const H = makeHarness();
    const wrapper = mount(H);
    await flushPromises();
    expect(wrapper.text()).toContain("true|true");
  });

  it("marks tunnel down only after FIVE consecutive /api/health failures", async () => {
    // Was THREE. Raised in 6e6fe21 because a phone's tunnel stalls for 10-30s
    // routinely (Doze, radio handoff) and three misses tripped the offline
    // banner on healthy-but-laggy links. The test was not updated with it and
    // had been failing ever since — which is also why nobody noticed the four
    // other reds in this suite.
    mock.onGet("/api/health").networkError();
    const H = makeHarness();
    const wrapper = mount(H);
    await flushPromises();
    await new Promise((r) => setTimeout(r, 30));
    // Fail 1 (mount) — debounced, still considered alive.
    expect(wrapper.text()).toContain("true|true");
    for (const n of [2, 3, 4]) {
      await captured!.probe();
      await flushPromises();
      expect(wrapper.text(), `fail ${n} must still read as alive`).toContain("true|true");
    }
    await captured!.probe(); // fail 5 → down
    await flushPromises();
    expect(wrapper.text()).toContain("false|false");
  });

  it("recovers to alive after a success following failures", async () => {
    mock.onGet("/api/health").networkError();
    const H = makeHarness();
    const wrapper = mount(H);
    await flushPromises();
    for (let i = 0; i < 4; i++) await captured!.probe(); // fails 2-5 → down
    await flushPromises();
    expect(wrapper.text()).toContain("false|false");
    mock.reset();
    mock.onGet("/api/health").reply(200, { status: "ok" });
    await captured!.probe(); // success resets
    await flushPromises();
    expect(wrapper.text()).toContain("true|true");
  });
});
