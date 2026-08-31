<script setup lang="ts">
import type { ChoiceStep, ChoiceOption } from "@/lib/pendingChoice";

// Interactive-choice panel mirroring claude's AskUserQuestion / plan-approval
// TUI. The parent runs the state machine and hands us one screen at a time:
//   phase "answer"     → render `step` (single-select buttons, or multi-select
//                        checkboxes + a Next button); shows "N / M" progress.
//   phase "submit"     → the final review screen; one Submit button closes a
//                        multi-question prompt (its trailing "✔ Submit" tab).
//   phase "submitting" → answers are in flight, waiting for the tool to clear.
// The "manual" arrow row stays available as an escape hatch if the picker ever
// drifts from what we render (hint line: Enter select · Tab/Arrow nav · Esc).
const UP_KEY = "\u001b[A";
const DOWN_KEY = "\u001b[B";
const ENTER_KEY = "\r";
const ESC_KEY = "\u001b";

defineProps<{
  step: ChoiceStep | null;
  phase: "answer" | "submit" | "submitting";
  progress?: string;
  selected?: number[];
  disabled?: boolean;
}>();

const emit = defineEmits<{
  (e: "pick", opt: ChoiceOption): void;
  (e: "toggle", index: number): void;
  (e: "confirm-multi"): void;
  (e: "submit"): void;
  (e: "key", keys: string): void;
}>();
</script>

<template>
  <div class="choice">
    <template v-if="phase === 'answer' && step">
      <div class="choice-q">
        <span v-if="progress" class="choice-progress">{{ progress }}</span>
        {{ step.question }}
        <span v-if="step.multiSelect" class="choice-tag">multi-select</span>
      </div>
      <!-- single-select: tap picks the row AND advances -->
      <template v-if="!step.multiSelect">
        <button
          v-for="(opt, i) in step.options"
          :key="i"
          class="choice-opt"
          :disabled="disabled"
          @click="emit('pick', opt)"
        >
          <span class="opt-idx">{{ i + 1 }}</span>
          <span class="opt-body">
            <span class="opt-label">{{ opt.label }}</span>
            <span v-if="opt.desc" class="opt-desc">{{ opt.desc }}</span>
          </span>
        </button>
      </template>
      <!-- multi-select: tap toggles; Next sends the whole selection at once -->
      <template v-else>
        <button
          v-for="(opt, i) in step.options"
          :key="i"
          class="choice-opt"
          :class="{ 'opt-checked': selected?.includes(i) }"
          :disabled="disabled"
          @click="emit('toggle', i)"
        >
          <span class="opt-check">{{ selected?.includes(i) ? "☑" : "☐" }}</span>
          <span class="opt-body">
            <span class="opt-label">{{ opt.label }}</span>
            <span v-if="opt.desc" class="opt-desc">{{ opt.desc }}</span>
          </span>
        </button>
        <button
          class="choice-confirm"
          :disabled="disabled"
          @click="emit('confirm-multi')"
        >
          Next
        </button>
      </template>
    </template>
    <template v-else-if="phase === 'submit'">
      <div class="choice-q">All questions answered — claude resumes once you submit</div>
      <button class="choice-confirm" :disabled="disabled" @click="emit('submit')">
        Submit answers
      </button>
    </template>
    <div v-else class="choice-q choice-submitting">Submitting your answers…</div>
    <div class="choice-manual">
      <span class="manual-hint">manual:</span>
      <button @click="emit('key', UP_KEY)">↑</button>
      <button @click="emit('key', DOWN_KEY)">↓</button>
      <button @click="emit('key', ENTER_KEY)">⏎</button>
      <button @click="emit('key', ESC_KEY)">esc</button>
    </div>
  </div>
</template>

<style scoped>
.choice {
  border-top: 1px solid var(--outline-soft);
  background: var(--surface);
  padding: 10px 12px;
  max-height: 46vh;
  overflow-y: auto;
}
.choice-q {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 8px;
}
.choice-progress {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
}
.choice-submitting {
  color: var(--text-soft);
  font-weight: 500;
}
.choice-tag {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--surface-2);
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 600;
}
.opt-check {
  flex: none;
  width: 20px;
  text-align: center;
  font-size: 16px;
  color: var(--primary);
}
.choice-opt.opt-checked {
  border-color: var(--primary-soft);
  background: var(--primary-container);
}
.choice-confirm {
  width: 100%;
  padding: 11px 12px;
  margin-top: 4px;
  border: none;
  border-radius: var(--radius-sm);
  background: var(--primary);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
}
.choice-confirm:active {
  opacity: 0.85;
}
.choice-confirm:disabled {
  opacity: 0.5;
}
.choice-opt {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  text-align: left;
  padding: 10px 12px;
  margin-bottom: 6px;
  border: 1px solid var(--outline);
  border-radius: var(--radius-sm);
  background: var(--surface-1);
  color: var(--text);
}
.choice-opt:active {
  background: var(--primary-container);
  border-color: var(--primary-soft);
}
.choice-opt:disabled {
  opacity: 0.5;
}
.opt-idx {
  flex: none;
  width: 20px;
  height: 20px;
  border-radius: 6px;
  background: var(--primary);
  color: #fff;
  font-size: 12px;
  line-height: 20px;
  text-align: center;
}
.opt-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.opt-label {
  font-size: 14px;
  font-weight: 600;
}
.opt-desc {
  font-size: 12px;
  color: var(--text-soft);
}
.choice-manual {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.manual-hint {
  font-size: 11px;
  color: var(--text-faint);
}
.choice-manual button {
  min-width: 36px;
  height: 30px;
  border: 1px solid var(--outline);
  border-radius: var(--radius-xs);
  background: var(--surface-1);
  color: var(--text-soft);
  font-size: 13px;
}
.choice-manual button:active {
  background: var(--surface-2);
}
</style>
