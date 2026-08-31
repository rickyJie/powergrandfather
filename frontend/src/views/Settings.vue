<!--
  Settings page — card-based layout matching the app's Newspaper /
  paper-on-desk aesthetic (Newsreader serif headers, mono values,
  pastel state tags). Each concern is its own card floating on the
  --canvas background; within a card, the same kicker + title + body
  vertical rhythm repeats so the eye can chunk sections at a glance.
-->
<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AgentBadge from '../components/AgentBadge.vue'
import AgentSelector from '../components/AgentSelector.vue'
import BackupPanel from '../components/BackupPanel.vue'
import SyncSettings from '../components/SyncSettings.vue'
import { usePreferencesStore } from '../stores/preferences'
import { useBackendsStore } from '../stores/backends'
import {
  deleteProxyEnvFile,
  getProxyEnv,
  putProxyEnvFile,
  refreshProxyEnv,
  type ProxyEnvView,
} from '../api/proxyEnv'
import {
  getLarkSettings,
  testLarkPush,
  updateLarkSettings,
  type LarkSettingsPatch,
  type LarkSettingsView,
  type TestPushResult,
} from '../api/larkSettings'

const prefs = usePreferencesStore()
const backends = useBackendsStore()
const route = useRoute()
const router = useRouter()
const section = computed(() => {
  const q = route.query.section
  if (q === 'sync') return 'sync'
  if (q === 'lark') return 'lark'
  if (q === 'backup') return 'backup'
  return 'general'
})

function selectSection(next: 'general' | 'lark' | 'sync' | 'backup') {
  void router.replace({
    path: '/settings',
    query: next === 'general' ? {} : { ...route.query, section: next },
  })
}

// Proxy env panel state.
const proxy = ref<ProxyEnvView | null>(null)
const proxyLoading = ref(false)
const proxyError = ref<string | null>(null)
const proxyEdit = ref<Record<string, string>>({
  HTTP_PROXY: '', HTTPS_PROXY: '', ALL_PROXY: '', NO_PROXY: '',
})
const proxyStatus = ref<string | null>(null)

function _seedEditFromView(v: ProxyEnvView) {
  const seed = { HTTP_PROXY: '', HTTPS_PROXY: '', ALL_PROXY: '', NO_PROXY: '' }
  for (const [name, entry] of Object.entries(v.vars)) {
    if (name in seed && entry.source === 'file') {
      (seed as any)[name] = entry.value
    }
  }
  proxyEdit.value = seed
}

async function loadProxy() {
  proxyLoading.value = true
  proxyError.value = null
  try {
    proxy.value = await getProxyEnv()
    _seedEditFromView(proxy.value)
  } catch (e) {
    proxyError.value = apiErrorMessage(e)
  } finally {
    proxyLoading.value = false
  }
}

async function refreshProxy() {
  proxyLoading.value = true
  proxyError.value = null
  proxyStatus.value = null
  try {
    proxy.value = await refreshProxyEnv()
    _seedEditFromView(proxy.value)
    proxyStatus.value = 'Re-sniffed.'
  } catch (e) {
    proxyError.value = apiErrorMessage(e)
  } finally {
    proxyLoading.value = false
  }
}

async function saveProxyFile() {
  proxyLoading.value = true
  proxyError.value = null
  proxyStatus.value = null
  try {
    const entries: Record<string, string> = {}
    for (const [k, v] of Object.entries(proxyEdit.value)) {
      if (v !== '') entries[k] = v
    }
    proxy.value = await putProxyEnvFile(entries)
    _seedEditFromView(proxy.value)
    proxyStatus.value = 'Saved. New sessions will pick up the new values.'
  } catch (e) {
    proxyError.value = apiErrorMessage(e)
  } finally {
    proxyLoading.value = false
  }
}

async function clearProxyFile() {
  if (!confirm('Delete ~/.csm/proxy.env? The panel will revert to pure sniff.')) return
  proxyLoading.value = true
  proxyError.value = null
  proxyStatus.value = null
  try {
    proxy.value = await deleteProxyEnvFile()
    _seedEditFromView(proxy.value)
    proxyStatus.value = 'Override file deleted.'
  } catch (e) {
    proxyError.value = apiErrorMessage(e)
  } finally {
    proxyLoading.value = false
  }
}

// ---- Lark notifications card state ----
// Server state (last successful GET/PUT) vs form draft (user edits
// before Save). Test push is disabled while there are unsaved edits so
// the test result can't be misinterpreted as validating the on-screen
// form values (it actually validates whatever is in the DB).
const larkView = ref<LarkSettingsView | null>(null)
const larkDraft = ref({
  enabled: false,
  chat_id: '',
  user_id: '',
  dedup_window_sec: 60,
})
const larkLoading = ref(false)
const larkSaving = ref(false)
const larkTesting = ref(false)
const larkError = ref<string | null>(null)
const larkStatus = ref<string | null>(null)
const larkTestResult = ref<TestPushResult | null>(null)

function _seedLarkDraftFromView(v: LarkSettingsView) {
  larkDraft.value = {
    enabled: v.enabled,
    chat_id: v.chat_id ?? '',
    user_id: v.user_id ?? '',
    dedup_window_sec: v.dedup_window_sec,
  }
}

const larkDirty = computed(() => {
  const v = larkView.value
  if (!v) return false
  const d = larkDraft.value
  return (
    v.enabled !== d.enabled ||
    (v.chat_id ?? '') !== d.chat_id ||
    (v.user_id ?? '') !== d.user_id ||
    v.dedup_window_sec !== d.dedup_window_sec
  )
})

// Live "wrong prefix" hints for the two id fields. Non-blocking — the
// backend already rejects malformed ids, this just surfaces the mistake
// before Save so the user doesn't hit a cryptic lark-cli error.
const larkChatIdWrongPrefix = computed(() => {
  const v = larkDraft.value.chat_id.trim()
  return v.length > 0 && !v.startsWith('oc_')
})
const larkUserIdWrongPrefix = computed(() => {
  const v = larkDraft.value.user_id.trim()
  return v.length > 0 && !v.startsWith('ou_')
})

async function loadLark() {
  larkLoading.value = true
  larkError.value = null
  try {
    larkView.value = await getLarkSettings()
    _seedLarkDraftFromView(larkView.value)
  } catch (e) {
    larkError.value = apiErrorMessage(e)
  } finally {
    larkLoading.value = false
  }
}

async function saveLark() {
  larkSaving.value = true
  larkError.value = null
  larkStatus.value = null
  larkTestResult.value = null
  try {
    const patch: LarkSettingsPatch = {
      enabled: larkDraft.value.enabled,
      chat_id: larkDraft.value.chat_id,  // "" clears
      user_id: larkDraft.value.user_id,
      dedup_window_sec: larkDraft.value.dedup_window_sec,
    }
    larkView.value = await updateLarkSettings(patch)
    _seedLarkDraftFromView(larkView.value)
    larkStatus.value = 'Saved.'
  } catch (e) {
    larkError.value = apiErrorMessage(e)
  } finally {
    larkSaving.value = false
  }
}

async function testLark() {
  larkTesting.value = true
  larkError.value = null
  larkTestResult.value = null
  try {
    larkTestResult.value = await testLarkPush()
  } catch (e) {
    larkError.value = apiErrorMessage(e)
  } finally {
    larkTesting.value = false
  }
}

onMounted(async () => {
  await Promise.all([
    prefs.ensureLoaded(),
    backends.ensureLoaded(),
    loadProxy(),
    loadLark(),
  ])
})

async function saveDefault(name: string | null) {
  if (!name) return
  await prefs.update({ default_agent: name })
}

async function saveSupervisor(name: string | null) {
  await prefs.update({ supervisor_agent: name })
}

// Default session prompt — local edit buffer so the textarea doesn't
// PUT on every keystroke. `dirty` gates the Save button so the user
// gets a clear "unsaved" affordance instead of silent throttled writes.
const promptDraft = ref<string>('')
const promptEnabledDraft = ref<boolean>(false)
const noteDraft = ref<string>('')
const noteEnabledDraft = ref<boolean>(false)
const promptDirty = computed(() => {
  const server = prefs.prefs?.default_session_prompt ?? ''
  const serverOn = prefs.prefs?.default_session_prompt_enabled ?? false
  const noteServer = prefs.prefs?.default_session_prompt_note ?? ''
  const noteServerOn = prefs.prefs?.default_session_prompt_note_enabled ?? false
  return (
    promptDraft.value !== server
    || promptEnabledDraft.value !== serverOn
    || noteDraft.value !== noteServer
    || noteEnabledDraft.value !== noteServerOn
  )
})
watch(
  () => [
    prefs.prefs?.default_session_prompt,
    prefs.prefs?.default_session_prompt_enabled,
    prefs.prefs?.default_session_prompt_note,
    prefs.prefs?.default_session_prompt_note_enabled,
  ] as const,
  ([txt, en, note, noteEn], old) => {
    // Adopt the incoming server value only when the local drafts still match
    // the PREVIOUSLY-seen server value — i.e. the user hasn't made a local
    // edit. This is what makes the initial async load work: the drafts start
    // '' / false and the previous server value is undefined (→ '' / false),
    // so the first real payload is adopted instead of being mistaken for an
    // in-progress edit. Once the user types, the drafts diverge from the old
    // server value and a background refresh no longer clobbers them.
    const [oldTxt, oldEn, oldNote, oldNoteEn] = old ?? [undefined, undefined, undefined, undefined]
    const draftsMatchOld =
      promptDraft.value === (oldTxt ?? '')
      && promptEnabledDraft.value === !!oldEn
      && noteDraft.value === (oldNote ?? '')
      && noteEnabledDraft.value === !!oldNoteEn
    if (draftsMatchOld) {
      promptDraft.value = txt ?? ''
      promptEnabledDraft.value = !!en
      noteDraft.value = note ?? ''
      noteEnabledDraft.value = !!noteEn
    }
  },
  { immediate: true },
)
async function saveSessionPrompt() {
  await prefs.update({
    default_session_prompt: promptDraft.value.trim() || null,
    default_session_prompt_enabled: promptEnabledDraft.value,
    default_session_prompt_note: noteDraft.value.trim() || null,
    default_session_prompt_note_enabled: noteEnabledDraft.value,
  })
}
const UNICODE_MATH_PRESET = (
  'When writing maths, prefer Unicode symbols (∑ ∫ ∂ α β γ Σ √ π ∞ · ≤ ≥ ≠ ≈ x² x_i and so on) '
  + 'over LaTeX ($...$ / $$...$$). '
  + 'Fall back to LaTeX source only where Unicode cannot express it — matrices, nested fractions, complex alignment.'
)
function applyUnicodePreset() {
  promptDraft.value = UNICODE_MATH_PRESET
  promptEnabledDraft.value = true
}
const HINT_NOTE_PRESET = (
  '(Note: the above is a session-level default hint — background setup, not a task for this turn. '
  + 'No reply needed to this message; respond when I send my next actual instruction.)'
)
function applyHintNotePreset() {
  noteDraft.value = HINT_NOTE_PRESET
  noteEnabledDraft.value = true
}
</script>

<template>
  <div class="settings-page">
    <div class="settings-inner" :class="{ 'sync-inner': section === 'sync' }">
    <header class="page-header">
      <div class="kicker">Preferences</div>
      <h1>Settings</h1>
      <p class="page-sub">Local, single-user configuration for this CSM install.</p>
    </header>

    <nav class="settings-tabs" aria-label="Settings sections">
      <button :class="{ active: section === 'general' }" @click="selectSection('general')">
        General
      </button>
      <button :class="{ active: section === 'lark' }" @click="selectSection('lark')">
        Lark
      </button>
      <button :class="{ active: section === 'sync' }" @click="selectSection('sync')">
        Sync
      </button>
      <button :class="{ active: section === 'backup' }" @click="selectSection('backup')">
        Backup
      </button>
    </nav>

    <div v-if="section === 'general'">
    <!-- Agent defaults -->
    <section class="card">
      <div class="card-head">
        <div class="kicker">Agent</div>
        <h2>Default agent</h2>
      </div>
      <p class="card-desc">
        Which CLI-adapter to use when creating a new session, running a
        workflow, or opening an Agent Deck card that hasn't pinned its own.
        Overridable per-session from the "New session" dialog.
      </p>
      <div class="card-row">
        <AgentBadge :agent="prefs.prefs?.default_agent" />
        <AgentSelector
          :model-value="prefs.prefs?.default_agent ?? null"
          @update:modelValue="saveDefault"
        />
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <div class="kicker">Agent</div>
        <h2>Supervisor agent</h2>
      </div>
      <p class="card-desc">
        Runs post-mission review of AUTO sessions. Leave blank to follow
        the default; pin a cheaper model to bound review costs.
      </p>
      <div class="card-row">
        <AgentBadge :agent="prefs.prefs?.supervisor_agent" fallback="(follows default)" />
        <AgentSelector
          :model-value="prefs.prefs?.supervisor_agent ?? null"
          :allow-null="true"
          null-label="Follow default"
          @update:modelValue="saveSupervisor"
        />
      </div>
    </section>

    <!-- Default session prompt — auto-sent as the first user input
         on every new INTERACTIVE session when enabled. Ignored for
         AUTO / CHAT_AGENT sessions (workflow / AgentDefinition owns
         their prompts). Cheapest way to nudge every session with
         global preferences (e.g., "prefer Unicode math over LaTeX",
         "answer in Chinese", "keep responses terse"). -->
    <section class="card">
      <div class="card-head">
        <div class="kicker">Session</div>
        <h2>Default session prompt</h2>
      </div>
      <p class="card-desc">
        When enabled, CSM sends this text as the first user input to every
        new interactive session (~3s after spawn, once the CLI is at its
        prompt). Ignored for automation and agent-deck sessions — those
        keep their own workflow / agent-definition prompts.
      </p>
      <div class="card-row" style="flex-direction: column; align-items: stretch; gap: 12px;">
        <label class="pref-toggle">
          <input type="checkbox" v-model="promptEnabledDraft">
          <span>Auto-send on new interactive session</span>
        </label>
        <textarea
          v-model="promptDraft"
          class="pref-textarea"
          rows="5"
          placeholder="e.g. Prefer Unicode math symbols over LaTeX. Answer in Chinese. Keep responses under 200 words unless asked."
        />

        <!-- Supplementary note — appended after the prompt above at delivery
             time when enabled. Tags the auto-sent prompt as informational so
             the model doesn't treat it as a real task and burn a turn. -->
        <div class="pref-subsection">
          <label class="pref-toggle">
            <input type="checkbox" v-model="noteEnabledDraft">
            <span>Append supplementary note</span>
          </label>
          <p class="pref-hint">
            When on, this note is appended after the prompt above (blank line
            between) and sent together — e.g. tell the agent the prompt is just
            a hint and needs no reply. Only applies while the prompt itself is
            enabled and non-empty.
          </p>
          <textarea
            v-model="noteDraft"
            class="pref-textarea"
            rows="3"
            :disabled="!noteEnabledDraft"
            placeholder="e.g. The above is a session-level default hint, background only — no reply needed to this message."
          />
        </div>

        <div class="pref-actions">
          <button type="button" class="btn-secondary" @click="applyUnicodePreset">
            + Unicode math preset
          </button>
          <button type="button" class="btn-secondary" @click="applyHintNotePreset">
            + Hint note preset
          </button>
          <button
            type="button"
            class="btn-primary"
            :disabled="!promptDirty"
            @click="saveSessionPrompt"
          >{{ promptDirty ? 'Save' : 'Saved' }}</button>
        </div>
      </div>
    </section>

    <!-- Registered agents -->
    <section class="card">
      <div class="card-head">
        <div class="kicker">Runtime</div>
        <h2>Registered agents</h2>
      </div>
      <p class="card-desc">
        Every adapter compiled into this build.
        <code>enabled</code> reflects the
        <code>CSM_ENABLE_&lt;name&gt;</code> env flag;
        <code>installed</code> / <code>authenticated</code> reflect a
        live probe of the CLI binary.
      </p>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th></th>
              <th>Name</th>
              <th>Display</th>
              <th>Enabled</th>
              <th>Installed</th>
              <th>Auth</th>
              <th>Version</th>
              <th>Flags</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in backends.items" :key="b.name">
              <td>
                <span class="color-chip" :style="{ background: b.color || 'var(--ink)' }">
                  {{ b.icon || b.name[0].toUpperCase() }}
                </span>
              </td>
              <td><code>{{ b.name }}</code></td>
              <td>{{ b.display_name }}</td>
              <td>
                <span class="state-tag" :class="b.enabled ? 'ok' : 'off'">
                  {{ b.enabled ? 'yes' : 'no' }}
                </span>
              </td>
              <td>
                <span class="state-tag" :class="b.status.installed ? 'ok' : 'off'">
                  {{ b.status.installed ? 'yes' : 'no' }}
                </span>
              </td>
              <td>
                <span class="state-tag" :class="b.status.authenticated ? 'ok' : 'off'">
                  {{ b.status.authenticated ? 'yes' : 'no' }}
                </span>
              </td>
              <td><code>{{ b.status.version || '—' }}</code></td>
              <td>{{ b.flags_schema?.length ?? 0 }}</td>
              <td class="err-cell">{{ b.status.error || '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Proxy env -->
    <section class="card">
      <div class="card-head with-action">
        <div>
          <div class="kicker">Network</div>
          <h2>Proxy env</h2>
        </div>
        <button class="btn btn-ghost" @click="refreshProxy" :disabled="proxyLoading">
          Re-sniff
        </button>
      </div>
      <p class="card-desc">
        Proxy vars (<code>HTTP_PROXY</code> / <code>HTTPS_PROXY</code> /
        <code>ALL_PROXY</code> / <code>NO_PROXY</code>) are sniffed from
        <code>$SHELL -ic 'export -p'</code> at CSM startup and layered
        into every spawned session's environment. The override file at
        <code>{{ proxy?.env_file_path ?? '~/.csm/proxy.env' }}</code>
        wins over sniffed values.
      </p>

      <p v-if="proxyError" class="banner banner-err">{{ proxyError }}</p>

      <div v-if="proxy" class="meta-grid">
        <div class="meta-item">
          <div class="meta-label">Auto-sniff</div>
          <div class="meta-value">
            <span class="state-tag" :class="proxy.sniff_enabled ? 'ok' : 'off'">
              {{ proxy.sniff_enabled ? 'on' : 'off' }}
            </span>
            <code v-if="proxy.sniff_shell" class="meta-code">{{ proxy.sniff_shell }}</code>
          </div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Override file</div>
          <div class="meta-value">
            <code class="meta-code">{{ proxy.env_file_path || '—' }}</code>
            <span v-if="proxy.env_file_path" class="state-tag" :class="proxy.env_file_exists ? 'ok' : 'off'">
              {{ proxy.env_file_exists ? 'present' : 'not present' }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="proxy && Object.keys(proxy.vars).length" class="table-wrap">
        <table class="data-table">
          <thead>
            <tr><th>Variable</th><th>Value</th><th>Source</th></tr>
          </thead>
          <tbody>
            <tr v-for="(v, name) in proxy.vars" :key="name">
              <td><code>{{ name }}</code></td>
              <td><code>{{ v.value }}</code></td>
              <td>
                <span class="src-pill" :class="'src-' + v.source">{{ v.source }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="proxy && !proxyLoading" class="empty-note">
        No proxy vars detected. Fill in the override form below or drop
        <code>~/.csm/proxy.env</code> to add some.
      </p>

      <ul v-if="proxy?.warnings?.length" class="warnings">
        <li v-for="(w, i) in proxy.warnings" :key="i">{{ w }}</li>
      </ul>

      <div v-if="proxy" class="subcard">
        <div class="subcard-head">
          <div class="kicker">Override</div>
          <h3>Override file</h3>
        </div>
        <p class="card-desc small">
          Values written here become the source of truth for that
          variable. Leave a field empty to defer to the sniff.
        </p>
        <div class="form-grid">
          <template v-for="name in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY']" :key="name">
            <label :for="`in-${name}`" class="form-label">{{ name }}</label>
            <input
              :id="`in-${name}`"
              v-model="proxyEdit[name]"
              type="text"
              class="form-input"
              :placeholder="proxy.vars[name]?.source === 'sniff' ? `sniffed: ${proxy.vars[name].value}` : ''"
              :disabled="proxyLoading"
            />
          </template>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" @click="saveProxyFile" :disabled="proxyLoading">
            Save
          </button>
          <button
            class="btn btn-outline"
            @click="clearProxyFile"
            :disabled="proxyLoading || !proxy.env_file_exists"
          >
            Delete file
          </button>
          <span v-if="proxyStatus" class="form-status">{{ proxyStatus }}</span>
        </div>
      </div>
    </section>
    </div>

    <div v-else-if="section === 'lark'">
    <!-- Lark notifications — promoted to top-level nav tab alongside
         Sync / Backup so notification config has its own space instead
         of being a card buried at the bottom of General. -->
    <section class="card">
      <div class="card-head">
        <div class="kicker">Notifications</div>
        <h2>Lark push (outbound)</h2>
      </div>
      <p class="card-desc">
        Pushes critical notifications (session crashes, automation
        failures, token warnings, port conflicts) to a Lark chat via
        <code>lark-cli</code>, always as the <strong>bot identity</strong>
        (hardcoded — user identity is intentionally not used). Requires
        <code>lark-cli</code> on <code>PATH</code> with a configured app
        (see setup guide below). Changes here take effect immediately —
        no restart needed.
      </p>

      <p v-if="larkError" class="banner banner-err">{{ larkError }}</p>

      <div v-if="larkView" class="meta-grid">
        <div class="meta-item">
          <div class="meta-label">lark-cli</div>
          <div class="meta-value">
            <span class="state-tag" :class="larkView.cli_installed ? 'ok' : 'off'">
              {{ larkView.cli_installed ? 'installed' : 'not found' }}
            </span>
          </div>
        </div>
        <div class="meta-item">
          <div class="meta-label">Push state</div>
          <div class="meta-value">
            <span class="state-tag" :class="larkView.enabled ? 'ok' : 'off'">
              {{ larkView.enabled ? 'enabled' : 'disabled' }}
            </span>
          </div>
        </div>
      </div>

      <!-- First-run onboarding: shows a hint when lark-cli isn't found,
           plus an always-available "how to set up" details block so
           users who never touched lark-cli have a concrete path. -->
      <p v-if="larkView && !larkView.cli_installed" class="banner banner-warn">
        <strong>lark-cli not on PATH.</strong> Install it and run
        <code>lark-cli config init</code>, then reload this page. See the
        setup guide below.
      </p>

      <details class="lark-setup" :open="!!larkView && !larkView.cli_installed">
        <summary>How to set up (first-time users)</summary>
        <ol class="lark-setup-steps">
          <li>
            <strong>Install <code>lark-cli</code></strong> —
            <code>npm i -g @larksuiteoapi/lark-cli</code>
            (Node ≥ 18). Verify with
            <code>lark-cli --version</code>.
          </li>
          <li>
            <strong>Configure an app (once)</strong> —
            <code>lark-cli config init</code>. Bot identity is automatic
            from the configured appId + appSecret; there is no
            <code>auth login</code> for bot. Confirm with
            <code>lark-cli auth status --json</code> — you want
            <code>identities.bot.status = "ready"</code>. Grant
            <code>im:message</code> to the app in the Lark developer
            console if pushes later fail with a scope error.
          </li>
          <li>
            <strong>Pick a target</strong> — one of:
            <ul class="lark-setup-sub">
              <li>
                <strong>Group chat</strong> →
                <code>lark-cli im +chat-search --query "your group name"</code>
                gives an <code>oc_...</code>. Then <strong>add the bot as a group member</strong>,
                otherwise the push fails with <code>chat_id not found</code>.
              </li>
              <li>
                <strong>DM to a user</strong> →
                <code>lark-cli contact +user-info --email you@example.com</code>
                gives an <code>ou_...</code>. The bot must have been messaged
                by that user at least once for the p2p channel to exist.
              </li>
            </ul>
            Paste into the <strong>matching</strong> field below —
            <code>oc_</code> goes into <em>Chat ID</em>,
            <code>ou_</code> goes into <em>User ID</em>. Do not mix
            them up.
          </li>
          <li>
            <strong>Save + Test push</strong>. If the test message lands
            in your Lark, you're done — real notifications will follow
            the same path.
          </li>
        </ol>
      </details>

      <div class="subcard">
        <div class="subcard-head">
          <div class="kicker">Config</div>
          <h3>Push target</h3>
        </div>
        <p class="card-desc small">
          Fill <strong>exactly one</strong> — <em>Chat ID</em>
          (<code>oc_…</code>, a group or p2p conversation) or
          <em>User ID</em> (<code>ou_…</code>, a user's open_id for
          direct DM). If both are set, <code>chat_id</code> wins and
          <code>user_id</code> is ignored. Empty a field to clear it.
        </p>

        <label class="lark-switch">
          <input
            type="checkbox"
            v-model="larkDraft.enabled"
            :disabled="larkLoading"
          />
          <span>Enable Lark push</span>
        </label>

        <div class="form-grid">
          <label for="in-lark-chat-id" class="form-label">
            Chat ID
            <span class="form-hint">must start with <code>oc_</code></span>
          </label>
          <div class="form-field-col">
            <input
              id="in-lark-chat-id"
              v-model="larkDraft.chat_id"
              type="text"
              class="form-input"
              :class="{ 'input-warn': larkChatIdWrongPrefix }"
              placeholder="oc_xxxxxxxxxxxxx  (group or p2p conversation id)"
              :disabled="larkLoading"
            />
            <p v-if="larkChatIdWrongPrefix" class="field-warn">
              Looks like a user open_id (<code>ou_</code>). Move it to
              <strong>User ID</strong> below, or clear this field.
            </p>
          </div>
          <label for="in-lark-user-id" class="form-label">
            User ID
            <span class="form-hint">must start with <code>ou_</code></span>
          </label>
          <div class="form-field-col">
            <input
              id="in-lark-user-id"
              v-model="larkDraft.user_id"
              type="text"
              class="form-input"
              :class="{ 'input-warn': larkUserIdWrongPrefix }"
              placeholder="ou_xxxxxxxxxxxxx  (user open_id — bot DMs this user)"
              :disabled="larkLoading"
            />
            <p v-if="larkUserIdWrongPrefix" class="field-warn">
              Looks like a chat id (<code>oc_</code>). Move it to
              <strong>Chat ID</strong> above, or clear this field.
            </p>
          </div>
          <label for="in-lark-dedup" class="form-label">Dedup window</label>
          <input
            id="in-lark-dedup"
            v-model.number="larkDraft.dedup_window_sec"
            type="number"
            min="1"
            max="86400"
            class="form-input lark-num"
            :disabled="larkLoading"
          />
        </div>

        <div class="form-actions">
          <button
            class="btn btn-primary"
            @click="saveLark"
            :disabled="larkSaving || larkLoading || !larkDirty"
          >
            {{ larkSaving ? 'Saving…' : 'Save' }}
          </button>
          <button
            class="btn btn-outline"
            @click="testLark"
            :disabled="larkTesting || larkLoading || larkDirty || !larkView?.enabled"
            :title="larkDirty ? 'Save your changes first' : 'Send a real test message'"
          >
            <span v-if="larkTesting" class="spinner" aria-hidden="true" />
            {{ larkTesting ? 'Testing…' : 'Test push' }}
          </button>
          <span v-if="larkStatus" class="form-status">{{ larkStatus }}</span>
          <span
            v-if="larkTestResult"
            class="form-status"
            :class="{ 'form-status-err': !larkTestResult.sent }"
          >
            <template v-if="larkTestResult.sent">
              ✓ sent ({{ larkTestResult.duration_ms }} ms)
            </template>
            <template v-else-if="larkTestResult.error">
              ✗ {{ larkTestResult.error }}
            </template>
            <template v-else>
              ✗ sink skipped (check enabled + target)
            </template>
          </span>
        </div>

        <p class="card-desc small lark-advanced-hint">
          Advanced fields (<code>dnd_hours</code>, <code>tz</code>,
          per-type toggles) can be tuned via
          <code>PUT /api/settings/lark</code> until a v2 UI lands.
        </p>
      </div>
    </section>
    </div>

    <SyncSettings v-else-if="section === 'sync'" embedded />
    <BackupPanel v-else-if="section === 'backup'" />
    </div>
  </div>
</template>

<style scoped>
/* -------- page shell -------- */
.settings-page {
  color: var(--ink);
  /* .canvas is overflow:hidden; give the page its own scroll container that
     spans the full canvas width so the scrollbar sits at the viewport's right
     edge (not at the content's max-width edge). */
  height: 100%;
  overflow-y: auto;
  box-sizing: border-box;
}
.settings-inner {
  max-width: 880px;
  padding: 32px 40px 48px;
  box-sizing: border-box;
}
.settings-inner.sync-inner { max-width: 1200px; }
.page-header { margin-bottom: 28px; }
.page-header h1 {
  font-family: 'Newsreader', serif;
  font-weight: 500;
  font-size: 32px;
  margin: 2px 0 6px;
  letter-spacing: -0.01em;
}
.page-sub { color: var(--ink-mute); font-size: 13px; margin: 0; }
.settings-tabs {
  display: flex;
  gap: 4px;
  margin: -10px 0 22px;
  border-bottom: 1px solid var(--border);
}
.settings-tabs button {
  padding: 8px 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--ink-mute);
  cursor: pointer;
  font: inherit;
}
.settings-tabs button.active {
  border-bottom-color: var(--accent);
  color: var(--ink);
  font-weight: 600;
}
.kicker {
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  font-weight: 600;
  color: var(--ink-faint);
}

/* -------- card -------- */
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 22px 26px 24px;
  margin-bottom: 18px;
  box-shadow: var(--shadow-faint);
}
.card-head { margin-bottom: 10px; }
.card-head.with-action {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
}
.card-head h2 {
  font-family: 'Newsreader', serif;
  font-weight: 500;
  font-size: 20px;
  margin: 2px 0 0;
  letter-spacing: -0.005em;
}
.card-desc {
  color: var(--ink-2);
  font-size: 13px;
  line-height: 1.55;
  margin: 0 0 16px;
  max-width: 640px;
}
.card-desc.small { font-size: 12px; margin-bottom: 12px; }
.card-desc code { font-family: 'Geist Mono', monospace; font-size: 0.92em; }
.card-row { display: flex; align-items: center; gap: 14px; }

/* Default-session-prompt widgets — textarea + toggle + action row. */
.pref-toggle {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--ink);
  cursor: pointer;
  user-select: none;
}
.pref-toggle input[type=checkbox] { width: 16px; height: 16px; cursor: pointer; }
.pref-textarea {
  width: 100%; min-height: 90px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card); color: var(--ink);
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 13px; line-height: 1.5;
  resize: vertical;
  box-sizing: border-box;
}
.pref-textarea:focus { outline: 2px solid var(--accent); outline-offset: -2px; }
.pref-textarea:disabled { opacity: 0.55; cursor: not-allowed; }
.pref-subsection {
  display: flex; flex-direction: column; gap: 8px;
  padding-top: 12px; margin-top: 4px;
  border-top: 1px dashed var(--border);
}
.pref-hint {
  margin: 0;
  font-size: 12px; line-height: 1.5;
  color: var(--ink-mute);
}
.pref-actions {
  display: flex; align-items: center; justify-content: flex-end; gap: 10px;
}
.pref-actions .btn-secondary {
  padding: 6px 12px; font-size: 12px;
  border: 1px solid var(--border); border-radius: 6px;
  background: transparent; color: var(--ink-mute);
  cursor: pointer;
  transition: background 120ms, color 120ms, border-color 120ms;
}
.pref-actions .btn-secondary:hover { color: var(--ink); border-color: var(--ink-mute); }
.pref-actions .btn-primary {
  padding: 6px 14px; font-size: 12px; font-weight: 500;
  border: 0; border-radius: 6px;
  background: var(--accent); color: var(--card);
  cursor: pointer;
  transition: opacity 120ms;
}
.pref-actions .btn-primary:disabled {
  opacity: 0.45; cursor: default;
}

/* -------- meta grid (key/value pairs) -------- */
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px 24px;
  padding: 12px 14px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 14px;
}
.meta-label {
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin-bottom: 3px;
  font-weight: 600;
}
.meta-value { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.meta-code {
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  color: var(--ink-2);
  word-break: break-all;
}

/* -------- state / source pills -------- */
.state-tag {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
  font-family: 'Geist Mono', monospace;
}
.state-tag.ok  { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }
.state-tag.off { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }

.src-pill {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  font-family: 'Geist Mono', monospace;
}
.src-sniff { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.src-file  { background: var(--pastel-green-bg); color: var(--pastel-green-fg); }

/* -------- tables -------- */
.table-wrap {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 4px;
}
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th, .data-table td {
  padding: 9px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.data-table thead th {
  background: var(--canvas);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
}
.data-table tbody tr:last-child td { border-bottom: none; }
.data-table tbody tr:hover td { background: var(--canvas); }
.data-table code {
  font-family: 'Geist Mono', monospace;
  font-size: 11.5px;
  color: var(--ink);
}
.err-cell {
  color: var(--pastel-red-fg);
  font-size: 11.5px;
  max-width: 260px;
}

.color-chip {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px;
  border-radius: 50%;
  color: var(--card);
  font-size: 10px;
  font-weight: 700;
}

/* -------- banners / warnings / empty -------- */
.banner {
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  margin-bottom: 12px;
}
.banner-err { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.banner-warn {
  background: var(--pastel-yellow-bg);
  color: var(--pastel-yellow-fg);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  margin-bottom: 12px;
}
.banner-warn code { font-family: 'Geist Mono', monospace; font-size: 0.92em; }

/* Lark setup guide — collapsible <details> */
.lark-setup {
  margin: 4px 0 14px;
  padding: 8px 14px 12px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 12.5px;
}
.lark-setup > summary {
  cursor: pointer;
  padding: 4px 0;
  font-weight: 600;
  color: var(--ink);
  user-select: none;
}
.lark-setup > summary:hover { color: var(--accent); }
.lark-setup-steps {
  margin: 8px 0 0;
  padding-left: 22px;
  color: var(--ink-2);
  line-height: 1.7;
}
.lark-setup-steps li { margin-bottom: 6px; }
.lark-setup-steps code {
  font-family: 'Geist Mono', monospace;
  font-size: 0.92em;
  background: var(--card);
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
.lark-setup-sub {
  margin: 4px 0 6px 0;
  padding-left: 20px;
  list-style: disc;
}
.lark-setup-sub li { margin: 4px 0; }

/* Format prefix hint next to Chat ID / User ID labels */
.form-hint {
  display: block;
  font-weight: 400;
  font-size: 11px;
  color: var(--ink-mute);
  margin-top: 2px;
}
.form-hint code {
  font-family: 'Geist Mono', monospace;
  font-size: 0.9em;
  background: var(--canvas);
  padding: 0 4px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
/* form-field-col wraps the input + inline warning so the warning
   appears directly under its field inside the two-column form-grid. */
.form-field-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.form-input.input-warn {
  border-color: var(--pastel-yellow-fg, #b58105);
  background: var(--pastel-yellow-bg, #fff8e1);
}
.field-warn {
  margin: 0;
  padding: 4px 8px;
  font-size: 11.5px;
  color: var(--pastel-yellow-fg, #855c00);
  background: var(--pastel-yellow-bg, #fff8e1);
  border: 1px solid var(--pastel-yellow-fg, #e0b849);
  border-radius: 4px;
  line-height: 1.4;
}
.field-warn code {
  font-family: 'Geist Mono', monospace;
  font-size: 0.95em;
  background: rgba(0,0,0,0.05);
  padding: 0 3px;
  border-radius: 3px;
}
.warnings {
  margin: 12px 0 0;
  padding: 8px 12px 8px 26px;
  background: var(--pastel-yellow-bg);
  color: var(--pastel-yellow-fg);
  border-radius: 6px;
  font-size: 12px;
}
.warnings li { margin: 2px 0; }
.empty-note {
  padding: 12px 14px;
  background: var(--canvas);
  border: 1px dashed var(--border);
  border-radius: 8px;
  color: var(--ink-mute);
  font-size: 12px;
  margin: 0 0 12px;
}

/* -------- subcard (override editor inside proxy card) -------- */
.subcard {
  margin-top: 18px;
  padding: 16px 18px 18px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.subcard-head { margin-bottom: 6px; }
.subcard-head h3 {
  font-family: 'Newsreader', serif;
  font-weight: 500;
  font-size: 15px;
  margin: 2px 0 0;
}

/* -------- form -------- */
.form-grid {
  display: grid;
  grid-template-columns: 130px 1fr;
  gap: 10px 14px;
  align-items: center;
  margin-bottom: 16px;
}
.form-label {
  font-family: 'Geist Mono', monospace;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--ink-mute);
  letter-spacing: 0.02em;
}
.form-input {
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--card);
  color: var(--ink);
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  transition: border-color 120ms var(--ease-soft), box-shadow 120ms var(--ease-soft);
}
.form-input::placeholder { color: var(--ink-faint); }
.form-input:focus {
  outline: none;
  border-color: var(--ink-mute);
  box-shadow: 0 0 0 3px rgba(0,0,0,0.05);
}
.form-input:disabled { opacity: 0.6; cursor: not-allowed; }

.form-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.form-status { font-size: 12px; color: var(--ink-mute); }
.form-status-err { color: var(--pastel-red-fg); }

/* Lark card: switch + inline spinner + narrow number input */
.lark-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ink);
  margin-bottom: 14px;
  cursor: pointer;
  user-select: none;
}
.lark-switch input { margin: 0; }
.form-input.lark-num { max-width: 120px; }
.lark-advanced-hint {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--border);
  color: var(--ink-faint);
}

.spinner {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 6px;
  border: 2px solid var(--border);
  border-top-color: var(--ink);
  border-radius: 50%;
  animation: spinner-rot 0.7s linear infinite;
  vertical-align: -1px;
}
@keyframes spinner-rot { to { transform: rotate(360deg); } }

/* -------- buttons -------- */
.btn {
  padding: 6px 14px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--ink);
  transition: background 120ms var(--ease-soft), border-color 120ms var(--ease-soft);
}
.btn:hover:not(:disabled) { background: var(--canvas); }
.btn:disabled { opacity: 0.5; cursor: default; }

.btn-primary {
  background: var(--ink);
  color: var(--card);
  border-color: var(--ink);
  font-weight: 600;
}
.btn-primary:hover:not(:disabled) { background: var(--ink-2); border-color: var(--ink-2); }

.btn-outline {
  background: transparent;
  color: var(--ink);
  border-color: var(--border-strong);
}

.btn-ghost {
  background: transparent;
  border-color: var(--border);
  color: var(--ink-2);
  font-size: 11.5px;
  padding: 4px 10px;
}
</style>
