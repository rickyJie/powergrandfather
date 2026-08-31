<script setup lang="ts">
import { onMounted, ref, computed } from "vue";
import { useRouter } from "vue-router";
import { showToast, showConfirmDialog } from "vant";
import { LIVE, useSessionsStore } from "@/stores/sessions";
import { useChatStore } from "@/stores/chat";
import { useNotificationsStore } from "@/stores/notifications";
import { useUiStore, haptic } from "@/stores/ui";
import { sessionsApi, type SessionRow } from "@/api/sessions";
import NewSessionModal from "@/components/NewSessionModal.vue";
import UsageCard from "@/components/UsageCard.vue";

// Session list, rendered as the slide-in drawer of the chat-first shell.
// Rich rows: a live pulse + current tool, the last assistant line as a preview,
// and an unread badge — so the drawer answers "what's happening across all my
// agents" without opening each one.
const router = useRouter();
const store = useSessionsStore();
const chat = useChatStore();
const notifStore = useNotificationsStore();
const ui = useUiStore();

const search = ref("");
const showNewModal = ref(false);
const showHistory = ref(false);

// A live session is one the backend is actually running right now — that's all
// the drawer leads with. Everything that's ended lives under a collapsed
// "History" section so it's reachable without cluttering the active view.
// Every status where the backend still holds a live, writable PTY — i.e. you
// can send to it. Imported rather than re-declared: the store queries exactly
// this set, and a local copy that drifted would either hide a live session in
// History or list a dead one as Active. (waiting_input / waiting_auth were
// once missing here, which made a session go grey the moment it replied.)
const byRecent = (a: SessionRow, b: SessionRow) =>
  (b.last_activity_ts || "").localeCompare(a.last_activity_ts || "");

function matchesSearch(s: SessionRow): boolean {
  const q = search.value.trim().toLowerCase();
  if (!q) return true;
  return (
    (s.title || "").toLowerCase().includes(q) ||
    s.cwd.toLowerCase().includes(q) ||
    (s.agent || "").toLowerCase().includes(q)
  );
}

// Rows resumed into a newer session are dead duplicates sharing one JSONL with
// the live head — hide them everywhere so the list collapses each resume chain
// to its head and you never tap a row that reads but can't be sent to.
const notSuperseded = (s: SessionRow) => !s.superseded_by;

const activeRows = computed<SessionRow[]>(() =>
  store.items
    .filter(
      (s) =>
        s.type !== "chat_agent" &&
        notSuperseded(s) &&
        LIVE.has(s.status) &&
        !s.archived_at &&
        matchesSearch(s)
    )
    .sort(byRecent)
);

const historyRows = computed<SessionRow[]>(() =>
  store.items
    .filter(
      (s) =>
        s.type !== "chat_agent" &&
        notSuperseded(s) &&
        (!LIVE.has(s.status) || !!s.archived_at) &&
        matchesSearch(s)
    )
    .sort(byRecent)
);

function dotClass(s: SessionRow): string {
  if (s.status === "running") return "live"; // actively working (green pulse)
  // Alive but not mid-turn — ready for your input, or blocked on a prompt.
  if (["idle", "waiting_input", "waiting_auth", "starting"].includes(s.status))
    return "idle";
  return "off"; // terminal (ended / exited / crashed)
}

function preview(s: SessionRow): string {
  // Strip the loudest markdown so the one-line preview reads as prose, not
  // source: bold/italic/code markers, heading hashes, list bullets, links.
  let t = (s.last_assistant_msg || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\*\*([^*]*)\*\*/g, "$1")
    .replace(/[*_#>`]/g, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return t.length > 80 ? t.slice(0, 80) + "…" : t;
}

function relTime(iso: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`;
  return `${Math.floor(diff / 86_400_000)}d`;
}

// Three independent sources feed this badge, and `??` let the first one that
// merely EXISTS hide the others — chat.unread is set to 0 the moment a session
// is opened and never deleted, so it permanently masked new messages that
// arrived after you left. Take the max instead; each source is cleared
// explicitly when the session is opened (ChatView.boot).
//   notifStore  — new_message rows, the only signal that reaches you while
//                 you're looking at some OTHER chat (WS push).
//   chat.unread — live events on the socket this client owns.
//   unread_count— server-side, bumped by permission prompts.
function unread(s: SessionRow): number {
  return Math.max(
    notifStore.unreadBySession[s.id] ?? 0,
    chat.unread[s.id] ?? 0,
    s.unread_count ?? 0
  );
}

function open(row: SessionRow) {
  haptic(8);
  ui.setActive(row.id);
  ui.closeDrawer();
  router.push(`/s/${row.id}`);
}

async function swipeArchive(row: SessionRow) {
  try {
    const updated = await sessionsApi.setArchived(row.id, !row.archived_at);
    store.upsertOne(updated);
    haptic();
    showToast({ message: row.archived_at ? "Unarchived" : "Archived", duration: 700 });
  } catch {
    showToast({ message: "Failed", type: "fail" });
  }
}

async function swipeStop(row: SessionRow) {
  try {
    await showConfirmDialog({
      title: "Stop session?",
      message: "SIGINT → SIGTERM → SIGKILL (up to 15s).",
      confirmButtonText: "Stop",
    });
  } catch {
    return;
  }
  try {
    await sessionsApi.stop(row.id);
    store.removeOne(row.id);
    haptic(20);
    showToast({ message: "Stopped", type: "success" });
  } catch {
    showToast({ message: "Stop failed", type: "fail" });
  }
}

function onCreated(sid: string) {
  ui.setActive(sid);
  ui.closeDrawer();
  router.push(`/s/${sid}`);
}

onMounted(() => store.refresh());

// History rows aren't fetched until you ask for them — see the store. The
// drawer opens showing only live sessions, which is all that's on screen.
async function toggleHistory() {
  showHistory.value = !showHistory.value;
  if (showHistory.value) await store.loadHistory();
}
</script>

<template>
  <div class="drawer">
    <header class="drawer-head">
      <span class="brand">Sessions</span>
      <!-- No bell: a global unread counter says "something, somewhere" and
           then makes you hunt for it. The per-row badge below carries the
           same new_message notifications, on the session they belong to. -->
      <div class="head-actions">
        <button class="icon-btn" aria-label="Close" @click="ui.closeDrawer()">
          <van-icon name="cross" size="20" />
        </button>
      </div>
    </header>

    <!-- Plan quota. Sits above the list because "can I keep going at all?"
         outranks "which session do I open" — and because the drawer is the
         one surface you pass through on every session switch. -->
    <UsageCard />

    <div class="filters">
      <van-search v-model="search" placeholder="Search title / cwd / agent" />
    </div>

    <div class="list">
      <div v-if="store.loading && !activeRows.length && !historyRows.length" class="state">
        <van-loading />
      </div>
      <van-empty
        v-else-if="!activeRows.length && !historyRows.length"
        description="No sessions"
      />

      <template v-else>
        <!-- Active: what the backend is running right now. -->
        <div v-if="activeRows.length" class="sec-label">Active · {{ activeRows.length }}</div>
        <p v-else class="sec-empty">No live sessions.</p>
        <van-swipe-cell v-for="row in activeRows" :key="row.id">
          <div class="row" @click="open(row)">
            <span class="dot" :class="dotClass(row)" />
            <div class="row-body">
              <div class="row-top">
                <span class="row-title">
                  {{ row.title || row.cwd.split("/").pop() || row.id.slice(0, 8) }}
                </span>
                <span class="row-time">{{ relTime(row.last_activity_ts) }}</span>
              </div>
              <div class="row-sub">
                <span v-if="row.current_tool" class="tool mono">{{ row.current_tool }}</span>
                <span v-else-if="preview(row)" class="prev">{{ preview(row) }}</span>
                <span v-else class="cwd mono">{{ row.cwd }}</span>
              </div>
            </div>
            <span v-if="unread(row) > 0" class="badge">{{ unread(row) }}</span>
          </div>
          <template #right>
            <div class="swipe-actions">
              <button class="sw archive" @click.stop="swipeArchive(row)">
                {{ row.archived_at ? "Unarch" : "Archive" }}
              </button>
              <button class="sw stop" @click.stop="swipeStop(row)">Stop</button>
            </div>
          </template>
        </van-swipe-cell>

        <!-- History: ended / archived sessions, collapsed by default. -->
        <button
          v-if="historyRows.length"
          class="sec-toggle"
          @click="toggleHistory"
        >
          <span>History · {{ historyRows.length }}</span>
          <van-icon :name="showHistory ? 'arrow-up' : 'arrow-down'" />
        </button>
        <template v-if="showHistory">
          <!-- History is fetched on first expand, so show progress instead of
               an empty section that reads as "no history". -->
          <div v-if="store.loadingMore && !historyRows.length" class="state">
            <van-loading size="18" />
          </div>
          <van-swipe-cell v-for="row in historyRows" :key="row.id">
            <div class="row is-history" @click="open(row)">
              <span class="dot" :class="dotClass(row)" />
              <div class="row-body">
                <div class="row-top">
                  <span class="row-title">
                    {{ row.title || row.cwd.split("/").pop() || row.id.slice(0, 8) }}
                  </span>
                  <span class="row-time">{{ relTime(row.last_activity_ts) }}</span>
                </div>
                <div class="row-sub">
                  <span v-if="preview(row)" class="prev">{{ preview(row) }}</span>
                  <span v-else class="cwd mono">{{ row.cwd }}</span>
                </div>
              </div>
              <span v-if="unread(row) > 0" class="badge">{{ unread(row) }}</span>
            </div>
            <template #right>
              <div class="swipe-actions">
                <button class="sw archive" @click.stop="swipeArchive(row)">
                  {{ row.archived_at ? "Unarch" : "Archive" }}
                </button>
              </div>
            </template>
          </van-swipe-cell>
        </template>
      </template>
    </div>

    <button class="new-btn" @click="showNewModal = true">
      <van-icon name="plus" size="18" /> New session
    </button>

    <NewSessionModal v-model:show="showNewModal" @created="onCreated" />
  </div>
</template>

<style scoped>
.drawer {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--canvas);
}
.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: calc(env(safe-area-inset-top, 0px) + 12px) 12px 8px;
}
.brand {
  font-size: 18px;
  font-weight: 700;
  color: var(--ink);
}
.head-actions {
  display: flex;
  gap: 2px;
}
.icon-btn {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  border: none;
  background: transparent;
  color: var(--ink-2);
  border-radius: 9px;
}
.icon-btn:active {
  background: var(--accent-soft-bg);
}
.filters {
  padding: 0 4px 4px;
}
.list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0 8px;
}
.state {
  padding: 40px;
  text-align: center;
}
.sec-label {
  font-size: var(--fs-label);
  font-weight: 700;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: var(--text-faint);
  padding: 10px 16px 4px;
}
.sec-empty {
  margin: 0;
  padding: 6px 16px 10px;
  font-size: 13px;
  color: var(--text-faint);
}
.sec-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 12px 16px 4px;
  margin-top: 6px;
  border: none;
  border-top: 1px solid var(--outline-soft);
  background: transparent;
  font-size: var(--fs-label);
  font-weight: 700;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: var(--text-faint);
}
.row.is-history {
  opacity: 0.72;
}
.row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 14px;
  background: var(--canvas);
}
.row:active {
  background: var(--accent-soft-bg);
}
.dot {
  flex: none;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--ink-faint);
}
.dot.idle {
  background: var(--van-warning-color);
}
.dot.off {
  background: var(--ink-faint);
}
.dot.live {
  background: var(--success);
  animation: pulse 1.8s infinite;
}
@keyframes pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(74, 138, 94, 0.5);
  }
  70% {
    box-shadow: 0 0 0 6px rgba(74, 138, 94, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(74, 138, 94, 0);
  }
}
.row-body {
  flex: 1;
  min-width: 0;
}
.row-top {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.row-title {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-time {
  flex: none;
  font-size: 11px;
  color: var(--ink-faint);
}
.row-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--ink-mute);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tool {
  color: var(--accent-soft-fg);
}
.mono {
  font-family: var(--font-mono);
}
.badge {
  flex: none;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}
.swipe-actions {
  display: flex;
  height: 100%;
}
.sw {
  border: none;
  color: #fff;
  font-size: 12px;
  padding: 0 16px;
  height: 100%;
}
.sw.archive {
  background: var(--ink-mute);
}
.sw.stop {
  background: var(--van-warning-color);
}
.new-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin: 8px 12px calc(env(safe-area-inset-bottom, 0px) + 12px);
  padding: 12px;
  border: 1px dashed var(--border-strong);
  border-radius: 12px;
  background: var(--card);
  color: var(--accent);
  font-size: 14px;
  font-weight: 600;
}
.new-btn:active {
  background: var(--accent-soft-bg);
}
.sr-only {
  display: none;
}
</style>
