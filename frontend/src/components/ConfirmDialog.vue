<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title?: string
    message: string
    confirmText?: string
    cancelText?: string
    danger?: boolean
  }>(),
  {
    title: 'Confirm',
    confirmText: 'Confirm',
    cancelText: 'Cancel',
    danger: false,
  },
)

const emit = defineEmits<{
  (e: 'confirm'): void
  (e: 'cancel'): void
}>()

const confirmBtn = ref<HTMLButtonElement | null>(null)
const dialogEl = ref<HTMLDivElement | null>(null)

// Focus + trap: when opened, focus the confirm button. Tab cycles within the
// dialog; Esc cancels.
watch(
  () => props.open,
  async (now) => {
    if (!now) return
    await nextTick()
    confirmBtn.value?.focus()
  },
)

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('cancel')
    return
  }
  if (e.key !== 'Tab') return
  const root = dialogEl.value
  if (!root) return
  const focusable = root.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="confirm-backdrop"
      role="presentation"
      @click.self="emit('cancel')"
      @keydown="onKeydown"
    >
      <div
        ref="dialogEl"
        class="confirm-modal panel"
        role="alertdialog"
        :aria-labelledby="`confirm-title`"
        :aria-describedby="`confirm-message`"
      >
        <h3 id="confirm-title" class="serif">{{ title }}</h3>
        <p id="confirm-message" class="msg">{{ message }}</p>
        <div class="actions">
          <button class="ghost" @click="emit('cancel')">{{ cancelText }}</button>
          <button
            ref="confirmBtn"
            class="primary"
            :class="{ danger }"
            @click="emit('confirm')"
          >{{ confirmText }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.confirm-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 300;
}
.confirm-modal {
  padding: 22px 26px;
  min-width: 24rem; max-width: 32rem;
  background: var(--card);
  border-radius: 10px;
  border: 1px solid var(--border);
}
.confirm-modal h3 { margin: 0 0 10px; font-size: 16px; }
.msg {
  margin: 0 0 16px;
  font-size: 13px;
  line-height: 1.55;
  color: var(--ink);
  white-space: pre-wrap;
}
.actions { display: flex; justify-content: flex-end; gap: 8px; }
.actions button {
  padding: 6px 14px;
  font-size: 13px;
  border-radius: 4px;
  cursor: pointer;
}
.actions .ghost {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--ink);
}
.actions .ghost:hover { background: var(--canvas); }
.actions .primary {
  background: var(--ink);
  color: var(--card);
  border: 1px solid var(--ink);
}
.actions .primary.danger {
  background: var(--pastel-red-fg);
  border-color: var(--pastel-red-fg);
  color: white;
}
.actions .primary:focus-visible,
.actions .ghost:focus-visible {
  outline: 2px solid var(--pastel-blue-fg);
  outline-offset: 2px;
}
</style>
