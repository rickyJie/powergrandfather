<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { showToast, showConfirmDialog } from "vant";
import MessageStream from "@/components/message-stream/MessageStream.vue";
import MessageInput from "@/components/message-stream/MessageInput.vue";
import TuiSessionCard from "@/components/TuiSessionCard.vue";
import ChoicePanel from "@/components/ChoicePanel.vue";
import SessionChangesSheet from "@/components/SessionChangesSheet.vue";
import { sessionsApi, type SessionRow } from "@/api/sessions";
import { useSessionMessageSocket, type ChatWebSocket } from "@/api/ws";
import { useChatStore } from "@/stores/chat";
import { useSessionsStore } from "@/stores/sessions";
import { useNotificationsStore } from "@/stores/notifications";
import { useUiStore, haptic } from "@/stores/ui";
import {
  detectPendingChoice,
  multiSelectKeys,
  submitAnswersKeys,
  type ChoiceOption,
} from "@/lib/pendingChoice";

// Immersive, chat-first session view. The session id comes from the route
// (/s/:sid deep link) or, at the bare "/" home, from the persisted active
// session. Session switching happens in the drawer (☰), not a route stack.
const route = useRoute();
const router = useRouter();
const chat = useChatStore();
const sessionsStore = useSessionsStore();
const notifStore = useNotificationsStore();
const ui = useUiStore();

const sid = computed(
  () => (route.params.sid as string | undefined) || ui.activeSid || ""
);

const session = ref<SessionRow | null>(null);
const loadingMeta = ref(false);
const stopping = ref(false);
const interrupting = ref(false);
// Composer component ref — used to restore the draft if a fire-and-forget send
// later fails (see onSend).
const composerRef = ref<{ restore: (t: string) => void } | null>(null);
const wsConnected = ref(false);
const reconnectAttempt = ref(0);
const permanentClose = ref(false);
const waiting = ref(false);
const emptySession = ref(false);
let socket: ChatWebSocket | null = null;

// True from a session switch until that session's transcript actually shows
// up. The meta GET resolving and the transcript arriving are two different
// round trips, and in the gap between them MessageStream saw `loading=false,
// rows=[]` and printed "No messages yet." over a session with plenty — the
// switch looked broken for a beat, then messages popped in.
const hydrating = ref(false);
// A socket that connects and then says nothing must not spin forever.
const HYDRATE_CAP_MS = 15_000;
let hydrateTimer: number | null = null;

function stopHydrate() {
  hydrating.value = false;
  if (hydrateTimer !== null) {
    clearTimeout(hydrateTimer);
    hydrateTimer = null;
  }
}

function beginHydrate() {
  stopHydrate();
  // Coming back to a session we already hold messages for: render them at
  // once. Switching back and forth is most of what switching feels like, and
  // a spinner over content we already have is pure noise.
  if ((chat.transcripts[sid.value]?.length ?? 0) > 0) return;
  hydrating.value = true;
  hydrateTimer = window.setTimeout(stopHydrate, HYDRATE_CAP_MS);
}

// Mirrors the backend's _CHATTABLE_AGENTS. route_record() dispatches on
// record shape and normalises claude JSONL and codex rollouts into the same
// chat envelope, so both render here identically. Codex chat is narrower —
// its rollout carries user messages and each turn's final reply, but no
// tool-call progress — which is a content gap, not a rendering one.
// Any other agent has no transcript we can parse: fall back to the terminal
// card rather than sit on a socket that will never emit.
const CHATTABLE_AGENTS = ["claude", "codex"];
const agent = computed(() => session.value?.agent ?? "claude");
const asChat = computed(
  () => !!session.value && CHATTABLE_AGENTS.includes(agent.value),
);
const transcript = computed(() => chat.transcripts[sid.value] ?? []);

// The user's most recent sent message — offered in the composer for one-tap
// edit-and-resend. Skips `injected` records for the same reason the jump rail
// does: they carry role "user" without being the user's words, so without the
// check the composer offers to "resend" a skill preamble or the Esc-interrupt
// marker. Same flag, same rule — see UserMessageEvent.injected.
const lastUserText = computed(() => {
  const t = transcript.value;
  for (let i = t.length - 1; i >= 0; i--) {
    const ev = t[i] as { type: string; text: string; injected?: boolean };
    if (ev.type === "user_message" && !ev.injected) return ev.text;
  }
  return "";
});

// ---- lazy older-history paging ----
const loadingOlder = ref(false);
const canLoadOlder = computed(() => chat.canLoadOlder(sid.value));
function onLoadOlder() {
  if (loadingOlder.value || !socket) return;
  // Don't fire into a dead/reconnecting socket — the request would be dropped
  // and the button would just spin for the 8s safety timeout.
  if (!socket.isOpen) {
    showToast({ message: "Reconnecting — try again in a moment", duration: 1200 });
    return;
  }
  const before = chat.historyMeta[sid.value]?.offset ?? 0;
  if (before <= 0) return;
  loadingOlder.value = true;
  socket.send({ type: "load_history", before });
  // Safety: never wedge the button if the page frame is lost on a flaky tunnel.
  window.setTimeout(() => (loadingOlder.value = false), 8000);
}

// The jump rail spans the whole session, so tapping a dot can land on a message
// that hasn't been paged in. Same request as the button — MessageStream keeps
// asking as each page arrives until its target is loaded, then scrolls.
function onLoadUntil(_index: number) {
  onLoadOlder();
}

// Whole-session index for the rail + the pagination cursor it reads against.
const railNodes = computed(() => chat.railNodes(sid.value));
const railTotal = computed(() => chat.railTotal(sid.value));
const railOffset = computed(() => chat.historyMeta[sid.value]?.offset ?? 0);
const TERMINAL_CODES = new Set([4401, 4404, 4500]);

const isRunning = computed(() => session.value?.status === "running");
// Any status where the PTY is still live (sendable) — so the header dot reads
// "alive" for waiting_input/idle too, not just while a turn is executing.
const isLive = computed(() =>
  ["starting", "running", "idle", "waiting_input", "waiting_auth"].includes(
    session.value?.status ?? ""
  )
);
const statusLabel = computed(() => session.value?.status || "…");
// A running session with a live current_tool answers "what is it doing?" at a
// glance — the single most useful thing on a phone.
const currentTool = computed(() => session.value?.current_tool || "");

// ---- interactive-choice panel ----
// When claude is blocked on a picker whose options we can render (AskUserQuestion
// / plan approval), surface tappable buttons that write the raw key sequence to
// the PTY. Options come from the JSONL tool_use; the "manual" row is a hedge for
// calibrating the picker's keystrokes (arrow vs number) against a live prompt.
const pendingChoice = computed(() => detectPendingChoice(transcript.value));
const choosing = ref(false);

// Client-side state machine mirroring the real AskUserQuestion TUI. A single
// tool_use can bundle several sub-questions the CLI walks through one screen at
// a time, and after the LAST question it shows a "✔ Submit" review screen that
// needs one more Enter. The transcript emits NOTHING between sub-questions (the
// lone tool_use_result lands only after that final Submit), so we drive the
// whole sequence off a local step cursor:
//   step in [0, steps.length)  → answering that sub-question
//   step === steps.length      → the Submit review screen (multi-question only)
//   step  >  steps.length      → answers sent, waiting for tool_use_result
// Reset on a NEW tool call (keyed on toolId) so a fresh prompt starts at Q1 and
// a WS replay of the same call doesn't rewind a half-answered picker.
const choiceStep = ref(0);
const submitted = ref(false);
// Ticked rows for the current multi-select question (UI-only until confirmed).
const multiSel = ref<number[]>([]);
watch(
  () => pendingChoice.value?.toolId,
  () => {
    choiceStep.value = 0;
    submitted.value = false;
    multiSel.value = [];
  }
);
// A fresh sub-question starts with nothing ticked.
watch(choiceStep, () => {
  multiSel.value = [];
});

const currentStep = computed(() => {
  const pc = pendingChoice.value;
  if (!pc) return null;
  return pc.steps[choiceStep.value] ?? null;
});
// answer → a question is on screen; submit → the review/Submit screen; else the
// answers are in flight and we wait for pendingChoice to clear.
const choicePhase = computed<"answer" | "submit" | "submitting">(() => {
  const pc = pendingChoice.value;
  if (!pc) return "submitting";
  if (choiceStep.value < pc.steps.length) return "answer";
  // Only a MULTI-question prompt has the trailing Submit screen; a single
  // question submits on the pick itself.
  if (pc.steps.length > 1 && !submitted.value) return "submit";
  return "submitting";
});
// "2 / 3" progress hint, blank for single-question prompts.
const choiceProgress = computed(() => {
  const pc = pendingChoice.value;
  if (!pc || pc.steps.length <= 1) return "";
  const shown = Math.min(choiceStep.value + 1, pc.steps.length);
  return `${shown} / ${pc.steps.length}`;
});

function advanceStep() {
  const pc = pendingChoice.value;
  if (pc && choiceStep.value <= pc.steps.length) choiceStep.value += 1;
}

// Single-select pick: Enter selects the row AND auto-advances the CLI to the
// next question (or the Submit screen for the last one).
async function pickOption(opt: ChoiceOption) {
  if (choosing.value) return;
  choosing.value = true;
  haptic();
  try {
    await sessionsApi.sendKeys(sid.value, opt.keys);
    advanceStep();
  } catch {
    showToast({ message: "Send failed", type: "fail" });
  } finally {
    choosing.value = false;
  }
}

// Multi-select: taps only toggle local UI state; the keystrokes are batched
// here — tick every chosen row, drop to "Next", Enter to advance.
function toggleMulti(idx: number) {
  haptic(8);
  const cur = multiSel.value;
  multiSel.value = cur.includes(idx)
    ? cur.filter((i) => i !== idx)
    : [...cur, idx];
}
async function confirmMulti() {
  const step = currentStep.value;
  if (!step || choosing.value) return;
  choosing.value = true;
  haptic();
  try {
    await sessionsApi.sendKeys(
      sid.value,
      multiSelectKeys(multiSel.value, step.options.length)
    );
    advanceStep();
  } catch {
    showToast({ message: "Send failed", type: "fail" });
  } finally {
    choosing.value = false;
  }
}

// The final Enter on the "✔ Submit → Submit answers" screen. Without it a
// multi-question prompt stalls forever on the review screen.
async function submitAnswers() {
  if (choosing.value) return;
  choosing.value = true;
  haptic();
  try {
    await sessionsApi.sendKeys(sid.value, submitAnswersKeys);
    submitted.value = true;
  } catch {
    showToast({ message: "Send failed", type: "fail" });
  } finally {
    choosing.value = false;
  }
}

function sendKey(keys: string) {
  haptic(8);
  sessionsApi.sendKeys(sid.value, keys).catch(() => {
    showToast({ message: "Key send failed", type: "fail" });
  });
}

// ---- meta / actions ----
const actionsOpen = ref(false);
const renameOpen = ref(false);
const renameText = ref("");
const resuming = ref(false);
const archiving = ref(false);
const isArchived = computed(() => !!session.value?.archived_at);
const canResume = computed(() => {
  const s = session.value;
  if (!s) return false;
  if (!["ended", "exited", "crashed"].includes(s.status)) return false;
  // boot() already redirects off a superseded row, but don't offer Resume on
  // one if we somehow land here — the live head is the resumable row.
  if (s.superseded_by) return false;
  // Resumability is per-adapter, and `jsonl_present` answers it for claude
  // ONLY — the backend hardcodes it to false for every other agent because it
  // literally means "the claude JSONL is still on disk". Testing it
  // unconditionally hid Resume on every codex session, even though the resume
  // endpoint supports them (it locates the rollout via rollout_path with a
  // scan fallback). Mirrors the desktop check in views/Sessions.vue.
  const agent = s.agent ?? s.backend ?? "claude";
  if (agent === "claude") {
    return !!s.external_session_id && s.jsonl_present !== false;
  }
  if (agent === "codex") {
    return !!(s.external_session_id || s.rollout_path);
  }
  return false;
});

const actionSheet = computed(() => {
  const s = session.value;
  if (!s) return [];
  const items: { name: string; color?: string; disabled?: boolean }[] = [];
  if (canResume.value) items.push({ name: "Resume" });
  items.push({ name: "Changes" });
  items.push({ name: isArchived.value ? "Unarchive" : "Archive" });
  items.push({ name: "Stop", color: "var(--van-warning-color)" });
  items.push({ name: "Kill", color: "var(--van-danger-color)" });
  items.push({ name: "Purge transcript", color: "var(--van-danger-color)" });
  return items;
});

async function onAction(item: { name: string }) {
  actionsOpen.value = false;
  switch (item.name) {
    case "Resume":
      return doResume();
    case "Changes":
      return openChanges();
    case "Archive":
    case "Unarchive":
      return doArchive();
    case "Stop":
      return stopSession();
    case "Kill":
      return killSession();
    case "Purge transcript":
      return doPurge();
  }
}

async function loadSession() {
  if (!sid.value) {
    session.value = null;
    return;
  }
  loadingMeta.value = true;
  try {
    session.value = await sessionsApi.get(sid.value);
  } catch (e) {
    // 404 is the only answer that means the row is really gone. Everything
    // else is the tunnel — and throwing away a session we can already render
    // (seeded from the drawer's list) because one request blipped turns a
    // hiccup into "Session not found" on a session that is perfectly alive.
    if ((e as { response?: { status?: number } })?.response?.status === 404) {
      session.value = null;
      showToast({ message: "Session not found", type: "fail" });
    } else if (!session.value) {
      showToast({ message: "Could not load session", type: "fail" });
    }
  } finally {
    loadingMeta.value = false;
  }
}

/** The row the drawer already holds for `sid`, if any.
 *
 *  Everything the header shows — title, cwd, live dot, agent — is already in
 *  the list the user just tapped a row in. Rendering it immediately is what
 *  makes the header switch WITH the tap instead of one `/api/sessions/{sid}`
 *  round trip later. */
function cachedRow(): SessionRow | null {
  return sessionsStore.items.find((s) => s.id === sid.value) ?? null;
}

// A resumed session leaves a chain of dead rows that all share one JSONL: they
// still READ live (shared transcript) but WRITE fails ("session not live") on
// every row but the live head. Follow `superseded_by` to that head so a stale
// activeSid / a tapped dead row lands on the one you can actually send to.
async function resolveLiveHead(row: SessionRow | null): Promise<string | null> {
  if (!row) return null;
  let cur = row;
  const seen = new Set<string>();
  while (cur.superseded_by && !seen.has(cur.id)) {
    seen.add(cur.id);
    try {
      cur = await sessionsApi.get(cur.superseded_by);
    } catch {
      break;
    }
  }
  return cur.id;
}

function attachSocket() {
  if (!asChat.value) return;
  // Never leave a previous socket alive — a lingering one keeps its own ws +
  // heartbeat + reconnect and ingests every event a SECOND time (the "N
  // duplicate messages" bug). Force it down before opening a fresh one.
  teardownSocket();
  // Every one of these describes the session we are LEAVING. Carrying them
  // into the new one is how a healthy session opens under "No messages yet —
  // send one to start." or "Reconnecting… (attempt 4)".
  permanentClose.value = false;
  waiting.value = false;
  emptySession.value = false;
  wsConnected.value = false;
  reconnectAttempt.value = 0;
  socket = useSessionMessageSocket(sid.value, {
    onOpen: () => {
      wsConnected.value = true;
      reconnectAttempt.value = 0;
      permanentClose.value = false;
    },
    onClose: (ev) => {
      wsConnected.value = false;
      if (TERMINAL_CODES.has(ev.code)) {
        permanentClose.value = true;
        stopHydrate(); // don't spin under a "Disconnected" banner
      }
    },
    onReconnect: (attempt) => {
      reconnectAttempt.value = attempt;
    },
    onGaveUp: () => {
      wsConnected.value = false;
      permanentClose.value = true;
      stopHydrate();
    },
    onEvent: (ev) => {
      // Anything at all means the socket is live and answering, which is
      // exactly what "still hydrating" was waiting to find out.
      stopHydrate();
      const t = (ev as { type?: string }).type;
      if (t === "session_status") {
        const st = (ev as { status?: string }).status;
        waiting.value = st === "waiting";
        // `empty` = live session that hasn't produced a transcript yet. Codex
        // registers nothing until the first user turn, so this is the normal
        // state of a freshly spawned one — show the composer and say so
        // rather than looking broken.
        emptySession.value = st === "empty";
      } else {
        waiting.value = false;
        emptySession.value = false;
      }
      if (t === "history_page") loadingOlder.value = false;
      chat.ingest(sid.value, ev);
    },
  });
}

function teardownSocket() {
  socket?.close();
  socket = null;
}

function manualReconnect() {
  teardownSocket();
  permanentClose.value = false;
  reconnectAttempt.value = 0;
  waiting.value = false;
  attachSocket();
}

function onSend(text: string, done: (ok: boolean) => void) {
  // The optimistic bubble goes up instantly, so unlock + clear the composer
  // RIGHT AWAY — do NOT hold the input hostage to the POST round-trip (over a
  // slow tunnel that lock felt like "huge send latency" even though the message
  // was already on screen). The POST is fire-and-forget; on failure we roll the
  // bubble back and restore the draft. NOTE: the backend dedups auto-RETRIES by
  // client_msg_id (client.ts __idempotent), not by text — a MANUAL resend after
  // a restore is a fresh id, so if a "failed" POST had actually reached the PTY
  // it could double-write. Rare (needs a lost response on a live session); the
  // restore keeps the user's text rather than silently dropping it.
  haptic();
  let optimisticId: string | null = null;
  try {
    optimisticId = chat.addOptimisticUser(sid.value, text);
  } catch {
    optimisticId = null;
  }
  done(true);
  sessionsApi.sendMessage(sid.value, text).catch((e: unknown) => {
    if (optimisticId) chat.removeOptimisticUser(sid.value, optimisticId);
    const err = e as {
      response?: { status?: number };
      code?: string;
      message?: string;
    };
    const status = err?.response?.status;
    let detail: string;
    if (status === 409) detail = "session not live";
    // 503 = the PTY took only part of the message, so it never submitted. Say
    // that rather than "HTTP 503": the agent's composer may hold a partial
    // line, which the user needs to know before they resend.
    else if (status === 503) detail = "the agent isn't reading its input; message not submitted";
    else if (status) detail = `HTTP ${status}`;
    else detail = err?.code || err?.message || "network";
    // eslint-disable-next-line no-console
    console.error("[send] failed", e);
    // Only claim "draft kept" if we actually put the text back — if the user
    // has already started a new message, restore() no-ops and we must not lie.
    const kept = composerRef.value?.restore(text) ?? false;
    showToast({
      message: kept
        ? `Send failed (${detail}) — draft kept`
        : `Send failed (${detail})`,
      type: "fail",
    });
  });
}

async function onInterrupt() {
  if (interrupting.value) return;
  interrupting.value = true;
  haptic(20);
  try {
    await sessionsApi.sendMessage(sid.value, "\u0003");
    showToast({ message: "Interrupted", type: "success", duration: 800 });
  } catch {
    showToast({ message: "Interrupt failed", type: "fail" });
  } finally {
    interrupting.value = false;
  }
}

function openRename() {
  renameText.value = session.value?.title || "";
  renameOpen.value = true;
}
async function doRename() {
  const title = renameText.value.trim();
  if (!title) return;
  try {
    const updated = await sessionsApi.patch(sid.value, { title });
    session.value = updated;
    sessionsStore.upsertOne(updated);
    showToast({ message: "Renamed", type: "success", duration: 800 });
  } catch {
    showToast({ message: "Rename failed", type: "fail" });
  }
}

async function doResume() {
  if (resuming.value) return;
  resuming.value = true;
  try {
    const fresh = await sessionsApi.resume(sid.value);
    sessionsStore.upsertOne(fresh);
    showToast({ message: "Resumed", type: "success" });
    router.push(`/s/${fresh.id}`);
  } catch {
    showToast({ message: "Resume failed", type: "fail" });
  } finally {
    resuming.value = false;
  }
}

async function doArchive() {
  if (archiving.value) return;
  archiving.value = true;
  try {
    const updated = await sessionsApi.setArchived(sid.value, !isArchived.value);
    session.value = updated;
    sessionsStore.upsertOne(updated);
    showToast({ message: isArchived.value ? "Archived" : "Unarchived", duration: 800 });
  } catch {
    showToast({ message: "Failed", type: "fail" });
  } finally {
    archiving.value = false;
  }
}

async function doPurge() {
  try {
    await showConfirmDialog({
      title: "Purge transcript?",
      message: "Deletes this session's saved JSONL + output. Irreversible.",
      confirmButtonText: "Purge",
    });
  } catch {
    return;
  }
  try {
    await sessionsApi.purge(sid.value);
    showToast({ message: "Purged", type: "success" });
  } catch {
    showToast({ message: "Purge failed", type: "fail" });
  }
}

async function stopSession() {
  try {
    await showConfirmDialog({
      title: "Stop session?",
      message: "SIGINT → SIGTERM → SIGKILL (up to 15s).",
      confirmButtonText: "Stop",
    });
  } catch {
    return;
  }
  stopping.value = true;
  try {
    await sessionsApi.stop(sid.value);
    sessionsStore.removeOne(sid.value);
    showToast({ message: "Stopped", type: "success" });
    goAfterRemoval();
  } catch {
    showToast({ message: "Stop failed", type: "fail" });
  } finally {
    stopping.value = false;
  }
}

async function killSession() {
  stopping.value = true;
  try {
    await sessionsApi.kill(sid.value);
    sessionsStore.removeOne(sid.value);
    showToast({ message: "Killed", type: "success" });
    goAfterRemoval();
  } catch {
    showToast({ message: "Kill failed", type: "fail" });
  } finally {
    stopping.value = false;
  }
}

/** After stop/kill, leave this (now dead) chat: open the drawer to pick another. */
function goAfterRemoval() {
  ui.setActive(null);
  router.replace("/");
  ui.openDrawer();
}

// ---- changes / diff ----
// The changed-files + diff bottom sheets live in <SessionChangesSheet>, which
// owns its own fetch state; here we just toggle it open.
const changesOpen = ref(false);
function openChanges() {
  changesOpen.value = true;
}

// A session switch flips both route.params.sid AND ui.activeSid, on different
// ticks — so the watcher fires boot() twice for one switch. Without a guard the
// two runs interleave across the `await loadSession()` and the later one's
// socket clobbers the earlier's into an un-closed orphan. This epoch token lets
// a superseded boot bail before it attaches.
let bootEpoch = 0;

// ---- read receipts ----
// The drawer badge is the only place new-message notifications surface now, so
// something has to mark them read or they never clear. That "something" is a
// user gesture — opening a chat, and leaving it — never an incoming message:
// the Android app raises its tray notification from whatever is still unread
// (polling `only_unread=true` every 20s), so clearing on push would race the
// poller and swallow the alert on a backgrounded phone.
let readReceiptFor: string | null = null;

function sendReadReceipt(target: string | null) {
  if (!target) return;
  notifStore.markSessionRead(target).catch(() => {});
  sessionsStore.clearUnread(target);
}

async function boot() {
  const epoch = ++bootEpoch;
  // Leaving counts too: replies that landed while you sat in the chat have
  // been read, and would otherwise badge the session you just came from.
  if (readReceiptFor && readReceiptFor !== sid.value) {
    sendReadReceipt(readReceiptFor);
    readReceiptFor = null;
  }
  const routeSid = route.params.sid as string | undefined;
  if (routeSid) ui.setActive(routeSid);
  chat.setActive(sid.value || null);
  teardownSocket();
  if (!sid.value) {
    session.value = null;
    stopHydrate();
    // Nothing to show yet — nudge the drawer open so the user picks a session.
    ui.openDrawer();
    return;
  }
  // Swap the header to the new session NOW. Whatever is on screen belongs to
  // the session we just left, and leaving it up for a round trip doesn't read
  // as "loading", it reads as the wrong session — or as a title that updates
  // slowly. `null` when we have no cached row is still better: the header
  // falls back to the sid, which is at least about the right session.
  const seeded = cachedRow();
  session.value = seeded;
  // The cached row already says which agent this is and whether it's a dead
  // resumed row, so the transcript socket does not have to wait behind the
  // meta GET. That round trip was pure dead time before the first message.
  const canAttachEarly =
    !!seeded &&
    !seeded.superseded_by &&
    CHATTABLE_AGENTS.includes(seeded.agent ?? seeded.backend ?? "claude");
  if (canAttachEarly) {
    beginHydrate();
    attachSocket();
  }
  await loadSession();
  if (epoch !== bootEpoch) return; // a newer boot superseded us — don't attach
  // If this row was resumed into a newer one, redirect to the live head so the
  // user isn't stranded on a read-only dead row (reads work, sends 409).
  if (session.value?.superseded_by) {
    const head = await resolveLiveHead(session.value);
    if (epoch !== bootEpoch) return;
    if (head && head !== sid.value) {
      ui.setActive(head);
      router.replace(`/s/${head}`); // re-triggers boot() on the live head
      return;
    }
  }
  // Fire-and-forget: nothing on screen waits on it, and a flaky tunnel must
  // not delay the socket.
  if (session.value) {
    sendReadReceipt(sid.value);
    readReceiptFor = sid.value;
  }
  // Reconcile the early attach against what the server actually said: open one
  // if the cache couldn't (cold deep link), close one the fresh row disowns
  // (stale cached agent, or the row turned out not to be chattable).
  if (!socket) {
    beginHydrate();
    attachSocket();
  } else if (!asChat.value) {
    teardownSocket();
    stopHydrate();
  }
}

onMounted(boot);
onBeforeUnmount(() => {
  sendReadReceipt(readReceiptFor);
  readReceiptFor = null;
  chat.setActive(null);
  stopHydrate();
  teardownSocket();
});

// Re-boot when the deep-link sid OR the drawer-selected active session changes.
watch(
  () => [route.params.sid, ui.activeSid],
  (n, o) => {
    if (n[0] === o[0] && n[1] === o[1]) return;
    loadingMeta.value = true;
    boot();
  }
);
</script>

<template>
  <div class="chat-view">
    <!-- top bar -->
    <header class="topbar">
      <button class="icon-btn" aria-label="Sessions" @click="ui.openDrawer()">
        <van-icon name="bars" size="22" />
      </button>
      <div class="title-block" @click="session && openRename()">
        <div class="title-line">
          <span class="dot" :class="{ live: isLive }" v-if="session" />
          <span class="title-text">
            {{ session?.title || (sid ? sid.slice(0, 8) : "No session") }}
          </span>
        </div>
        <div v-if="currentTool" class="subline mono">{{ currentTool }}</div>
        <div v-else-if="session?.cwd" class="subline mono">{{ session.cwd }}</div>
        <div v-else-if="session" class="subline">{{ statusLabel }}</div>
        <!-- Cold deep link: no cached row to seed from, so say we're fetching
             it rather than leaving the sid sitting there looking inert. -->
        <div v-else-if="loadingMeta" class="subline">Loading session…</div>
      </div>
      <button
        v-if="session"
        class="icon-btn"
        aria-label="Actions"
        @click="actionsOpen = true"
      >
        <van-icon name="ellipsis" size="22" />
      </button>
    </header>

    <!-- empty state -->
    <div v-if="!sid || (!loadingMeta && !session)" class="empty">
      <van-empty description="Pick a session">
        <van-button round type="primary" size="small" @click="ui.openDrawer()">
          Open sessions
        </van-button>
      </van-empty>
    </div>

    <!-- agent with no parseable transcript: terminal card instead of chat -->
    <template v-else-if="!loadingMeta && session && !asChat">
      <TuiSessionCard :session-id="sid" :adapter="agent" :cwd="session.cwd" />
    </template>

    <!-- chat (claude / codex) -->
    <template v-else-if="asChat">
      <div v-if="permanentClose" class="banner banner-danger">
        Disconnected —
        <span class="banner-action" @click="manualReconnect">reconnect</span>
      </div>
      <div v-else-if="waiting" class="banner banner-info">
        Waiting for the session to start…
      </div>
      <div v-else-if="emptySession" class="banner banner-info">
        No messages yet — send one to start.
      </div>
      <div v-else-if="!wsConnected && reconnectAttempt > 0" class="banner banner-warn">
        Reconnecting… (attempt {{ reconnectAttempt }})
      </div>

      <MessageStream
        :events="transcript"
        :loading="loadingMeta || hydrating"
        :can-load-older="canLoadOlder"
        :loading-older="loadingOlder"
        :nodes="railNodes"
        :offset="railOffset"
        :total="railTotal"
        class="stream"
        @load-older="onLoadOlder"
        @load-until="onLoadUntil"
      />

      <!-- interactive choice panel: tappable options for an in-terminal picker -->
      <ChoicePanel
        v-if="pendingChoice"
        :step="currentStep"
        :phase="choicePhase"
        :progress="choiceProgress"
        :selected="multiSel"
        :disabled="choosing"
        @pick="pickOption"
        @toggle="toggleMulti"
        @confirm-multi="confirmMulti"
        @submit="submitAnswers"
        @key="sendKey"
      />

      <MessageInput
        ref="composerRef"
        :interrupting="interrupting"
        :last-user-text="lastUserText"
        :live="isLive"
        placeholder="Message… (prefix with ! to run a shell command)"
        @send="onSend"
        @interrupt="onInterrupt"
      />
    </template>

    <div v-else class="empty"><van-loading /></div>

    <!-- action sheet -->
    <van-action-sheet
      v-model:show="actionsOpen"
      :actions="actionSheet"
      cancel-text="Cancel"
      close-on-click-action
      @select="onAction"
    />

    <!-- rename -->
    <van-dialog
      v-model:show="renameOpen"
      title="Rename session"
      show-cancel-button
      @confirm="doRename"
    >
      <van-field v-model="renameText" placeholder="New title" class="rename-field" />
    </van-dialog>

    <!-- changed files + diff (self-contained sheets) -->
    <SessionChangesSheet v-if="sid" :sid="sid" v-model:show="changesOpen" />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  background: var(--canvas);
}
.topbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: calc(env(safe-area-inset-top, 0px) + 6px) 6px 6px;
  background: var(--bg);
  border-bottom: 1px solid var(--outline-soft);
}
.icon-btn {
  flex: none;
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border: none;
  background: transparent;
  color: var(--text);
  border-radius: 10px;
}
.icon-btn:active {
  background: var(--surface-2);
}
.title-block {
  flex: 1;
  min-width: 0;
  text-align: center;
  line-height: 1.2;
}
.title-line {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.title-text {
  font-weight: 600;
  font-size: 15px;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.subline {
  font-size: 11px;
  color: var(--ink-mute);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 8px;
}
.mono {
  font-family: var(--font-mono);
}
.dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
}
.dot.live {
  background: var(--success);
  box-shadow: 0 0 0 0 rgba(74, 138, 94, 0.5);
  animation: pulse 1.8s infinite;
}
@keyframes pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(74, 138, 94, 0.45);
  }
  70% {
    box-shadow: 0 0 0 7px rgba(74, 138, 94, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(74, 138, 94, 0);
  }
}
.stream {
  flex: 1;
  min-height: 0;
}
.empty {
  flex: 1;
  display: grid;
  place-items: center;
  padding: 40px;
}
.banner {
  padding: 6px 12px;
  font-size: 12px;
  text-align: center;
  color: #fff;
}
.banner-danger {
  background: var(--van-danger-color);
}
.banner-warn {
  background: var(--van-warning-color);
}
.banner-info {
  background: var(--accent);
}
.banner-action {
  text-decoration: underline;
  font-weight: 600;
}
.rename-field {
  margin: 12px 0;
}
</style>
