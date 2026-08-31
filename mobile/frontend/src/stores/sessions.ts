import { defineStore } from "pinia";
import { ref } from "vue";
import {
  sessionsApi,
  type SessionRow,
  type SessionListResult,
} from "@/api/sessions";

// Sessions list + basic detail cache. Message-stream state (per session)
// lives in the chat store (same shape as agent conversations) so we don't
// duplicate reactive maps.

const PAGE = 100;

// Statuses where the backend still holds a live, writable PTY. Exported so
// the drawer's Active/History split is derived from the SAME list the query
// uses — two hand-maintained copies would drift, and a status missing from
// one of them makes a perfectly live session render as dead history.
export const LIVE_STATUSES = "starting,running,idle,waiting_input,waiting_auth";
export const LIVE = new Set(LIVE_STATUSES.split(","));

export const useSessionsStore = defineStore("sessions", () => {
  const items = ref<SessionRow[]>([]);
  const loading = ref(false);
  const loadingMore = ref(false);
  const hasMore = ref(false);
  const error = ref(false);
  const filter = ref<{ status?: string; type?: string; agent?: string }>({});
  // History is fetched only once the user expands that section. Opening the
  // drawer used to pull 100 rows of every status — 227KB over the tunnel to
  // render the 3 live sessions actually on screen, since History starts
  // collapsed. Live-only is 9KB and answers in 8ms server-side.
  const historyLoaded = ref(false);

  function applyPage(res: SessionListResult, append: boolean) {
    items.value = append ? [...items.value, ...res.items] : res.items;
    hasMore.value = res.has_more; // authoritative flag from the backend
  }

  async function refresh() {
    loading.value = true;
    try {
      // An explicit status filter wins; otherwise fetch just the live set
      // unless the user has already expanded History, in which case they
      // opted into the full list and it must stay accurate.
      const wantAll = !!filter.value.status || historyLoaded.value;
      const res = await sessionsApi.list({
        ...filter.value,
        ...(wantAll ? {} : { status: LIVE_STATUSES }),
        limit: PAGE,
        offset: 0,
      });
      applyPage(res, false);
      error.value = false;
    } catch {
      error.value = true;
    } finally {
      loading.value = false;
    }
  }

  /** Pull the rows behind the collapsed "History" section. Idempotent: the
   *  first expand pays for it, later ones are free. */
  async function loadHistory() {
    if (historyLoaded.value || loadingMore.value) return;
    loadingMore.value = true;
    try {
      const res = await sessionsApi.list({ ...filter.value, limit: PAGE, offset: 0 });
      applyPage(res, false);
      historyLoaded.value = true;
      error.value = false;
    } catch {
      error.value = true;
    } finally {
      loadingMore.value = false;
    }
  }

  async function loadMore() {
    if (loadingMore.value || !hasMore.value) return;
    loadingMore.value = true;
    try {
      const res = await sessionsApi.list({
        ...filter.value,
        limit: PAGE,
        offset: items.value.length,
      });
      applyPage(res, true);
    } finally {
      loadingMore.value = false;
    }
  }

  function setFilter(next: typeof filter.value) {
    filter.value = { ...next };
  }

  function upsertOne(row: SessionRow) {
    const i = items.value.findIndex((s) => s.id === row.id);
    if (i === -1) items.value.unshift(row);
    else items.value[i] = row;
  }

  function removeOne(sid: string) {
    items.value = items.value.filter((s) => s.id !== sid);
  }

  /** Zero the cached row's unread badge. The server side is cleared by
   *  `notifications.markSessionRead`, but the row in this list was fetched
   *  before that and would keep showing the stale count until the next
   *  refresh — which, since the drawer only fetches on first mount, could be
   *  never. */
  function clearUnread(sid: string) {
    const row = items.value.find((s) => s.id === sid);
    if (row && row.unread_count) row.unread_count = 0;
  }

  return {
    items,
    loading,
    loadingMore,
    hasMore,
    error,
    filter,
    historyLoaded,
    refresh,
    loadHistory,
    loadMore,
    setFilter,
    upsertOne,
    removeOne,
    clearUnread,
  };
});
