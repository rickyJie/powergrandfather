<!--
  Generic per-adapter flags UI (M9.4).

  Reads `backend.flags_schema` from the resolved Backend and renders each
  descriptor by its `kind` discriminator. Mutates the passed-in `argv`
  string via v-model. Contains ZERO adapter-name branching — a new
  adapter renders correctly as soon as its `flags_schema()` is defined
  backend-side.

  Supported kinds:
    - checkbox   → binary flag; toggles presence in argv
    - select     → flag + value pair; empty value removes both
    - resume     → same UX as select but choices come from live sessions
    - info       → static explanatory text; no interaction

  Contract for callers:
    - v-model:argv is a string (the whole command line: "claude -x -y").
    - v-model:argv emits on every mutation.
    - `agent` is the resolved adapter name (from useEffectiveAgent).
    - If agent is null OR the backend metadata isn't loaded yet OR the
      backend declares an empty flags_schema, this component renders
      nothing (v-if guard).
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useBackend } from '../composables/useBackend'
import type { FlagDescriptor } from '../api/backends'
import type { SessionRow } from '../api/sessions'

const props = defineProps<{
  agent: string | null
  argv: string
  // Optional: candidate rows for `resume` kind. When absent, the resume
  // dropdown shows "(no resumable sessions loaded)".
  resumableSessions?: SessionRow[]
  // Optional: for `resume` kind — session id → argv value mapping. When
  // absent we default to session.claude_session_id (claude's convention);
  // future adapters that need a different id source can override.
  resumeIdOf?: (s: SessionRow) => string
  // Optional: display label for a resumable session in the dropdown.
  resumeLabelOf?: (s: SessionRow) => string
}>()

const emit = defineEmits<{
  (e: 'update:argv', v: string): void
}>()

const backend = useBackend(() => props.agent)
const schema = computed<FlagDescriptor[]>(() => backend.value?.flags_schema ?? [])
const hasSchema = computed(() => schema.value.length > 0)

// -----------------------------------------------------------------------
// argv helpers — small pure functions, unit-testable independent of Vue.
// -----------------------------------------------------------------------

function _parts(s: string): string[] {
  return s.trim() ? s.trim().split(/\s+/) : []
}
function _join(parts: string[]): string { return parts.join(' ') }

function hasFlag(flag: string): boolean {
  return _parts(props.argv).includes(flag)
}

function toggleFlag(flag: string) {
  const p = _parts(props.argv)
  const i = p.indexOf(flag)
  if (i >= 0) p.splice(i, 1)
  else p.push(flag)
  emit('update:argv', _join(p))
}

function getFlagArg(flag: string): string {
  const p = _parts(props.argv)
  const i = p.indexOf(flag)
  return i >= 0 && i + 1 < p.length ? p[i + 1] : ''
}

function setFlagArg(flag: string, value: string) {
  const p = _parts(props.argv)
  const i = p.indexOf(flag)
  if (i >= 0) p.splice(i, 2)
  if (value) p.push(flag, value)
  emit('update:argv', _join(p))
}

// -----------------------------------------------------------------------
// Resume helpers — kept small; the panel is the natural boundary for
// "given a resumable session, produce (value, label)".
// -----------------------------------------------------------------------

const resumables = computed<SessionRow[]>(() =>
  (props.resumableSessions ?? []).filter(s =>
    // Only rows that have SOMETHING that can be resumed. Adapters differ
    // on which field carries the id — the caller-supplied `resumeIdOf`
    // resolves it; default assumes claude_session_id.
    props.resumeIdOf ? !!props.resumeIdOf(s) : !!s.claude_session_id,
  ),
)

function resumeValue(s: SessionRow): string {
  if (props.resumeIdOf) return props.resumeIdOf(s)
  return s.claude_session_id || ''
}
function resumeLabel(s: SessionRow): string {
  if (props.resumeLabelOf) return props.resumeLabelOf(s)
  const shortCwd = s.cwd.split('/').slice(-2).join('/')
  return `${s.title || s.id.slice(0, 8)} · ${shortCwd}`
}
</script>

<template>
  <div v-if="hasSchema" class="afp">
    <div class="afp-flags">
      <template v-for="f in schema" :key="(f as any).name ?? 'info'">
        <!-- info block -->
        <div v-if="f.kind === 'info'" class="afp-info">{{ f.text }}</div>

        <!-- checkbox -->
        <label v-else-if="f.kind === 'checkbox'" class="afp-flag" :title="f.hint ?? undefined">
          <input
            type="checkbox"
            :checked="hasFlag(f.argv_flag)"
            @change="toggleFlag(f.argv_flag)"
          />
          <span>{{ f.label }}</span>
        </label>

        <!-- select -->
        <label v-else-if="f.kind === 'select'" class="afp-flag" :title="f.hint ?? undefined">
          <span class="afp-name">{{ f.label }}</span>
          <select
            :value="getFlagArg(f.argv_flag)"
            @change="setFlagArg(f.argv_flag, ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="c in f.choices" :key="c.value" :value="c.value">{{ c.label }}</option>
          </select>
        </label>

        <!-- resume (special: live sessions list) -->
        <label v-else-if="f.kind === 'resume'" class="afp-flag" :title="f.hint ?? undefined">
          <span class="afp-name">{{ f.label }}</span>
          <select
            :value="getFlagArg(f.argv_flag)"
            @change="setFlagArg(f.argv_flag, ($event.target as HTMLSelectElement).value)"
          >
            <option value="">(none)</option>
            <option
              v-for="s in resumables"
              :key="s.id"
              :value="resumeValue(s)"
            >{{ resumeLabel(s) }}</option>
          </select>
        </label>
      </template>
    </div>
  </div>
</template>

<style scoped>
.afp { display: flex; flex-direction: column; gap: 6px; }
.afp-flags {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.afp-flag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ink);
  cursor: pointer;
}
.afp-flag input[type="checkbox"] {
  margin: 0;
  accent-color: var(--ink);
}
.afp-flag select {
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--canvas);
  color: var(--ink);
  font: inherit;
}
.afp-name {
  color: var(--ink-mute);
  font-weight: 500;
}
.afp-info {
  flex-basis: 100%;
  padding: 8px 12px;
  background: var(--canvas);
  border-left: 3px solid var(--ink-mute);
  border-radius: 3px;
  color: var(--ink-mute);
  font-size: 12px;
  line-height: 1.5;
}
</style>
