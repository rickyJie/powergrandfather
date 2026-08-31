<!--
  First-run wizard modal — lets the user pick a default CLI adapter on
  their first visit. Renders full-screen when preferences.is_first_run
  is true; otherwise nothing.

  Per PM P0 finding, includes a "Continue read-only" escape hatch so a
  user with zero usable agents can still tour the UI. Also renders
  usable adapters as pickable cards and unusable ones as greyed-out
  cards with a hint on how to fix them.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { usePreferencesStore } from '../stores/preferences'
import { useBackendsStore } from '../stores/backends'

const prefs = usePreferencesStore()
const backends = useBackendsStore()

const selected = ref<string | null>(null)
const saving = ref(false)
const skipToReadOnly = ref(false)

onMounted(async () => {
  await Promise.all([prefs.ensureLoaded(), backends.ensureLoaded()])
})

const visible = computed(() => {
  // Only pop when the pref explicitly says so AND we haven't opted for
  // read-only bypass. Never show while stores are still loading.
  if (!prefs.loaded) return false
  if (!prefs.prefs?.is_first_run) return false
  if (skipToReadOnly.value) return false
  return true
})

// M9.2: pinia store now exposes `usable` as a getter — no method call.
const usable = computed(() => backends.usable)
const anyUsable = computed(() => backends.hasAnyUsable)

async function confirm() {
  if (!selected.value) return
  saving.value = true
  try {
    await prefs.update({
      default_agent: selected.value,
      has_completed_first_run: true,
    })
    // ensure the singleton flag propagates to the pref-driven watchers
    await prefs.refresh()
  } catch (e) {
    console.error('first-run wizard save failed', e)
  } finally {
    saving.value = false
  }
}

function continueReadOnly() {
  // Escape hatch — PM P0. Doesn't set has_completed_first_run, so the
  // wizard will re-appear on next visit (the user can still work in
  // read-only mode this session).
  skipToReadOnly.value = true
}
</script>

<template>
  <div v-if="visible" class="fr-wizard" role="dialog" aria-labelledby="fr-title">
    <div class="fr-card">
      <h1 id="fr-title">Choose your default agent</h1>
      <p class="fr-lede">
        PowerGrandFather runs Claude Code and other agent CLIs side-by-side.
        Pick one to be your default; you can override it per-session later
        and switch the default from Settings.
      </p>

      <div v-if="!backends.loaded" class="fr-loading">Loading agents…</div>

      <div v-else-if="!anyUsable" class="fr-empty">
        <p><strong>No agents are usable right now.</strong></p>
        <ul>
          <li v-for="b in backends.items" :key="b.name">
            <strong>{{ b.display_name }}</strong>
            —
            <span v-if="!b.status.installed">not installed</span>
            <span v-else-if="!b.status.authenticated">installed but not authenticated</span>
            <span v-else>{{ b.status.error || 'unavailable' }}</span>
          </li>
        </ul>
        <p class="fr-hint">
          Install / authenticate one of the CLIs above, then reload this page.
          Or continue in read-only mode to explore the UI first.
        </p>
      </div>

      <div v-else class="fr-grid">
        <!--
          Cards render backend-declared accent (b.color) inline so a new
          adapter's card gets its own accent without frontend CSS changes.
          Schema-driven per M9 refactor.
        -->
        <button
          v-for="b in backends.items"
          :key="b.name"
          class="fr-choice"
          :class="{
            'fr-choice--selected': selected === b.name,
            'fr-choice--disabled': !b.status.usable,
          }"
          :style="{ '--card-accent': b.color || 'var(--ink)' }"
          :disabled="!b.status.usable"
          @click="b.status.usable && (selected = b.name)"
        >
          <div class="fr-choice-title">
            <span class="fr-choice-icon">{{ b.icon || b.name[0].toUpperCase() }}</span>
            {{ b.display_name }}
            <span v-if="b.status.version" class="fr-version">{{ b.status.version }}</span>
          </div>
          <div class="fr-choice-detail">
            <span v-if="b.status.usable" class="fr-ok">READY</span>
            <span v-else class="fr-bad">
              {{ !b.status.installed ? 'not installed' :
                 !b.status.authenticated ? 'not authenticated' :
                 (b.status.error || 'unavailable') }}
            </span>
          </div>
        </button>
      </div>

      <div class="fr-actions">
        <button
          type="button"
          class="fr-secondary"
          @click="continueReadOnly"
          title="Skip for now — you'll see the wizard again next visit"
        >Continue read-only</button>
        <button
          type="button"
          class="fr-primary"
          :disabled="!selected || saving || !anyUsable"
          @click="confirm"
        >{{ saving ? 'Saving…' : 'Use this agent' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fr-wizard {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
}

.fr-card {
  background: var(--card);
  border-radius: 12px;
  padding: 32px 36px;
  max-width: 640px;
  width: calc(100% - 32px);
  color: var(--ink);
  box-shadow: var(--shadow-lg);
  max-height: 90vh;
  overflow-y: auto;
}

.fr-card h1 {
  margin: 0 0 8px;
  font-family: 'Newsreader', serif;
  font-weight: 500;
  font-size: 24px;
}

.fr-lede {
  color: var(--ink-mute);
  margin: 0 0 20px;
  line-height: 1.5;
}

.fr-loading, .fr-empty {
  padding: 24px 0;
  color: var(--ink-mute);
}

.fr-empty ul {
  margin: 8px 0 12px 20px;
  padding: 0;
  line-height: 1.7;
}
.fr-empty .fr-hint {
  color: var(--ink-mute);
  font-size: 13px;
  margin-top: 8px;
}

.fr-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin: 8px 0 20px;
}

.fr-choice {
  padding: 14px 16px;
  border: 2px solid var(--border);
  border-left: 4px solid var(--card-accent, var(--ink));
  border-radius: 10px;
  background: var(--canvas);
  cursor: pointer;
  text-align: left;
  transition: border-color 120ms, background 120ms;
  font: inherit;
  color: var(--ink);
}
.fr-choice:not(:disabled):hover { border-color: var(--card-accent, var(--ink)); }
.fr-choice--selected { border-color: var(--card-accent, var(--ink)); background: var(--card); }
.fr-choice--disabled { opacity: 0.5; cursor: not-allowed; }

.fr-choice-title {
  font-weight: 600;
  font-size: 15px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.fr-choice-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px;
  border-radius: 50%;
  background: var(--card-accent, var(--ink));
  color: var(--card);
  font-size: 12px;
  font-weight: 700;
}
.fr-version {
  font-family: 'Geist Mono', monospace;
  font-size: 11px;
  color: var(--ink-mute);
  font-weight: 400;
}
.fr-choice-detail { margin-top: 6px; font-size: 12px; }
.fr-ok { color: #059669; font-weight: 600; letter-spacing: 0.03em; }
.fr-bad { color: #dc2626; }

.fr-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 16px;
}

.fr-primary, .fr-secondary {
  padding: 8px 20px;
  border-radius: 6px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--ink);
  font: inherit;
}
.fr-primary {
  background: var(--ink);
  color: var(--card);
  border-color: var(--ink);
}
.fr-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.fr-secondary:hover { background: var(--canvas); }
</style>
