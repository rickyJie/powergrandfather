<script setup lang="ts">
/**
 * LaunchParamsModal — prompts the user for a workflow's parameters
 * before launching a mission.
 *
 * Fields render according to `parameter.type`:
 *   - "string"       → text input
 *   - "int" / "float"→ number input
 *   - "bool"         → checkbox
 * Defaults from the YAML pre-fill; required params with no default
 * disable Submit until filled.
 *
 * Emits `submit(values)` with a `{name → string | number | boolean}` map
 * that the parent then hands to `launchMission()` or POST /schedules.
 */
import { computed, ref, watch } from 'vue'

type Parameter = {
  name: string
  type: string
  required: boolean
  default: any
  description: string | null
}

const props = defineProps<{
  open: boolean
  workflowName: string | null
  parameters: Parameter[]
  /** Custom submit-button label (e.g. "Schedule at 08-15 22:00"). Default: 'Launch'. */
  submitLabel?: string
  /** Optional pre-heading (e.g. mission time for scheduled launch). */
  contextLine?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'submit', values: Record<string, any>): void
}>()

const values = ref<Record<string, any>>({})

function coerceDefault(p: Parameter): any {
  const d = p.default
  if (d === undefined || d === null) {
    if (p.type === 'bool') return false
    if (p.type === 'int' || p.type === 'float') return ''
    return ''
  }
  return d
}

watch(
  () => [props.open, props.parameters] as const,
  ([isOpen, params]) => {
    if (!isOpen) return
    const seeded: Record<string, any> = {}
    for (const p of params) seeded[p.name] = coerceDefault(p)
    values.value = seeded
  },
  { immediate: true },
)

function isEmpty(v: any): boolean {
  return v === '' || v === null || v === undefined
}

const missingRequired = computed(() =>
  props.parameters
    .filter(p => p.required && isEmpty(values.value[p.name]))
    .map(p => p.name),
)

const canSubmit = computed(() => missingRequired.value.length === 0)

function onSubmit() {
  if (!canSubmit.value) return
  // Coerce number strings to numbers before emitting.
  const out: Record<string, any> = {}
  for (const p of props.parameters) {
    const v = values.value[p.name]
    if (p.type === 'int' && v !== '' && v !== null) {
      out[p.name] = typeof v === 'number' ? v : parseInt(String(v), 10)
    } else if (p.type === 'float' && v !== '' && v !== null) {
      out[p.name] = typeof v === 'number' ? v : parseFloat(String(v))
    } else if (p.type === 'bool') {
      out[p.name] = Boolean(v)
    } else {
      out[p.name] = v
    }
  }
  emit('submit', out)
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal panel lpm-modal" role="dialog" aria-modal="true" aria-label="Launch parameters">
      <div class="lpm-header">
        <div>
          <div class="lpm-eyebrow">Launch parameters</div>
          <h3 class="serif lpm-title">
            <code>{{ workflowName }}</code>
          </h3>
          <div v-if="contextLine" class="lpm-context">{{ contextLine }}</div>
        </div>
        <button class="lpm-close" @click="emit('close')" aria-label="Close">×</button>
      </div>

      <div class="lpm-body">
        <div v-if="parameters.length === 0" class="lpm-empty">
          This workflow takes no parameters — launch away.
        </div>

        <div v-else class="lpm-fields">
          <div v-for="p in parameters" :key="p.name" class="lpm-field">
            <div class="lpm-field-head">
              <span class="lpm-name">{{ p.name }}</span>
              <span v-if="p.required" class="lpm-required">required</span>
              <span class="lpm-type">{{ p.type }}</span>
            </div>
            <div v-if="p.description" class="lpm-desc">{{ p.description }}</div>

            <input
              v-if="p.type === 'string'"
              v-model="values[p.name]"
              type="text"
              :placeholder="p.default != null ? String(p.default) : ''"
            />
            <input
              v-else-if="p.type === 'int'"
              v-model="values[p.name]"
              type="number"
              step="1"
              :placeholder="p.default != null ? String(p.default) : ''"
            />
            <input
              v-else-if="p.type === 'float'"
              v-model="values[p.name]"
              type="number"
              step="0.1"
              :placeholder="p.default != null ? String(p.default) : ''"
            />
            <label v-else-if="p.type === 'bool'" class="lpm-checkbox">
              <input type="checkbox" v-model="values[p.name]" />
              <span>{{ values[p.name] ? 'true' : 'false' }}</span>
            </label>
            <input
              v-else
              v-model="values[p.name]"
              type="text"
              :placeholder="p.default != null ? String(p.default) : ''"
            />
          </div>
        </div>

        <div v-if="missingRequired.length" class="lpm-warn">
          Still missing: {{ missingRequired.join(', ') }}
        </div>

        <div class="lpm-actions">
          <button class="primary" :disabled="!canSubmit" @click="onSubmit">
            {{ submitLabel || 'Launch' }}
          </button>
          <button @click="emit('close')">Cancel</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lpm-modal {
  max-width: 560px; width: 92%; max-height: 90vh;
  padding: 0; overflow-y: auto;
}
.lpm-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--border);
}
.lpm-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute); margin-bottom: 3px;
}
.lpm-title { margin: 0; font-size: 17px; color: var(--ink); }
.lpm-context {
  font-size: 12px; color: var(--ink-mute); margin-top: 4px;
  font-family: 'Geist Mono', 'SF Mono', monospace;
}
.lpm-close {
  background: transparent; border: none; font-size: 22px; padding: 0 6px;
  color: var(--ink-mute); cursor: pointer; box-shadow: none;
}
.lpm-close:hover { color: var(--ink); transform: none; }

.lpm-body { padding: 14px 18px 18px; font-size: 13px; }

.lpm-empty {
  padding: 20px; text-align: center;
  color: var(--ink-mute); font-size: 12.5px;
}

.lpm-fields { display: flex; flex-direction: column; gap: 12px; }
.lpm-field {
  padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--canvas);
}
.lpm-field-head {
  display: flex; align-items: center; gap: 8px; margin-bottom: 4px;
}
.lpm-name {
  font-family: 'Geist Mono', 'SF Mono', monospace;
  font-size: 12.5px; font-weight: 600; color: var(--ink);
}
.lpm-required {
  font-size: 10px; padding: 1px 6px; border-radius: 3px;
  background: var(--pastel-red-bg); color: var(--pastel-red-fg);
  text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600;
}
.lpm-type {
  font-size: 10.5px; color: var(--ink-mute);
  padding: 1px 6px; border-radius: 3px;
  background: var(--border); margin-left: auto;
  font-family: 'Geist Mono', 'SF Mono', monospace;
}
.lpm-desc {
  font-size: 11.5px; color: var(--ink-mute); margin-bottom: 6px;
}
.lpm-field input[type="text"],
.lpm-field input[type="number"] {
  width: 100%; box-sizing: border-box;
  background: var(--card);
}
.lpm-checkbox {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 12.5px; color: var(--ink);
  font-family: 'Geist Mono', 'SF Mono', monospace;
}

.lpm-warn {
  margin-top: 10px; padding: 8px 10px;
  background: var(--pastel-yellow-bg);
  color: var(--pastel-yellow-fg);
  border-left: 3px solid var(--pastel-yellow-fg);
  border-radius: 4px; font-size: 12px;
}

.lpm-actions { display: flex; gap: 8px; margin-top: 14px; }

code {
  background: var(--canvas); border: 1px solid var(--border);
  padding: 0 5px; border-radius: 3px;
  font-family: 'Geist Mono', 'SF Mono', monospace; font-size: 12px;
}
</style>
