<!--
  Small pill showing which CLI-adapter powers a session / run / agent card.
  Schema-driven (M9.3): color + icon come from the backend's metadata via
  `useBackend`, NOT from hardcoded per-adapter CSS. Adding a third adapter
  works with zero changes here.

  Props:
    - agent: string | null | undefined
        The adapter name. Null / unknown renders the "uses default" pill.
    - fallback: string — label shown when agent is null (per PM P0 feedback,
        must be a readable phrase, never blank).
    - compact: bool — icon-only when true.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useBackend } from '../composables/useBackend'

const props = withDefaults(defineProps<{
  agent?: string | null
  fallback?: string
  compact?: boolean
}>(), {
  fallback: 'uses default',
  compact: false,
})

const backend = useBackend(() => props.agent ?? null)

const displayLabel = computed(() => {
  if (!props.agent) return props.fallback
  return backend.value?.display_name ?? props.agent
})

// Prefer backend-declared glyph; fall back to first upper if the metadata
// hasn't loaded yet OR the backend didn't declare an icon.
const iconChar = computed(() => {
  if (!props.agent) return '·'
  return backend.value?.icon || props.agent[0].toUpperCase()
})

// Backend-declared accent color (hex or CSS var). When missing (unknown
// adapter / not yet loaded), fall back to the neutral ink color so the
// badge is legible.
const accentColor = computed(() =>
  backend.value?.color || 'var(--ink)',
)

const isDefault = computed(() => !props.agent)
const brandedIcon = computed(() =>
  props.agent === 'claude' || props.agent === 'codex' ? props.agent : null,
)
</script>

<template>
  <span
    class="agent-badge"
    :class="{
      'agent-badge--default': isDefault,
      'agent-badge--compact': compact,
      [`agent-badge--${brandedIcon}`]: brandedIcon,
    }"
    :style="{ '--agent-color': accentColor }"
    :title="displayLabel"
  >
    <span class="agent-badge__icon">
      <!-- Compact, single-colour marks stay crisp in the narrow session rail. -->
      <svg v-if="brandedIcon === 'claude'" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9M3 12h18" />
      </svg>
      <svg v-else-if="brandedIcon === 'codex'" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 3.1 16.1 5.5 20.2 7.9v8.2l-4.1 2.4-4.1 2.4-4.1-2.4-4.1-2.4V7.9l4.1-2.4Z" />
        <path d="m7.9 5.5 8.2 4.8v8.2M20.2 7.9 12 12.7l-8.2-4.8M12 20.9v-8.2" />
      </svg>
      <template v-else>{{ iconChar }}</template>
    </span>
    <span v-if="!compact" class="agent-badge__label">{{ displayLabel }}</span>
  </span>
</template>

<style scoped>
.agent-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--canvas);
  border: 1px solid var(--border);
  font-size: 11px;
  font-weight: 500;
  color: var(--ink-mute);
  line-height: 1.4;
  white-space: nowrap;
}

/*  Adapter accent — pulled from the CSS variable set inline via `--agent-color`.
    This is how a 3rd (or 4th, or Nth) adapter can render with the correct color
    without any CSS edit here — the backend's `color` field flows straight through.
*/
.agent-badge__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: var(--agent-color);
  color: var(--card);
  font-size: 9px;
  font-weight: 700;
  flex: 0 0 auto;
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / 18%);
}

.agent-badge__icon svg {
  width: 10px;
  height: 10px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.agent-badge--claude .agent-badge__icon {
  border-radius: 5px;
  background: #d97757;
}

.agent-badge--claude .agent-badge__icon svg { stroke-width: 2.2; }

.agent-badge--codex .agent-badge__icon {
  background: #087f75;
}

.agent-badge--codex .agent-badge__icon svg { stroke-width: 1.65; }

.agent-badge--compact {
  padding: 2px;
  border-color: color-mix(in srgb, var(--agent-color) 28%, var(--border));
  background: color-mix(in srgb, var(--agent-color) 7%, var(--canvas));
}

.agent-badge__label { letter-spacing: 0.02em; }

.agent-badge--default {
  border-style: dashed;
  font-style: italic;
}
</style>
