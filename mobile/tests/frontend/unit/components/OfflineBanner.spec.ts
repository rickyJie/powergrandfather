import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import MockAdapter from "axios-mock-adapter";
import Vant from "vant";
import OfflineBanner from "../../../../frontend/src/components/OfflineBanner.vue";
import { http } from "../../../../frontend/src/api/client";

describe("OfflineBanner", () => {
  let mock: MockAdapter;
  beforeEach(() => {
    mock = new MockAdapter(http);
    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });

  it("hides itself when online + tunnel alive", async () => {
    mock.onGet("/api/health").reply(200, { status: "ok" });
    const wrapper = mount(OfflineBanner, { global: { plugins: [Vant] } });
    await flushPromises();
    expect(wrapper.find(".banner").exists()).toBe(false);
  });

  it("does NOT flap the banner on a single /api/health miss (debounced)", async () => {
    // A single transient failure must not show "backend unreachable" — the
    // probe requires two consecutive misses (see useNetworkStatus).
    mock.onGet("/api/health").networkError();
    const wrapper = mount(OfflineBanner, { global: { plugins: [Vant] } });
    await flushPromises();
    await new Promise((r) => setTimeout(r, 30));
    expect(wrapper.find(".banner.tunnel").exists()).toBe(false);
  });
});
