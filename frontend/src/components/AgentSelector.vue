<!--
  Dropdown for picking a CLI adapter (used by the "New session" dialog and
  Settings page). Filters to `status.usable` by default so unusable
  adapters don't appear as valid choices.

  Emits `update:modelValue` with the selected adapter name.
-->
<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useBackendsStore } from '../stores/backends'

const props = withDefaults(defineProps<{
  modelValue: string | null
  usableOnly?: boolean
  allowNull?: boolean            // when true, includes "(use default)" option
  nullLabel?: string
}>(), {
  usableOnly: true,
  allowNull: false,
  nullLabel: '(use default)',
})

const emit = defineEmits<{ (e: 'update:modelValue', v: string | null): void }>()

const store = useBackendsStore()
onMounted(() => { store.ensureLoaded() })

// M9.2: `usable` is now a Pinia getter (computed), so reactive updates
// propagate cleanly and we don't allocate a new array per render.
const options = computed(() =>
  props.usableOnly ? store.usable : store.items,
)

function onChange(e: Event) {
  const v = (e.target as HTMLSelectElement).value
  emit('update:modelValue', v === '' ? null : v)
}
</script>

<template>
  <span class="agent-selector-wrap">
    <select
      class="agent-selector"
      :value="modelValue ?? ''"
      @change="onChange"
      :disabled="store.loading && !store.loaded"
    >
      <option v-if="allowNull" value="">{{ nullLabel }}</option>
      <option
        v-for="opt in options"
        :key="opt.name"
        :value="opt.name"
      >{{ opt.display_name }} ({{ opt.name }})</option>
      <option
        v-if="options.length === 0"
        value=""
        disabled
      >{{ store.loading ? '— loading… —' : store.error ? '— failed to load —' : '— no agents available —' }}</option>
    </select>
    <!-- A transient load failure leaves the list empty; give the user an
         immediate way to retry instead of a dead "no agents available". -->
    <button
      v-if="store.error && options.length === 0"
      type="button"
      class="agent-retry"
      :disabled="store.loading"
      :title="store.error"
      @click="store.refresh()"
    >{{ store.loading ? '…' : '⟳ Retry' }}</button>
  </span>
</template>

<style scoped>
.agent-selector {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--canvas);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
}
.agent-selector:disabled { opacity: 0.6; cursor: not-allowed; }
.agent-selector-wrap { display: inline-flex; align-items: center; gap: 6px; }
.agent-retry {
  padding: 5px 9px;
  border: 1px solid var(--pastel-red-fg, #b3261e);
  border-radius: 6px;
  background: var(--canvas);
  color: var(--pastel-red-fg, #b3261e);
  font-size: 12px; font-family: inherit; cursor: pointer;
}
.agent-retry:hover:not(:disabled) { background: var(--pastel-red-bg, #fbe9e7); }
.agent-retry:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
