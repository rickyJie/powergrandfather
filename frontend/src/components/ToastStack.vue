<script setup lang="ts">
/**
 * Renders the toast queue in a fixed bottom-right stack. Mount once at the
 * app root.
 */
import { useToast } from '../composables/useToast'

const { toasts, dismiss } = useToast()
</script>

<template>
  <div class="toast-stack" role="status" aria-live="polite" aria-atomic="false">
    <transition-group name="toast">
      <div
        v-for="t in toasts"
        :key="t.id"
        class="toast"
        :class="`toast-${t.kind}`"
        :role="t.kind === 'error' ? 'alert' : undefined"
        @click="dismiss(t.id)"
      >
        <span class="toast-icon">
          {{ t.kind === 'success' ? '✓' : t.kind === 'error' ? '✕' : t.kind === 'warn' ? '⚠' : 'ℹ' }}
        </span>
        <span class="toast-msg">{{ t.msg }}</span>
        <button class="toast-x" @click.stop="dismiss(t.id)" title="Dismiss">×</button>
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.toast-stack {
  /* Bottom offset leaves room for the FeedbackButton (44px circle at
     bottom-right + 20px margin = 64px reserved). */
  position: fixed; bottom: 80px; right: 20px; z-index: 9999;
  display: flex; flex-direction: column; gap: 8px;
  max-width: 380px;
}
.toast {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 12px;
  background: var(--card);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.08);
  font-size: 13px; line-height: 1.4;
  cursor: pointer;
}
.toast-icon {
  font-weight: 700; font-size: 14px;
  width: 20px; height: 20px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; flex-shrink: 0;
}
.toast-msg { flex: 1; color: var(--ink); word-break: break-word; }
.toast-x {
  background: transparent; border: none; cursor: pointer;
  color: var(--ink-faint); font-size: 16px; padding: 0 4px;
  line-height: 1;
}
.toast-x:hover { color: var(--ink); }

.toast-info    { border-left: 3px solid var(--pastel-blue-fg); }
.toast-info    .toast-icon { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.toast-success { border-left: 3px solid var(--pastel-green-fg); }
.toast-success .toast-icon { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.toast-warn    { border-left: 3px solid var(--pastel-yellow-fg); }
.toast-warn    .toast-icon { background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg); }
.toast-error   { border-left: 3px solid var(--pastel-red-fg); }
.toast-error   .toast-icon { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }

.toast-enter-active, .toast-leave-active {
  transition: opacity 160ms ease, transform 160ms ease;
}
.toast-enter-from { opacity: 0; transform: translateX(20px); }
.toast-leave-to   { opacity: 0; transform: translateX(20px); }
</style>
