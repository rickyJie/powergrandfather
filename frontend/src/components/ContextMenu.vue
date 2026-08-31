<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch, nextTick } from 'vue'

export interface ContextMenuItem {
  label: string
  action?: () => void
  disabled?: boolean
  danger?: boolean          // red foreground for destructive actions
  divider?: boolean         // renders as a horizontal separator; other fields ignored
  icon?: string             // optional leading glyph
}

const props = defineProps<{
  visible: boolean
  x: number                  // client-space anchor (right-click event page coords)
  y: number
  items: ContextMenuItem[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const menuRef = ref<HTMLElement | null>(null)
// Adjusted position after we measure the menu and know whether it would
// overflow the viewport. Falls back to the raw anchor until we've had
// one paint cycle to measure.
const adjusted = ref({ x: 0, y: 0 })

// Measure the rendered menu and flip its origin if it would overflow.
// Right-clicks near the bottom-right corner of the viewport (very common
// for the last row in a scrollable list) would otherwise render off-screen.
async function repositionAfterPaint() {
  if (!props.visible) return
  await nextTick()
  const el = menuRef.value
  if (!el) return
  const { innerWidth: vw, innerHeight: vh } = window
  const rect = el.getBoundingClientRect()
  let nx = props.x
  let ny = props.y
  if (nx + rect.width > vw - 4) nx = Math.max(4, vw - rect.width - 4)
  if (ny + rect.height > vh - 4) ny = Math.max(4, vh - rect.height - 4)
  adjusted.value = { x: nx, y: ny }
  const first = el.querySelector<HTMLButtonElement>('.ctx-item:not(:disabled)')
  first?.focus()
}

watch(() => [props.visible, props.x, props.y] as const, ([v]) => {
  if (v) {
    adjusted.value = { x: props.x, y: props.y }
    repositionAfterPaint()
  }
})

function onOutsideClick(ev: MouseEvent) {
  if (!props.visible) return
  if (menuRef.value && menuRef.value.contains(ev.target as Node)) return
  emit('close')
}

function onKey(ev: KeyboardEvent) {
  if (!props.visible) return
  if (ev.key === 'Escape') {
    ev.preventDefault()
    emit('close')
    return
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) return
  const buttons = Array.from(
    menuRef.value?.querySelectorAll<HTMLButtonElement>('.ctx-item:not(:disabled)') || [],
  )
  if (!buttons.length) return
  ev.preventDefault()
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const next =
    ev.key === 'Home' ? 0
    : ev.key === 'End' ? buttons.length - 1
    : ev.key === 'ArrowDown' ? (current + 1 + buttons.length) % buttons.length
    : (current - 1 + buttons.length) % buttons.length
  buttons[next]?.focus()
}

function onItem(item: ContextMenuItem) {
  if (item.disabled || item.divider) return
  item.action?.()
  emit('close')
}

function onViewportChange() {
  emit('close')
}

onMounted(() => {
  window.addEventListener('mousedown', onOutsideClick, true)
  window.addEventListener('contextmenu', onOutsideClick, true)
  window.addEventListener('keydown', onKey)
  window.addEventListener('resize', onViewportChange)
  window.addEventListener('scroll', onViewportChange, true)
})
onUnmounted(() => {
  window.removeEventListener('mousedown', onOutsideClick, true)
  window.removeEventListener('contextmenu', onOutsideClick, true)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('resize', onViewportChange)
  window.removeEventListener('scroll', onViewportChange, true)
})

const style = computed(() => ({
  left: adjusted.value.x + 'px',
  top: adjusted.value.y + 'px',
}))
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      ref="menuRef"
      class="ctx-menu"
      :style="style"
      role="menu"
      @click.stop
      @contextmenu.prevent
    >
      <template v-for="(item, i) in items" :key="i">
        <div v-if="item.divider" class="ctx-divider"></div>
        <button
          v-else
          class="ctx-item"
          :class="{ danger: item.danger, disabled: item.disabled }"
          :disabled="item.disabled"
          role="menuitem"
          @click="onItem(item)"
        >
          <span v-if="item.icon" class="ctx-icon">{{ item.icon }}</span>
          <span class="ctx-label">{{ item.label }}</span>
        </button>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.ctx-menu {
  position: fixed;
  min-width: 180px;
  max-width: 280px;
  padding: 4px 0;
  background: var(--card, #fff);
  border: 1px solid var(--border, #ddd);
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
  z-index: 10000;
  font-size: 13px;
  user-select: none;
}
.ctx-item {
  display: flex; align-items: center; gap: 8px;
  width: 100%;
  padding: 6px 12px;
  background: transparent;
  border: none;
  text-align: left;
  color: var(--ink, #222);
  cursor: pointer;
  font-family: inherit;
  font-size: inherit;
}
.ctx-item:hover:not(:disabled) { background: var(--canvas, #f5f5f5); }
.ctx-item:disabled,
.ctx-item.disabled {
  color: var(--ink-faint, #aaa);
  cursor: not-allowed;
}
.ctx-item.danger { color: var(--pastel-red-fg, #b85450); }
.ctx-item.danger:hover:not(:disabled) { background: var(--pastel-red-bg, #FCE9E7); }
.ctx-icon { width: 14px; display: inline-flex; justify-content: center; flex-shrink: 0; }
.ctx-label { flex: 1; }
.ctx-divider {
  height: 1px;
  margin: 4px 0;
  background: var(--border, #ddd);
}
</style>
