<script setup lang="ts">
/**
 * ModalShell — accessible modal wrapper (F1 from ui_review_auto_1).
 *
 * Provides:
 *   - `role="presentation"` on the backdrop (click-outside dismiss)
 *   - `role="dialog" aria-modal="true"` on the panel
 *   - `aria-labelledby="<title-slot-id>"` when a `title` prop is passed
 *   - Focus lock + Esc-to-close (opt-in via `dismissible`)
 *
 * Usage:
 *   <ModalShell
 *     :open="showX"
 *     title="New project"
 *     size="md"
 *     @close="showX = false"
 *   >
 *     <div>...body...</div>
 *   </ModalShell>
 *
 * Existing bespoke modals should have their outer `<div class="modal-backdrop">`
 * / `<div class="modal panel">` markup replaced with this component; the
 * heading (previously a plain `<h3>` inside the panel) migrates to the
 * `title` prop so `aria-labelledby` wires up automatically.
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  open: boolean
  title?: string
  size?: 'sm' | 'md' | 'lg' | 'xl'
  dismissible?: boolean         // Esc + click-outside close (default true)
  ariaLabel?: string            // fallback when no visible title
}>(), {
  size: 'md',
  dismissible: true,
})

const emit = defineEmits<{ (e: 'close'): void }>()

const panelRef = ref<HTMLElement | null>(null)
const titleId = computed(() => `modal-title-${Math.random().toString(36).slice(2, 8)}`)

function onBackdrop(ev: MouseEvent) {
  if (!props.dismissible) return
  emit('close')
  ev.stopPropagation()
}
function onKeydown(ev: KeyboardEvent) {
  if (!props.open || !props.dismissible) return
  if (ev.key === 'Escape') {
    emit('close')
  }
}

watch(() => props.open, async (isOpen) => {
  if (isOpen) {
    await nextTick()
    panelRef.value?.focus()
  }
})

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div
    v-if="open"
    class="modal-backdrop"
    role="presentation"
    @click.self="onBackdrop"
  >
    <div
      ref="panelRef"
      class="modal panel"
      :class="[`modal-${size}`]"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="title ? titleId : undefined"
      :aria-label="!title ? ariaLabel : undefined"
      tabindex="-1"
      @click.stop
    >
      <header v-if="title || $slots.header" class="modal-header">
        <slot name="header">
          <h3 :id="titleId" class="modal-title">{{ title }}</h3>
        </slot>
        <button
          v-if="dismissible"
          class="modal-close"
          aria-label="Close"
          @click="emit('close')"
        >×</button>
      </header>
      <div class="modal-body">
        <slot />
      </div>
      <footer v-if="$slots.footer" class="modal-footer">
        <slot name="footer" />
      </footer>
    </div>
  </div>
</template>

<style scoped>
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(15, 15, 20, 0.5);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
  padding: 24px;
}
.modal {
  max-height: calc(100vh - 48px);
  overflow: hidden;
  display: flex; flex-direction: column;
  border-radius: 12px;
  box-shadow: 0 24px 48px rgba(0,0,0,0.18), 0 2px 6px rgba(0,0,0,0.08);
  outline: none;
}
.modal-sm { width: min(360px, 100%); }
.modal-md { width: min(520px, 100%); }
.modal-lg { width: min(760px, 100%); }
.modal-xl { width: min(1100px, 100%); }

.modal-header {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 20px 12px;
  border-bottom: 1px solid var(--border);
}
.modal-title {
  margin: 0; font-size: 16px; font-weight: 600; color: var(--ink);
  font-family: 'Instrument Serif', 'Iowan Old Style', Georgia, serif;
  letter-spacing: 0.2px;
}
.modal-close {
  background: transparent; border: none;
  font-size: 22px; line-height: 1; padding: 4px 8px;
  color: var(--ink-mute); cursor: pointer; border-radius: 4px;
}
.modal-close:hover { background: var(--canvas); color: var(--ink); }
.modal-body { padding: 16px 20px; overflow-y: auto; flex: 1; }
.modal-footer {
  padding: 12px 20px 16px;
  border-top: 1px solid var(--border);
  display: flex; gap: 8px; justify-content: flex-end;
}
</style>
