import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { sessionsApi } from "@/api/sessions";
import { LIVE_STATUSES, useSessionsStore } from "@/stores/sessions";

/**
 * Opening the drawer used to pull 100 rows of EVERY status — 227KB over the
 * tunnel to render the 3 live sessions actually on screen, because History
 * starts collapsed. Live-only is 9KB and answers in 8ms server-side.
 */
describe("sessions store — what the drawer actually fetches", () => {
  let list: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    setActivePinia(createPinia());
    list = vi.spyOn(sessionsApi, "list").mockResolvedValue({
      count: 3,
      items: [],
      has_more: false,
    } as never);
  });

  it("asks only for live sessions on open", async () => {
    await useSessionsStore().refresh();

    expect(list).toHaveBeenCalledTimes(1);
    expect(list.mock.calls[0][0]).toMatchObject({ status: LIVE_STATUSES });
  });

  it("does not fetch history until it is expanded", async () => {
    const store = useSessionsStore();
    await store.refresh();
    expect(store.historyLoaded).toBe(false);

    await store.loadHistory();

    // The history call is the unfiltered one.
    expect(list.mock.calls[1][0]?.status).toBeUndefined();
    expect(store.historyLoaded).toBe(true);
  });

  it("expanding history twice costs one request", async () => {
    const store = useSessionsStore();
    await store.loadHistory();
    await store.loadHistory();

    expect(list).toHaveBeenCalledTimes(1);
  });

  it("keeps the full list accurate once history is on screen", async () => {
    // Otherwise a refresh would quietly drop every history row the user is
    // looking at.
    const store = useSessionsStore();
    await store.loadHistory();
    list.mockClear();

    await store.refresh();

    expect(list.mock.calls[0][0]?.status).toBeUndefined();
  });

  it("an explicit status filter is never overridden", async () => {
    const store = useSessionsStore();
    store.setFilter({ status: "exited" });

    await store.refresh();

    expect(list.mock.calls[0][0]).toMatchObject({ status: "exited" });
  });
});
