<script setup lang="ts">
/**
 * Popover — reusable hover tooltip with multi-line + rich content support.
 *
 * Solves the problem that native `title=` collapses newlines and can't
 * render any HTML. Wrap any trigger element; the slot is the tooltip body.
 *
 * Usage:
 *   <Popover :text="'line 1\nline 2'">
 *     <span>hover me</span>
 *   </Popover>
 *
 * Or use the `body` slot for rich content.
 */
import { ref } from 'vue'

defineProps<{
  text?: string
  delay?: number  // ms before showing on hover (default 300)
}>()

const open = ref(false)
let timer: number | null = null

function onEnter(delay: number = 300) {
  if (timer) window.clearTimeout(timer)
  timer = window.setTimeout(() => { open.value = true; timer = null }, delay)
}
function onLeave() {
  if (timer) { window.clearTimeout(timer); timer = null }
  open.value = false
}
</script>

<template>
  <span
    class="pop-trigger"
    @mouseenter="onEnter(delay)"
    @mouseleave="onLeave"
    @focusin="onEnter(delay)"
    @focusout="onLeave"
  >
    <slot />
    <transition name="pop">
      <span v-if="open" class="pop-body" role="tooltip">
        <slot name="body">
          <span v-if="text" style="white-space: pre-line;">{{ text }}</span>
        </slot>
      </span>
    </transition>
  </span>
</template>

<style scoped>
.pop-trigger {
  position: relative;
  display: inline-flex;
}
.pop-body {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--ink);
  color: var(--card);
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  line-height: 1.45;
  font-family: 'Geist', sans-serif;
  max-width: 320px;
  z-index: 10000;
  pointer-events: none;
  box-shadow: 0 4px 12px rgba(0,0,0,0.18);
}
.pop-body::after {
  content: '';
  position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
  border: 4px solid transparent; border-top-color: var(--ink);
}
.pop-enter-active, .pop-leave-active { transition: opacity 120ms, transform 120ms; }
.pop-enter-from { opacity: 0; transform: translateX(-50%) translateY(2px); }
.pop-leave-to { opacity: 0; }
</style>
