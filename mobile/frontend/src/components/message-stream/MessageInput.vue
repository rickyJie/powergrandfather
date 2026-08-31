<script setup lang="ts">
import { ref, computed, onBeforeUnmount } from "vue";
import { showConfirmDialog } from "vant";

interface Props {
  /** Disable send button (e.g. during in-flight POST). */
  sending?: boolean;
  /** Disable Ctrl-C button (e.g. during in-flight interrupt). */
  interrupting?: boolean;
  /** Optional placeholder override. */
  placeholder?: string;
  /** The user's most recent sent message — offered for one-tap edit-and-resend. */
  lastUserText?: string;
  /** Whether the session PTY is alive — gates the Ctrl-C button (no point
   *  offering "interrupt" on an ended/exited/crashed session). */
  live?: boolean;
}

interface Emits {
  /**
   * The parent calls `done(true)` once the message is confirmed sent (so we
   * clear the box), or `done(false)` on failure (so the draft is preserved).
   */
  (e: "send", text: string, done: (ok: boolean) => void): void;
  (e: "interrupt"): void;
}

const props = withDefaults(defineProps<Props>(), {
  sending: false,
  interrupting: false,
  placeholder: "Message... (! prefix runs bash)",
  live: true,
});
const emit = defineEmits<Emits>();

const text = ref("");
const lastCtrlC = ref(0);

const isBashHint = computed(() => text.value.trimStart().startsWith("!"));

function submit() {
  const t = text.value.trim();
  if (!t || props.sending) return;
  // The parent inserts an optimistic bubble and calls done(true) immediately so
  // the box clears and unlocks without waiting for the network round-trip. If
  // the fire-and-forget POST later fails, the parent calls restore() to put the
  // text back so nothing the user typed is lost.
  emit("send", t, (ok: boolean) => {
    if (ok) text.value = "";
  });
}

/** Re-populate the composer with a draft the parent needs to give back (a send
 *  that failed after we optimistically cleared the box). Returns false — WITHOUT
 *  overwriting — if the user has already typed a new draft, so the caller can
 *  tell the truth ("send failed") instead of falsely claiming the text was
 *  preserved. */
function restore(t: string): boolean {
  if (text.value.trim()) return false;
  text.value = t;
  return true;
}
defineExpose({ restore });

/** Pull the last sent message back into the box to tweak and resend. */
function recall() {
  if (props.lastUserText) text.value = props.lastUserText;
}

async function interrupt() {
  if (props.interrupting) return;
  // 500ms debounce against double-tap
  const now = Date.now();
  if (now - lastCtrlC.value < 500) return;
  lastCtrlC.value = now;
  try {
    await showConfirmDialog({
      title: "Send Ctrl-C?",
      message: "Interrupt the running agent turn.",
      confirmButtonText: "Interrupt",
      cancelButtonText: "Cancel",
    });
  } catch {
    return; // cancelled
  }
  emit("interrupt");
}

// Soft-keyboard offset: when visualViewport shrinks (keyboard opens),
// scroll the input into view and add bottom padding so it doesn't get
// buried by the keyboard.
const kbOffset = ref(0);
function onViewportResize() {
  if (!window.visualViewport) return;
  const diff = window.innerHeight - window.visualViewport.height;
  kbOffset.value = diff > 60 ? diff : 0;
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", onViewportResize);
  window.visualViewport.addEventListener("scroll", onViewportResize);
}
onBeforeUnmount(() => {
  if (window.visualViewport) {
    window.visualViewport.removeEventListener("resize", onViewportResize);
    window.visualViewport.removeEventListener("scroll", onViewportResize);
  }
});
</script>

<template>
  <div class="input-bar" :style="{ transform: `translateY(-${kbOffset}px)` }">
    <div v-if="isBashHint" class="hint">bash mode — sends to shell</div>
    <button
      v-else-if="lastUserText && !text.trim()"
      type="button"
      class="recall-chip"
      @click="recall"
    >
      ↑ Edit last message
    </button>
    <div class="input-row">
      <van-button
        v-if="live"
        size="small"
        type="danger"
        icon="close"
        :loading="interrupting"
        @click="interrupt"
      />
      <van-field
        v-model="text"
        type="textarea"
        rows="1"
        :autosize="{ maxHeight: 120 }"
        :placeholder="placeholder"
        class="input-field"
        @keydown.enter.exact.prevent="submit"
      />
      <van-button
        size="small"
        type="primary"
        :loading="sending"
        :disabled="!text.trim()"
        @click="submit"
      >
        Send
      </van-button>
    </div>
  </div>
</template>

<style scoped>
.input-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  padding: 8px 10px calc(10px + env(safe-area-inset-bottom, 0px));
  background: var(--bg);
  border-top: 1px solid var(--outline-soft);
  transition: transform 150ms ease;
}
.hint {
  font-size: 11px;
  color: var(--warning);
  padding: 2px 4px;
}
.recall-chip {
  margin: 0 0 4px 2px;
  padding: 3px 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-soft);
  background: var(--surface-1);
  border: 1px solid var(--outline-soft);
  border-radius: var(--radius-pill);
  cursor: pointer;
}
.recall-chip:active {
  background: var(--surface-2);
}
.input-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.input-field {
  flex: 1;
  padding: 8px 12px;
  background: var(--surface-1);
  border-radius: var(--radius-sm);
}
/* Vant only clamps the grown height when `autosize` is an OBJECT — with the
   bare boolean it sets height = scrollHeight with no bound (see
   vant/es/field/utils.mjs resizeTextarea). A pasted long message therefore
   grew this bar past the height of the phone, and since `.input-bar` is
   sticky-bottom it covered the whole transcript and pushed its own top off
   screen. ~120px is about five lines; past that the textarea scrolls. */
.input-field :deep(.van-field__control) {
  overflow-y: auto;
  /* Don't hand the overscroll to the message stream underneath once the
     composer hits its own end — on a phone that reads as the page jumping. */
  overscroll-behavior: contain;
}
</style>
