<script setup lang="ts">
import { apiErrorMessage } from '../../lib/apiError'
import { onMounted, reactive, ref } from 'vue'
import {
  agentAlertsApi,
  type AgentAlertRule,
  type GenerateResponse,
  type PresetDef,
} from '../../api/agent_alerts'
import { tokensApi } from '../../api/tokens'

const rules = ref<AgentAlertRule[]>([])
const presets = ref<PresetDef[]>([])
const loading = ref(false)
const loadErr = ref('')

// User's actual 5h window snapshot — used to show "your current value" hints
// alongside preset param inputs so a new user knows if the default threshold
// is realistic for their usage.
const currentWindow = ref<Record<string, any> | null>(null)

// Per-preset param overrides + enabling spinner state + expand toggle.
const presetParams = reactive<Record<string, Record<string, number>>>({})
const presetBusy = reactive<Record<string, boolean>>({})
const presetErr = reactive<Record<string, string>>({})
const presetOpen = reactive<Record<string, boolean>>({})

function toggleOpen(presetId: string) {
  presetOpen[presetId] = !presetOpen[presetId]
}

// Compact summary of current param values (shown when card is collapsed).
function presetSummary(preset: PresetDef): string {
  const p = presetParams[preset.id] || {}
  const v = (k: string) => p[k] ?? preset.params.find(x => x.key === k)?.default ?? 0
  const fmtNum = (n: number) => n.toLocaleString()
  switch (preset.id) {
    case 'msg_count_warn':
      return `alert at ${fmtNum(v('threshold'))} messages`
    case 'total_tokens_warn':
      return `alert above ${fmtNum(v('tokens_million'))}M tokens`
    case 'session_burn':
      return `one session ≥${v('share_pct')}% of the window and >${fmtNum(v('tokens_million'))}M`
    case 'cache_hit_drop':
      return `hit rate <${v('ratio_pct')}% with >${fmtNum(v('tokens_million'))}M total`
    default:
      // Fallback: join all params
      return preset.params.map(ps => `${ps.label} ${v(ps.key)}${ps.unit}`).join(' · ')
  }
}

// Custom-rule authoring flow state (simplified — advanced fields collapsed).
type Stage = 'form' | 'generating' | 'preview' | 'saving'
const showCustomModal = ref(false)
const stage = ref<Stage>('form')
const formErr = ref('')
const showAdvanced = ref(false)

const fName = ref('')
const fDesc = ref('')
const fPollSec = ref(60)
const fCooldownSec = ref(300)
const fChannels = ref<{ inapp: boolean; lark: boolean }>({ inapp: true, lark: false })
const fEscalate = ref(false)
const fLarkChat = ref('')
const fLarkUser = ref('')

const genResult = ref<GenerateResponse | null>(null)

// ---- Edit modal for existing preset-based rules ----
const editRule = ref<AgentAlertRule | null>(null)
const editParams = reactive<Record<string, number>>({})
const editChannels = reactive<{ inapp: boolean; lark: boolean }>({ inapp: true, lark: false })
const editEscalate = ref(false)
const editLarkChat = ref('')
const editLarkUser = ref('')
const editPollSec = ref(60)
const editCooldownSec = ref(300)
const editBusy = ref(false)
const editErr = ref('')

// Custom-rule edit mode reuses the authoring modal. `editingCustomRuleId`
// non-null indicates we're editing (vs creating) — saveEdit then calls
// PATCH instead of POST.
const editingCustomRuleId = ref<string | null>(null)

function openCustomEdit(r: AgentAlertRule) {
  editingCustomRuleId.value = r.id
  resetCustomForm()
  fName.value = r.name
  fDesc.value = r.nl_description
  fPollSec.value = r.poll_interval_sec
  fCooldownSec.value = r.cooldown_sec
  fChannels.value.inapp = (r.channels || []).includes('inapp')
  fChannels.value.lark = (r.channels || []).includes('lark')
  fEscalate.value = r.escalate
  fLarkChat.value = r.lark_chat_id || ''
  fLarkUser.value = r.lark_user_id || ''
  showCustomModal.value = true
}

function openEdit(r: AgentAlertRule) {
  const presetId = r.rule_metadata?.preset_id
  if (!presetId) {
    // Custom rule — reuse the authoring modal in edit mode.
    openCustomEdit(r)
    return
  }
  const preset = presets.value.find(p => p.id === presetId)
  if (!preset) {
    alert(`Preset ${presetId} no longer exists`)
    return
  }
  editRule.value = r
  editErr.value = ''
  // Clear stale keys from previous edits before seeding new ones.
  for (const k of Object.keys(editParams)) delete editParams[k]
  const saved = (r.rule_metadata?.preset_params || {}) as Record<string, number>
  for (const ps of preset.params) {
    editParams[ps.key] = saved[ps.key] ?? ps.default
  }
  // Seed channel + escalate + lark from current rule state.
  editChannels.inapp = (r.channels || []).includes('inapp')
  editChannels.lark = (r.channels || []).includes('lark')
  editEscalate.value = r.escalate
  editLarkChat.value = r.lark_chat_id || ''
  editLarkUser.value = r.lark_user_id || ''
  editPollSec.value = r.poll_interval_sec
  editCooldownSec.value = r.cooldown_sec
}

function closeEdit() {
  editRule.value = null
  editErr.value = ''
}

async function saveEdit() {
  if (!editRule.value) return
  editBusy.value = true
  editErr.value = ''
  try {
    if (!editChannels.inapp && !editChannels.lark) {
      throw new Error('Pick at least one notification channel')
    }
    const channels: string[] = []
    if (editChannels.inapp) channels.push('inapp')
    if (editChannels.lark) channels.push('lark')

    // 1) Rebuild the check script + threshold_spec from the preset with new params.
    await agentAlertsApi.updateFromPreset(editRule.value.id, { ...editParams })

    // 2) Patch operational fields (channels / escalate / lark ids / poll / cooldown).
    await agentAlertsApi.patch(editRule.value.id, {
      channels,
      escalate: editEscalate.value,
      lark_chat_id: editChannels.lark ? (editLarkChat.value.trim() || undefined) : undefined,
      lark_user_id: editChannels.lark ? (editLarkUser.value.trim() || undefined) : undefined,
      poll_interval_sec: editPollSec.value,
      cooldown_sec: editCooldownSec.value,
    })

    closeEdit()
    await refresh()
  } catch (e) {
    editErr.value = apiErrorMessage(e)
  } finally {
    editBusy.value = false
  }
}

function editPresetFor(r: AgentAlertRule): PresetDef | null {
  const pid = r.rule_metadata?.preset_id
  if (!pid) return null
  return presets.value.find(p => p.id === pid) || null
}

async function refresh() {
  loading.value = true
  loadErr.value = ''
  try {
    const [rl, pl, cw] = await Promise.all([
      agentAlertsApi.list(),
      agentAlertsApi.presets(),
      // Best-effort: pull the current 5h snapshot to show "your current value"
      // next to each preset param. Never blocks the panel. Must go through the
      // shared axios client so the `X-CSM-Client: 1` header is injected — a raw
      // window.fetch skips it and the backend rejects the request with 400
      // ("missing X-CSM-Client"), which is what spammed the log here.
      tokensApi.current(5).catch(() => null),
    ])
    rules.value = rl.items
    presets.value = pl.items
    currentWindow.value = cw
    // Initialize per-preset params from defaults if unset.
    for (const p of pl.items) {
      if (!presetParams[p.id]) {
        presetParams[p.id] = {}
        for (const ps of p.params) presetParams[p.id][ps.key] = ps.default
      }
    }
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  } finally {
    loading.value = false
  }
}

// Return "you are currently at X" hint text for a preset param, based on the
// user's actual 5h window snapshot. Used by the preset card body to help
// new users decide whether the default threshold is realistic.
function currentValueHint(preset: PresetDef, paramKey: string): string {
  const w = currentWindow.value
  if (!w) return ''
  const fmt = (n: number) => n.toLocaleString()
  const mfmt = (n: number) => `${(n / 1_000_000).toFixed(1)}M`
  if (preset.id === 'msg_count_warn' && paramKey === 'threshold') {
    return `you right now (5h): ${fmt(w.msg_count || 0)} messages`
  }
  if (preset.id === 'total_tokens_warn' && paramKey === 'tokens_million') {
    return `you right now (5h): ${mfmt(w.total_tokens || 0)}`
  }
  if (preset.id === 'session_burn') {
    if (paramKey === 'share_pct') return `your top share right now: ${((w.top_session_share || 0) * 100).toFixed(0)}%`
    if (paramKey === 'tokens_million') return `your top session right now: ${mfmt(w.top_session_tokens || 0)}`
  }
  if (preset.id === 'cache_hit_drop') {
    if (paramKey === 'ratio_pct') return `your Claude hit rate right now: ${((w.cache_hit_ratio_claude || 0) * 100).toFixed(1)}%`
    if (paramKey === 'tokens_million') return `your Claude total right now: ${mfmt(w.claude_total_tokens || 0)}`
  }
  return ''
}

async function simulateRule(r: AgentAlertRule) {
  try {
    await agentAlertsApi.simulate(r.id)
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  }
}

async function snoozeRule(r: AgentAlertRule, minutes: number) {
  try {
    await agentAlertsApi.snooze(r.id, minutes)
    await refresh()
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  }
}

async function unsnoozeRule(r: AgentAlertRule) {
  try {
    await agentAlertsApi.unsnooze(r.id)
    await refresh()
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  }
}

function snoozedActive(r: AgentAlertRule): boolean {
  if (!r.snoozed_until) return false
  return new Date(r.snoozed_until).getTime() > Date.now()
}

function fmtSnoozedRemaining(iso: string): string {
  const remainingSec = Math.floor((new Date(iso).getTime() - Date.now()) / 1000)
  if (remainingSec < 60) return `${remainingSec}s`
  if (remainingSec < 3600) return `${Math.floor(remainingSec / 60)}m`
  return `${Math.floor(remainingSec / 3600)}h ${Math.floor((remainingSec % 3600) / 60)}m`
}

async function enablePreset(preset: PresetDef) {
  presetErr[preset.id] = ''
  presetBusy[preset.id] = true
  try {
    await agentAlertsApi.fromPreset({
      preset_id: preset.id,
      params: presetParams[preset.id],
    })
    await refresh()
  } catch (e) {
    presetErr[preset.id] = apiErrorMessage(e)
  } finally {
    presetBusy[preset.id] = false
  }
}

function alreadyEnabled(presetId: string): boolean {
  return rules.value.some(r => (r as any)?.rule_metadata?.preset_id === presetId
    || r.name?.includes(presetId))
}

function resetCustomForm() {
  fName.value = ''
  fDesc.value = ''
  fPollSec.value = 60
  fCooldownSec.value = 300
  fChannels.value = { inapp: true, lark: false }
  fEscalate.value = false
  fLarkChat.value = ''
  fLarkUser.value = ''
  genResult.value = null
  stage.value = 'form'
  formErr.value = ''
  showAdvanced.value = false
}

function openCustom() {
  editingCustomRuleId.value = null
  resetCustomForm()
  showCustomModal.value = true
}

function closeCustom() {
  showCustomModal.value = false
  editingCustomRuleId.value = null
}

async function onGenerate() {
  formErr.value = ''
  if (!fName.value.trim()) { formErr.value = 'Name is required'; return }
  if (!fDesc.value.trim()) { formErr.value = 'Description is required'; return }
  if (!fChannels.value.inapp && !fChannels.value.lark) {
    formErr.value = 'Pick at least one notification channel'; return
  }
  stage.value = 'generating'
  try {
    const r = await agentAlertsApi.generate({
      name: fName.value.trim(),
      nl_description: fDesc.value.trim(),
      threshold_spec: {},   // agent infers from NL description
      escalate: fEscalate.value,
    })
    genResult.value = r
    stage.value = 'preview'
  } catch (e) {
    formErr.value = apiErrorMessage(e)
    stage.value = 'form'
  }
}

async function onSave() {
  if (!genResult.value?.script) return
  const channels: string[] = []
  if (fChannels.value.inapp) channels.push('inapp')
  if (fChannels.value.lark) channels.push('lark')
  stage.value = 'saving'
  try {
    if (editingCustomRuleId.value) {
      // Edit path: PATCH the existing rule with the regenerated script +
      // metadata rather than creating a new row.
      await agentAlertsApi.patch(editingCustomRuleId.value, {
        name: fName.value.trim(),
        nl_description: fDesc.value.trim(),
        threshold_spec: {},
        check_script: genResult.value.script,
        poll_interval_sec: fPollSec.value,
        cooldown_sec: fCooldownSec.value,
        channels,
        escalate: fEscalate.value,
        lark_chat_id: fChannels.value.lark ? fLarkChat.value.trim() || undefined : undefined,
        lark_user_id: fChannels.value.lark ? fLarkUser.value.trim() || undefined : undefined,
      })
    } else {
      await agentAlertsApi.create({
        name: fName.value.trim(),
        nl_description: fDesc.value.trim(),
        threshold_spec: {},
        check_script: genResult.value.script,
        poll_interval_sec: fPollSec.value,
        cooldown_sec: fCooldownSec.value,
        channels,
        escalate: fEscalate.value,
        lark_chat_id: fChannels.value.lark ? fLarkChat.value.trim() || undefined : undefined,
        lark_user_id: fChannels.value.lark ? fLarkUser.value.trim() || undefined : undefined,
        enabled: true,
      })
    }
    closeCustom()
    await refresh()
  } catch (e) {
    formErr.value = apiErrorMessage(e)
    stage.value = 'preview'
  }
}

async function toggleRule(r: AgentAlertRule) {
  try {
    await agentAlertsApi.patch(r.id, { enabled: !r.enabled })
    await refresh()
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  }
}

async function deleteRule(r: AgentAlertRule) {
  if (!confirm(`Delete the rule "${r.name}"?`)) return
  try {
    await agentAlertsApi.delete(r.id)
    await refresh()
  } catch (e) {
    loadErr.value = apiErrorMessage(e)
  }
}

function fmtRelative(iso?: string | null): string {
  if (!iso) return 'never fired'
  const d = new Date(iso)
  const sec = Math.floor((Date.now() - d.getTime()) / 1000)
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

onMounted(() => refresh())

defineExpose({ refresh })
</script>

<template>
  <div class="wrap">
    <!-- ============ Presets ============ -->
    <div class="panel">
      <h3 class="serif">
        Quick start · presets
        <button @click="openCustom" class="hdr-btn">+ Custom rule</button>
      </h3>
      <div class="scope-note">
        <strong>Alert rules</strong> only send notifications — they never pause a
        session. For automatic stops and hard limits, use the Budgets module.
      </div>
      <div class="preset-grid">
        <div
          v-for="p in presets"
          :key="p.id"
          class="preset-card"
          :class="{ open: presetOpen[p.id] }"
        >
          <!-- ===== Compact header (always visible, click to toggle) ===== -->
          <div class="preset-head" @click="toggleOpen(p.id)">
            <div class="preset-head-main">
              <div class="preset-title-row">
                <span class="preset-title">{{ p.title }}</span>
                <span v-if="p.escalate_default" class="tag agent">agent</span>
              </div>
              <div v-if="!presetOpen[p.id]" class="preset-summary">
                {{ presetSummary(p) }}
              </div>
            </div>
            <span class="chev" :class="{ open: presetOpen[p.id] }">▸</span>
          </div>

          <!-- ===== Expanded body ===== -->
          <div v-if="presetOpen[p.id]" class="preset-body" @click.stop>
            <div class="preset-desc">{{ p.description }}</div>
            <div class="preset-params">
              <label v-for="ps in p.params" :key="ps.key" class="param">
                <span class="param-label">{{ ps.label }}</span>
                <span class="param-input-wrap">
                  <input
                    type="number"
                    :min="ps.min"
                    :max="ps.max"
                    :step="ps.step"
                    v-model.number="presetParams[p.id][ps.key]"
                  />
                  <span class="param-unit">{{ ps.unit }}</span>
                </span>
                <small v-if="currentValueHint(p, ps.key)" class="param-hint">
                  {{ currentValueHint(p, ps.key) }}
                </small>
              </label>
            </div>
            <div class="preset-window-note">
              Checked over a rolling <strong>5-hour</strong> window
            </div>
            <div class="preset-example">
              <span class="ex-label">Example notification:</span>{{ p.notify_example }}
            </div>
          </div>

          <!-- ===== Footer with Enable button + error ===== -->
          <div class="preset-footer" @click.stop>
            <span v-if="presetErr[p.id]" class="preset-err">{{ presetErr[p.id] }}</span>
            <button
              class="primary"
              :disabled="presetBusy[p.id]"
              @click="enablePreset(p)"
            >
              {{ presetBusy[p.id] ? 'Enabling…' : 'Enable' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ Enabled rules — compact strips ============ -->
    <div class="panel">
      <h3 class="serif">
        Enabled rules
        <span class="count">{{ rules.length }}</span>
      </h3>
      <div v-if="loadErr" class="err">{{ loadErr }}</div>
      <div v-if="!loading && !rules.length" class="empty">
        No rules enabled yet — pick a preset above and turn it on.
      </div>
      <div class="strip-list">
        <div
          v-for="r in rules"
          :key="r.id"
          class="strip"
          :class="{ off: !r.enabled, errored: !!r.last_error }"
        >
          <div class="strip-main">
            <span class="dot" :class="{ on: r.enabled }"></span>
            <span class="strip-name">{{ r.name }}</span>
            <span class="strip-desc">{{ r.nl_description }}</span>
            <span class="strip-tags">
              <span class="tag idle">{{ r.poll_interval_sec }}s</span>
              <span v-for="c in r.channels" :key="c" class="tag info">
                {{ c === 'inapp' ? 'web' : c === 'lark' ? 'Lark' : c }}
              </span>
              <span v-if="r.escalate" class="tag agent">agent</span>
              <span v-if="snoozedActive(r)" class="tag muted" @click.stop="unsnoozeRule(r)" title="Click to un-snooze">
                snoozed {{ fmtSnoozedRemaining(r.snoozed_until!) }}
              </span>
            </span>
            <span class="strip-ts">last fired {{ fmtRelative(r.last_fired_at) }}</span>
            <span class="strip-actions">
              <button
                v-if="!snoozedActive(r)"
                @click="snoozeRule(r, 120)"
                class="edit"
                title="Snooze for 2 hours"
              >🔕</button>
              <button
                @click="simulateRule(r)"
                class="edit"
                title="Fire once, to see what the notification looks like"
              >▶</button>
              <button
                v-if="r.rule_metadata?.preset_id"
                @click="openEdit(r)"
                class="edit"
                title="Edit params"
              >✎</button>
              <button
                @click="toggleRule(r)"
                class="tag-btn"
                :class="r.enabled ? 'on' : 'off'"
              >{{ r.enabled ? 'on' : 'off' }}</button>
              <button @click="deleteRule(r)" class="del" title="Delete">×</button>
            </span>
          </div>
          <div v-if="r.last_error" class="strip-error">
            <span class="err-icon">⚠</span>
            <span class="err-text">{{ r.last_error }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ Edit modal (preset-based rules) ============ -->
    <Teleport to="body">
      <div v-if="editRule" class="modal-backdrop" @click.self="closeEdit">
        <div class="modal panel edit-modal">
          <h3 class="serif">Edit rule</h3>
          <div class="edit-rule-name">
            <strong>{{ editRule.name }}</strong>
            <small>{{ editPresetFor(editRule)?.description }}</small>
          </div>

          <!-- Section 1: threshold params -->
          <div class="edit-section">
            <div class="edit-section-title">Trigger thresholds</div>
            <div class="preset-params">
              <label v-for="ps in editPresetFor(editRule)?.params || []" :key="ps.key" class="param">
                <span class="param-label">{{ ps.label }}</span>
                <span class="param-input-wrap">
                  <input
                    type="number"
                    :min="ps.min"
                    :max="ps.max"
                    :step="ps.step"
                    v-model.number="editParams[ps.key]"
                  />
                  <span class="param-unit">{{ ps.unit }}</span>
                </span>
              </label>
            </div>
            <div class="edit-hint">The suggested range is a hint — no min/max is enforced</div>
          </div>

          <!-- Section 2: notification channels -->
          <div class="edit-section">
            <div class="edit-section-title">Notification channels</div>
            <div class="channels-row">
              <label class="inline">
                <input type="checkbox" v-model="editChannels.inapp" /> Web
              </label>
              <label class="inline">
                <input type="checkbox" v-model="editChannels.lark" /> Lark
              </label>
            </div>
            <div v-if="editChannels.lark" class="lark-fields">
              <label>
                Lark chat_id
                <input v-model="editLarkChat" placeholder="oc_xxx (blank = the default set in Settings → Lark)" />
              </label>
              <label>
                Lark user_id
                <input v-model="editLarkUser" placeholder="ou_xxx (optional)" />
              </label>
              <div class="edit-hint">
                Lark delivery targets are configured once under
                <router-link to="/settings">Settings → Lark push</router-link>
                (enable switch, chat_id / user_id, and a test button). The
                fields here only override that default for this one rule —
                leave them blank to use the Settings values.
              </div>
            </div>
          </div>

          <!-- Section 3: agent escalation -->
          <div class="edit-section">
            <label class="inline strong">
              <input type="checkbox" v-model="editEscalate" />
              Have an agent diagnose the cause when this fires
              <small>Calls claude for a root cause and recommendations. The
                notification body gets much more useful, at the cost of
                tokens.</small>
            </label>
          </div>

          <!-- Section 4: cadence -->
          <div class="edit-section">
            <div class="edit-section-title">Cadence</div>
            <div class="cadence-row">
              <label>
                Check every (seconds)
                <input type="number" v-model.number="editPollSec" min="10" max="3600" />
              </label>
              <label>
                Cooldown (seconds)
                <input type="number" v-model.number="editCooldownSec" min="0" max="86400" />
              </label>
            </div>
          </div>

          <div v-if="editErr" class="form-err">{{ editErr }}</div>
          <div class="modal-actions">
            <button @click="closeEdit">Cancel</button>
            <button class="primary" :disabled="editBusy" @click="saveEdit">
              {{ editBusy ? 'Saving…' : 'Save' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ============ Custom rule modal (advanced flow) ============ -->
    <Teleport to="body">
      <div v-if="showCustomModal" class="modal-backdrop" @click.self="closeCustom">
        <div class="modal panel authoring">
          <h3 class="serif">{{ editingCustomRuleId ? 'Edit custom rule' : 'Custom rule' }}</h3>

          <div v-if="stage === 'form'" class="form-simple">
            <label>
              Rule name
              <input v-model="fName" placeholder="name it, e.g. opus_burn_warning" />
            </label>
            <label>
              Describe the trigger in plain language
              <textarea v-model="fDesc" rows="4"
                placeholder="e.g. Opus burned more than 5M tokens in the last 5 hours and the cache hit rate is under 30%"></textarea>
              <small class="hint">The agent writes the check script from your description</small>
            </label>
            <div class="row">
              <label class="inline">
                <input type="checkbox" v-model="fChannels.inapp" /> Web notification
              </label>
              <label class="inline">
                <input type="checkbox" v-model="fChannels.lark" /> Lark notification
              </label>
            </div>
            <label class="inline strong">
              <input type="checkbox" v-model="fEscalate" />
              Have an agent diagnose the cause and recommend a fix when this fires
              <small>(Unchecked, you get the numbers only. Checked, it calls
                claude once to look at the top session, tool loops and model
                mix.)</small>
            </label>

            <button class="ghost small" @click="showAdvanced = !showAdvanced">
              {{ showAdvanced ? '▾' : '▸' }} Advanced
            </button>
            <div v-if="showAdvanced" class="advanced-grid">
              <label>
                Check every (seconds)
                <input type="number" v-model.number="fPollSec" min="10" max="3600" />
              </label>
              <label>
                Cooldown (seconds)
                <input type="number" v-model.number="fCooldownSec" min="0" max="86400" />
              </label>
              <template v-if="fChannels.lark">
                <label>
                  Lark chat_id
                  <input v-model="fLarkChat" placeholder="oc_xxx (blank = environment default)" />
                </label>
                <label>
                  Lark user_id
                  <input v-model="fLarkUser" placeholder="ou_xxx" />
                </label>
              </template>
            </div>

            <div v-if="formErr" class="form-err">{{ formErr }}</div>
          </div>

          <div v-else-if="stage === 'generating'" class="stage-msg">
            <div class="spin"></div>
            <p>Claude is writing your check script… (about 5-10s)</p>
          </div>

          <div v-else-if="stage === 'preview'" class="preview">
            <div v-if="!genResult?.ok" class="form-err">
              Generation failed: {{ genResult?.error }}
            </div>
            <div class="preview-block">
              <div class="preview-label">
                Dry run against your current 5h data
                <span v-if="genResult?.dry_run?.fired" class="tag warn">would fire</span>
                <span v-else class="tag idle">would not fire</span>
              </div>
              <pre class="code small">{{ JSON.stringify(genResult?.dry_run?.payload || {}, null, 2) }}</pre>
            </div>
            <details class="advanced">
              <summary>Show the generated check script</summary>
              <pre class="code">{{ genResult?.script || '(none)' }}</pre>
            </details>
            <div v-if="formErr" class="form-err">{{ formErr }}</div>
          </div>

          <div v-else class="stage-msg">
            <div class="spin"></div>
            <p>Saving…</p>
          </div>

          <div class="modal-actions">
            <template v-if="stage === 'form'">
              <button @click="closeCustom">Cancel</button>
              <button class="primary" @click="onGenerate">Have the agent write it</button>
            </template>
            <template v-else-if="stage === 'preview'">
              <button @click="stage = 'form'">Back to edit</button>
              <button @click="onGenerate">Regenerate</button>
              <button class="primary" :disabled="!genResult?.script" @click="onSave">Save rule</button>
            </template>
            <template v-else>
              <button disabled>Working…</button>
            </template>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.wrap { display: flex; flex-direction: column; gap: 14px; }

/* ---------- shared ---------- */
.panel { padding: 14px 16px; }
.panel h3 {
  display: flex; justify-content: space-between; align-items: center;
  margin: 0 0 12px; font-size: 14px;
}
.count {
  font-size: 11px; color: var(--ink-muted, #787774);
  padding: 1px 8px; border-radius: 3px; background: var(--canvas);
}
.hdr-btn {
  font-size: 11px; padding: 3px 10px; border-radius: 4px;
  background: transparent; border: 1px solid var(--border); color: var(--ink); cursor: pointer;
}
.hdr-btn:hover { background: var(--canvas); }
.err { font-size: 12px; color: var(--pastel-red-fg, #C25450); padding: 6px 0; }
.empty { font-size: 12px; color: var(--ink-muted, #787774); padding: 8px 0; }

.tag {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 10px; font-family: var(--mono, monospace);
  background: transparent; border: 1px solid var(--border); color: var(--ink);
}
.tag.info { background: var(--pastel-blue-bg, #E8F0F7); color: var(--pastel-blue-fg, #4A6D8C); border-color: transparent; }
.tag.idle { background: var(--canvas); }
.tag.warn { background: var(--pastel-yellow-bg, #FCF6E4); color: var(--pastel-yellow-fg, #957024); border-color: transparent; }
.tag.agent { background: var(--pastel-purple-bg, #F0EAF7); color: var(--pastel-purple-fg, #7E57A6); border-color: transparent; }
.tag.muted { background: var(--pastel-yellow-bg, #FCF6E4); color: var(--pastel-yellow-fg, #957024); border-color: transparent; cursor: pointer; }
.tag.muted:hover { filter: brightness(0.95); }

/* ---------- presets grid (compact + expandable) ---------- */
.preset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
.preset-card {
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--canvas); display: flex; flex-direction: column;
  transition: box-shadow 0.15s;
}
.preset-card.open { box-shadow: 0 1px 6px rgba(0,0,0,0.08); }

/* Header row — always visible + clickable to toggle */
.preset-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; cursor: pointer; user-select: none;
}
.preset-head:hover { background: color-mix(in srgb, var(--canvas), var(--ink) 3%); }
.preset-head-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.preset-title-row { display: flex; align-items: center; gap: 6px; }
.preset-title { font-size: 13px; font-weight: 600; color: var(--ink); }
.preset-summary {
  font-size: 12px; color: var(--ink-muted, #787774);
  overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
}
.chev {
  color: var(--ink-muted, #787774); font-size: 11px;
  transition: transform 0.15s; flex-shrink: 0;
}
.chev.open { transform: rotate(90deg); }

/* Expanded body */
.preset-body {
  padding: 4px 12px 10px; display: flex; flex-direction: column; gap: 10px;
  border-top: 1px solid var(--border);
}
.preset-desc { font-size: 12px; color: var(--ink-muted, #787774); line-height: 1.5; padding-top: 6px; }
.preset-params { display: flex; flex-direction: column; gap: 6px; }
.param {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: 4px 8px;
  font-size: 12px;
}
.param .param-hint { grid-column: 1 / -1; text-align: right; }
.param-label { color: var(--ink-muted, #787774); }
.param-input-wrap { display: inline-flex; align-items: center; gap: 4px; }
.param input {
  width: 5.5rem; padding: 3px 6px; border: 1px solid var(--border); border-radius: 3px;
  font-size: 12px; text-align: right; background: var(--card); color: var(--ink);
}
.param-unit { font-size: 11px; color: var(--ink-muted, #787774); }
.preset-example {
  font-size: 11px; color: var(--ink-muted, #787774);
  font-family: var(--mono, monospace); padding: 6px 8px;
  background: var(--card); border-radius: 4px; border: 1px dashed var(--border);
  line-height: 1.5;
}
.param-hint {
  font-size: 10.5px; color: var(--pastel-blue-fg, #4A6D8C);
  align-self: flex-end; padding-right: 4px;
}
.preset-window-note {
  font-size: 10.5px; color: var(--ink-muted, #787774);
  padding: 3px 8px; border-radius: 3px; background: var(--card);
  border: 1px dashed var(--border);
}
.preset-window-note strong { color: var(--ink); }
.scope-note {
  font-size: 11px; color: var(--ink-muted, #787774);
  padding: 8px 12px; border-radius: 5px; background: var(--canvas);
  margin-bottom: 12px; line-height: 1.6;
}
.scope-note strong { color: var(--ink); }
.ex-label { font-family: var(--sans, inherit); color: var(--ink); font-weight: 500; }

.preset-footer {
  display: flex; justify-content: flex-end; align-items: center; gap: 10px;
  padding: 8px 12px; border-top: 1px solid var(--border);
}
.preset-err {
  font-size: 11px; color: var(--pastel-red-fg, #C25450);
  flex: 1; text-align: left;
}
.preset-footer .primary {
  padding: 4px 14px; font-size: 12px; border-radius: 4px;
  background: var(--ink); color: var(--card); border: 1px solid var(--ink); cursor: pointer;
}
.preset-footer .primary:disabled { opacity: 0.5; cursor: not-allowed; }

/* ---------- compact rule strips ---------- */
.strip-list { display: flex; flex-direction: column; gap: 4px; }
.strip {
  padding: 6px 10px; border-radius: 5px; background: var(--canvas);
  border: 1px solid var(--border); font-size: 12px;
  display: flex; flex-direction: column; gap: 4px;
}
.strip-main {
  display: grid;
  grid-template-columns: 10px minmax(120px, 180px) 1fr auto auto auto;
  align-items: center; gap: 10px;
}
.strip.off { opacity: 0.55; }
.strip.errored { border-color: var(--pastel-red-fg, #C25450); background: color-mix(in srgb, var(--pastel-red-bg, #FCEAE9), var(--canvas) 60%); }
.strip-error {
  display: flex; gap: 6px; align-items: flex-start;
  padding: 6px 8px; border-radius: 4px;
  background: var(--pastel-red-bg, #FCEAE9);
  color: var(--pastel-red-fg, #C25450);
  font-size: 11px; line-height: 1.4;
}
.strip-error .err-icon { flex-shrink: 0; font-weight: 700; }
.strip-error .err-text { word-break: break-word; font-family: var(--mono, monospace); }
.dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--border);
}
.dot.on { background: var(--pastel-green-fg, #4A8A5F); }
.strip-name { font-weight: 600; color: var(--ink); }
.strip-desc {
  color: var(--ink-muted, #787774); overflow: hidden;
  white-space: nowrap; text-overflow: ellipsis;
}
.strip-tags { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
.strip-ts { font-size: 11px; color: var(--ink-muted, #787774); flex-shrink: 0; }
.strip-actions { display: flex; gap: 4px; flex-shrink: 0; }
.tag-btn {
  padding: 2px 10px; font-size: 11px; border-radius: 3px; cursor: pointer;
  border: 1px solid var(--border); background: var(--card); color: var(--ink);
}
.tag-btn.on { background: var(--pastel-green-bg, #E8F2EA); color: var(--pastel-green-fg, #4A8A5F); border-color: transparent; }
.tag-btn.off { background: var(--canvas); }
.edit {
  padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 12px;
  border: 1px solid var(--border); background: transparent; color: var(--ink);
}
.edit:hover { background: var(--pastel-blue-bg, #E8F0F7); color: var(--pastel-blue-fg, #4A6D8C); }
.del {
  padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 12px;
  border: 1px solid var(--border); background: transparent; color: var(--ink);
}
.del:hover { background: var(--pastel-red-bg, #FCEAE9); color: var(--pastel-red-fg, #C25450); }

/* Edit modal — smaller than the authoring one */
.modal.edit-modal {
  padding: 20px 24px; max-width: 34rem; width: 90vw;
  max-height: 90vh; overflow-y: auto;
  background: var(--card); border-radius: 10px; border: 1px solid var(--border);
}
.edit-rule-name {
  padding: 8px 12px; border-radius: 6px; background: var(--canvas);
  margin-bottom: 14px; display: flex; flex-direction: column; gap: 3px;
}
.edit-rule-name strong { font-size: 13px; color: var(--ink); }
.edit-rule-name small { font-size: 11px; color: var(--ink-muted, #787774); line-height: 1.5; }

.edit-section {
  padding: 12px 0; border-top: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 8px;
}
.edit-section:first-of-type { border-top: none; padding-top: 0; }
.edit-section-title {
  font-size: 12px; font-weight: 600; color: var(--ink);
  margin-bottom: 4px;
}
.edit-modal .preset-params { padding: 0; }
.edit-hint {
  font-size: 11px; color: var(--ink-muted, #787774); line-height: 1.5;
}
.edit-hint code {
  padding: 1px 4px; border-radius: 3px; background: var(--canvas);
  font-family: var(--mono, monospace); font-size: 10.5px;
}
.channels-row { display: flex; gap: 20px; }
.channels-row .inline, .edit-section .inline {
  display: inline-flex; align-items: center; gap: 6px;
  color: var(--ink); font-size: 13px; cursor: pointer;
}
.edit-section .inline.strong { font-weight: 500; flex-wrap: wrap; }
.edit-section .inline small {
  color: var(--ink-muted, #787774); font-size: 11px;
  margin-left: 22px; line-height: 1.5; display: block; width: 100%;
}
.lark-fields {
  display: flex; flex-direction: column; gap: 8px;
  padding: 10px 12px; margin-top: 4px;
  border-radius: 6px; background: var(--canvas);
}
.lark-fields label {
  display: flex; flex-direction: column; gap: 3px;
  font-size: 12px; color: var(--ink-muted, #787774);
}
.lark-fields input {
  padding: 5px 8px; border: 1px solid var(--border); border-radius: 4px;
  font-size: 12px; background: var(--card); color: var(--ink);
}
.cadence-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.cadence-row label {
  display: flex; flex-direction: column; gap: 3px;
  font-size: 12px; color: var(--ink-muted, #787774);
}
.cadence-row input {
  padding: 5px 8px; border: 1px solid var(--border); border-radius: 4px;
  font-size: 12px; background: var(--card); color: var(--ink);
}

/* ---------- modal ---------- */
.modal-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 200;
}
.modal.authoring {
  padding: 22px 26px; max-width: 40rem; width: 90vw; max-height: 90vh; overflow: auto;
  background: var(--card); border-radius: 10px; border: 1px solid var(--border);
}
.modal h3 { margin: 0 0 14px; font-size: 16px; }
.form-simple { display: flex; flex-direction: column; gap: 14px; }
.form-simple label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--ink-muted, #787774); }
.form-simple label.inline {
  flex-direction: row; align-items: flex-start; gap: 6px;
  color: var(--ink); font-size: 13px; flex-wrap: wrap;
}
.form-simple label.inline small { color: var(--ink-muted, #787774); font-size: 11px; margin-left: 22px; line-height: 1.5; }
.form-simple label.inline.strong { font-weight: 500; }
.form-simple input, .form-simple textarea {
  padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px;
  font-size: 13px; background: var(--card); color: var(--ink);
}
.hint { font-size: 11px; color: var(--ink-muted, #787774); }
.row { display: flex; gap: 20px; }
.form-simple button.ghost.small {
  align-self: flex-start; padding: 3px 8px; font-size: 11px; border-radius: 3px;
  border: 1px solid var(--border); background: transparent; color: var(--ink); cursor: pointer;
}
.advanced-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  padding: 10px 12px; border-radius: 6px; background: var(--canvas);
}
.form-err { font-size: 12px; color: var(--pastel-red-fg, #C25450); padding: 6px 0; }

.stage-msg { display: flex; flex-direction: column; align-items: center; padding: 32px 12px; gap: 14px; }
.spin { width: 32px; height: 32px; border: 3px solid var(--border); border-top-color: var(--pastel-blue-fg, #4A6D8C); border-radius: 50%; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.preview { display: flex; flex-direction: column; gap: 14px; }
.preview-block { display: flex; flex-direction: column; gap: 6px; }
.preview-label { font-size: 12px; color: var(--ink-muted, #787774); display: flex; gap: 8px; align-items: center; }
.code {
  background: var(--canvas); border: 1px solid var(--border); border-radius: 4px;
  padding: 10px 12px; margin: 0; font-family: var(--mono, monospace);
  font-size: 12px; line-height: 1.45; white-space: pre-wrap; overflow-x: auto;
  max-height: 260px; overflow-y: auto;
}
.code.small { font-size: 11px; max-height: 180px; }
details.advanced { margin: 0; }
details.advanced summary { font-size: 12px; color: var(--ink-muted, #787774); cursor: pointer; padding: 4px 0; }

.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.modal-actions button {
  padding: 6px 14px; border-radius: 4px; cursor: pointer;
  background: transparent; border: 1px solid var(--border); color: var(--ink); font-size: 13px;
}
.modal-actions button.primary { background: var(--ink); color: var(--card); border-color: var(--ink); }
.modal-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
