<script setup lang="ts">
/**
 * NewWorkflowModal — 3-step wizard:
 *
 *   Step 1 (form)         : repo_path + requirement + optional workflow_name
 *   Step 2 (clarify)      : agent's boundary questions with defaults
 *                            (skipped when needs_clarify=false)
 *   Step 3 (result)       : review verdict + Launch/Fix/Discard
 *
 * Between steps we spin a blocking spinner. All backend calls surface
 * errors gracefully — the user never gets stuck. "Accept all defaults
 * & generate" on Step 2 collapses the flow into an effective one-step
 * experience.
 */
import { apiErrorDetail, apiErrorMessage } from '../../lib/apiError'
import {
  reviewRuleCounts,
  reviewRules,
  semanticErrorOf,
  semanticVerdictsOf,
} from '../../api/automation'
import { computed, ref, watch } from 'vue'
import { automationApi } from '../../api/automation'
import { useToast } from '../../composables/useToast'
import FilePicker from '../FilePicker.vue'
const toast = useToast()

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'generated', workflowId: string): void
}>()

type WizardStep = 'form' | 'clarifying' | 'clarify' | 'generating' | 'result'
const step = ref<WizardStep>('form')

// ---- Step 1 inputs ----
const repoPath = ref('')
const requirement = ref('')
const workflowName = ref('')

// ---- FilePicker for repo path ----
const showRepoPicker = ref(false)
function onPickRepo(p: string) {
  repoPath.value = p
  showRepoPicker.value = false
}

// ---- Step 2 state ----
type ClarifyResponse = Awaited<ReturnType<typeof automationApi.clarifyWorkflow>>
type StageProposal = { name: string; kind: 'claude' | 'poll'; purpose: string }
const clarifyResult = ref<ClarifyResponse | null>(null)
// answers[qid] = selected option value; free_text[qid] = optional supplement
const answers = ref<Record<string, string>>({})
const freeText = ref<Record<string, string>>({})
// Editable copy of the agent's proposed stage skeleton. User can rename /
// delete / add / reorder before generate locks it in. Seeded from
// clarifyResult.stages when the clarify response comes back.
const editableStages = ref<StageProposal[]>([])
// Track whether the user actually opened the editor (vs. just accepted
// defaults) so we can send confirmed_stages only when they matter — a
// pristine array === agent proposal, which the backend already has via
// the clarification_id cache.
const stagesTouched = ref(false)

// ---- Progress spinner ----
const elapsedSec = ref(0)
const progressLabel = ref('')
let elapsedTimer: number | null = null

// ---- Step 3 output ----
type Result = Awaited<ReturnType<typeof automationApi.generateWorkflow>>
const result = ref<Result | null>(null)

function resetAll() {
  step.value = 'form'
  repoPath.value = ''
  requirement.value = ''
  workflowName.value = ''
  clarifyResult.value = null
  answers.value = {}
  freeText.value = {}
  editableStages.value = []
  stagesTouched.value = false
  result.value = null
  stopElapsed()
}

watch(() => props.open, (isOpen) => {
  if (isOpen) resetAll()
})

function startElapsed(label: string) {
  progressLabel.value = label
  elapsedSec.value = 0
  if (elapsedTimer !== null) clearInterval(elapsedTimer)
  elapsedTimer = window.setInterval(() => { elapsedSec.value += 1 }, 1000)
}
function stopElapsed() {
  if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null }
}

// ---------------------------------------------------------------
// Step 1 → clarify
// ---------------------------------------------------------------
async function submitForm() {
  if (!repoPath.value.trim() || !requirement.value.trim()) {
    toast.warn('Fill in the repo path and the one-sentence requirement')
    return
  }
  step.value = 'clarifying'
  startElapsed('The agent is scanning the repo and working out what to ask…')
  try {
    const res = await automationApi.clarifyWorkflow({
      repo_path: repoPath.value.trim(),
      requirement: requirement.value.trim(),
      workflow_name: workflowName.value.trim() || undefined,
    })
    clarifyResult.value = res
    // Seed editable stages from agent proposal (may be empty if legacy/error).
    editableStages.value = (res.stages || []).map(s => ({ ...s }))
    stagesTouched.value = false
    if (!res.needs_clarify) {
      // Two reasons the backend flagged no-clarify:
      //   a) Agent judged fully linear + provided no skeleton → legit skip.
      //   b) Clarify errored (timeout / parse fail / non-zero exit).
      // Signal (b) clearly so the user knows the clarify step was
      // essentially disabled and can retry with a shorter requirement.
      if (res.error) {
        toast.warn(
          'Clarification failed; going straight to generation. Reason: '
          + res.error.slice(0, 120)
          + (res.error.length > 120 ? '…' : '')
          + ' (grep workflow-clarify in csm.log for detail)'
        )
        await runGenerate()
      } else {
        // Legit skip. If we got a stage proposal we still show the clarify
        // screen so the user can review the skeleton before generation.
        if ((res.stages || []).length > 0) {
          toast.info('The agent found nothing to clarify — check its stage breakdown before generating.')
          step.value = 'clarify'
        } else {
          toast.info('The agent found nothing to clarify — generating directly.')
          await runGenerate()
        }
      }
    } else {
      // Seed answers with the recommended defaults.
      const seed: Record<string, string> = {}
      for (const q of res.questions) {
        const rec = q.options.find(o => o.recommended) || q.options[0]
        seed[q.id] = rec.value
      }
      answers.value = seed
      freeText.value = {}
      step.value = 'clarify'
    }
  } catch (e) {
    // Network / 500 — degrade to one-shot generate.
    toast.warn('Clarification errored — going straight to YAML generation')
    await runGenerate()
  } finally {
    stopElapsed()
  }
}

// ---------------------------------------------------------------
// Step 2 → generate
// ---------------------------------------------------------------
async function submitClarify(useAllDefaults: boolean) {
  // If "Accept all defaults", overwrite answers with recommended values.
  if (useAllDefaults && clarifyResult.value) {
    const seed: Record<string, string> = {}
    for (const q of clarifyResult.value.questions) {
      const rec = q.options.find(o => o.recommended) || q.options[0]
      seed[q.id] = rec.value
    }
    answers.value = seed
    freeText.value = {}
  }
  await runGenerate()
}

// ---- Stage skeleton editing helpers (Step 2 preview card) ----
function moveStage(idx: number, dir: -1 | 1) {
  const arr = editableStages.value
  const j = idx + dir
  if (j < 0 || j >= arr.length) return
  const tmp = arr[idx]
  arr[idx] = arr[j]
  arr[j] = tmp
  stagesTouched.value = true
}
function removeStage(idx: number) {
  editableStages.value.splice(idx, 1)
  stagesTouched.value = true
}
function addStage() {
  editableStages.value.push({ name: 'new_stage', kind: 'claude', purpose: '' })
  stagesTouched.value = true
}
function markStagesTouched() {
  stagesTouched.value = true
}
const STAGE_NAME_RE = /^[a-z][a-z0-9_]*$/
const stagesValid = computed(() => {
  const arr = editableStages.value
  if (arr.length === 0) return true  // legacy — no preview
  if (arr.length > 8) return false
  const names = new Set<string>()
  for (const s of arr) {
    if (!STAGE_NAME_RE.test(s.name)) return false
    if (!['claude', 'poll'].includes(s.kind)) return false
    if (!s.purpose.trim()) return false
    if (names.has(s.name)) return false
    names.add(s.name)
  }
  return true
})

async function runGenerate() {
  step.value = 'generating'
  startElapsed('Generating the workflow YAML, running the R9-R19 self-check and the Pass-2 semantic review… (usually 90-360s)')
  result.value = null
  try {
    const res = await automationApi.generateWorkflow({
      repo_path: repoPath.value.trim(),
      requirement: requirement.value.trim(),
      workflow_name: workflowName.value.trim() || undefined,
      clarification_id: clarifyResult.value?.clarification_id || undefined,
      answers: Object.keys(answers.value).length ? answers.value : undefined,
      free_text: Object.values(freeText.value).some(v => (v || '').trim())
        ? freeText.value : undefined,
      // Send confirmed_stages only when the user actually edited the
      // agent's proposal — pristine stages already live in the clarify
      // cache and the backend picks them up via clarification_id.
      confirmed_stages: stagesTouched.value && editableStages.value.length
        ? editableStages.value.map(s => ({ name: s.name, kind: s.kind, purpose: s.purpose }))
        : undefined,
    })
    result.value = res
  } catch (e) {
    // The backend ships the whole Result as `detail` on failure (see
    // api/workflows.py) so review_report + stdout_tail survive the 500.
    // Must read the RAW detail — apiErrorMessage would collapse it to text.
    const detail = apiErrorDetail(e)
    result.value = (detail && typeof detail === 'object') ? detail as Result : {
      workflow_id: null,
      workflow_name: null,
      yaml_path: null,
      review_status: 'generation_failed',
      review_report: null,
      stdout_tail: '',
      duration_sec: 0,
      error: apiErrorMessage(e),
    } as Result
  } finally {
    stopElapsed()
    step.value = 'result'
  }
}

// ---------------------------------------------------------------
// Step 3 verdict helpers
// ---------------------------------------------------------------
const counts = computed(() => reviewRuleCounts(result.value?.review_report))
const failCount = computed(() => counts.value.fail)
const warnCount = computed(() => counts.value.warn)
const passCount = computed(() => counts.value.pass)
const nonPassRules = computed(() =>
  reviewRules(result.value?.review_report).filter((r) => r.status !== 'pass'),
)

// Pass-2 semantic verdicts (5 categories: stage_decomposition, output_naming,
// prompt_completeness, primitive_choice, branch_coverage). Empty when Pass-2
// wasn't run or errored — surfaced separately from R9-R19.
const semanticVerdicts = computed(() => semanticVerdictsOf(result.value?.review_report))
const semanticError = computed(() => semanticErrorOf(result.value?.review_report))
const semanticIssueCount = computed(() =>
  semanticVerdicts.value.filter(v => v.status !== 'pass').length,
)
function semanticIcon(status: string): string {
  if (status === 'fail') return '✗'
  if (status === 'warn') return '⚠'
  return '✓'
}
const SEMANTIC_LABELS: Record<string, string> = {
  stage_decomposition: 'Stage decomposition',
  output_naming: 'Output naming',
  prompt_completeness: 'Prompt completeness',
  primitive_choice: 'Primitive choice',
  branch_coverage: 'Edge-case coverage',
}
function semanticLabel(category: string): string {
  return SEMANTIC_LABELS[category] || category
}

function onLaunch() {
  if (result.value?.workflow_id) {
    emit('generated', result.value.workflow_id)
    emit('close')
  }
}

async function onFixWarns() {
  if (!result.value?.workflow_name) return
  // Concise fix instruction spliced against the current YAML — the edit
  // path preserves the skeleton and only touches the flagged parts, unlike
  // the old generate-again approach that re-rolled the whole file.
  const structural = nonPassRules.value
    .map((r: any) => `- ${r.rule_id} (${r.status}): ${r.reason}`)
    .join('\n')
  const semantic = semanticVerdicts.value
    .filter(v => v.status !== 'pass')
    .map(v => `- ${v.category} (${v.status}): ${v.reason}`)
    .join('\n')
  const parts: string[] = []
  if (structural) parts.push(`The R9-R19 structural review flagged these — fix them by rule id:\n${structural}`)
  if (semantic) parts.push(`The Pass-2 semantic review flagged these — fix them by category:\n${semantic}`)
  const feedback = parts.join('\n\n')
  if (!feedback) return
  step.value = 'generating'
  startElapsed('The agent is fixing the YAML against the review (edit path — the skeleton is preserved)…')
  const wfName = result.value.workflow_name
  result.value = null
  try {
    const res = await automationApi.editWithAgent(wfName, feedback)
    result.value = res as Result
  } catch (e) {
    // The backend ships the whole Result as `detail` on failure (see
    // api/workflows.py) so review_report + stdout_tail survive the 500.
    // Must read the RAW detail — apiErrorMessage would collapse it to text.
    const detail = apiErrorDetail(e)
    result.value = (detail && typeof detail === 'object') ? detail as Result : {
      workflow_id: null,
      workflow_name: wfName,
      yaml_path: null,
      review_status: 'generation_failed',
      review_report: null,
      stdout_tail: '',
      duration_sec: 0,
      error: apiErrorMessage(e),
    } as Result
  } finally {
    stopElapsed()
    step.value = 'result'
  }
}

function onDiscard() {
  resetAll()
}

// Guard: when clarifying / generating, closing the modal loses state but
// backend continues in the background. That's OK — result won't be
// applied since the workflow row upsert is idempotent.
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal nwf-modal panel" role="dialog" aria-modal="true" aria-label="New workflow">
      <div class="nwf-header">
        <div>
          <div class="nwf-eyebrow">Automation</div>
          <h3 class="serif">+ New workflow</h3>
        </div>
        <button class="close-btn" @click="emit('close')" aria-label="Close">✕</button>
      </div>

      <!-- Step indicator -->
      <div class="nwf-stepper">
        <div class="nwf-step" :class="{ active: step === 'form', done: step !== 'form' }">
          1. Requirement
        </div>
        <div class="nwf-step-line"></div>
        <div class="nwf-step"
             :class="{
               active: step === 'clarifying' || step === 'clarify',
               done: step === 'generating' || step === 'result',
             }">
          2. Clarify
        </div>
        <div class="nwf-step-line"></div>
        <div class="nwf-step"
             :class="{
               active: step === 'generating' || step === 'result',
             }">
          3. Generate
        </div>
      </div>

      <div class="nwf-body">
        <!-- ================= Step 1: form ================= -->
        <div v-if="step === 'form'">
          <p class="nwf-intro">
            Give it the <b>target repo path</b> and a <b>one-sentence
            requirement</b>. The agent asks a few scoping questions, then
            writes the YAML and runs the self-check. The whole thing takes
            about <b>2-6 minutes</b>.
          </p>

          <label class="nwf-label">
            <span>Repo path</span>
            <div class="nwf-input-row">
              <input
                v-model="repoPath"
                placeholder="/data/projects/sample_pipeline"
                class="nwf-input"
                autofocus
              />
              <button
                type="button"
                class="nwf-browse-btn"
                @click="showRepoPicker = true"
                title="Browse for repo directory"
              >
                📁 Browse
              </button>
            </div>
            <span class="nwf-hint">
              The absolute path of the repo you want to automate. Claude reads
              its code, git history and README to understand the context.
              <b>📁 Browse</b> picks from your recent cwds or straight off disk.
            </span>
          </label>

          <label class="nwf-label">
            <span>One-sentence requirement</span>
            <textarea
              v-model="requirement"
              rows="5"
              class="nwf-textarea"
              placeholder="Pull the last N days of submissions from the feedback inbox, review each one, make and commit + push any code changes they call for, then notify me."
            ></textarea>
            <span class="nwf-hint">
              The more natural the better. Stages, params, outputs and poll-vs-claude
              are all the agent's call. Describe what ONE run does —
              <b>how often it runs and what triggers it</b> belongs in Schedule,
              not in the requirement.
            </span>
          </label>

          <label class="nwf-label">
            <span>Workflow name (optional)</span>
            <input
              v-model="workflowName"
              placeholder="leave blank and the agent names it"
              class="nwf-input"
            />
            <span class="nwf-hint">
              Must match <code>[a-z][a-z0-9_]*</code>. Left blank, the agent
              picks a name that reflects the intent.
            </span>
          </label>

          <div class="nwf-actions">
            <button class="btn-primary" @click="submitForm"
                    :disabled="!repoPath.trim() || !requirement.trim()">
              Next: let the agent ask its questions
            </button>
            <button class="btn-secondary" @click="emit('close')">Cancel</button>
          </div>
        </div>

        <!-- ================= Clarifying spinner ================= -->
        <div v-else-if="step === 'clarifying'" class="nwf-progress">
          <div class="nwf-spinner">⏳</div>
          <div class="nwf-progress-label">{{ progressLabel }}</div>
          <div class="nwf-progress-time">{{ elapsedSec }}s elapsed (usually 15-120s, hard timeout at 180s)</div>
          <div class="nwf-progress-hint">
            The backend is running <code>claude -p …</code>; the agent needs at
            most 3 tool calls to come back with its questions. On timeout it
            falls back to generating the YAML directly.
          </div>
        </div>

        <!-- ================= Step 2: clarify ================= -->
        <div v-else-if="step === 'clarify' && clarifyResult">
          <div class="nwf-clarify-preview">
            <div class="nwf-clarify-eyebrow">What the agent understood</div>
            <div class="nwf-clarify-summary">
              {{ clarifyResult.stage_preview || '(no description)' }}
            </div>
          </div>

          <!-- Stage skeleton preview: user can rename/delete/add/reorder
               before generation locks in the decomposition. If the agent
               didn't propose a skeleton (legacy path), this whole block
               is hidden and the user goes straight to boundary questions. -->
          <div v-if="editableStages.length > 0" class="nwf-stage-preview">
            <div class="nwf-stage-head">
              <div>
                <div class="nwf-clarify-eyebrow">Stage skeleton (the agent's proposal — editable)</div>
                <div class="nwf-stage-sub">
                  If it looks right, move on to the scoping questions below. If
                  it doesn't, rename / delete / add / reorder here first — that
                  saves you generating a whole workflow before discovering the
                  breakdown was wrong.
                </div>
              </div>
              <button
                type="button"
                class="nwf-stage-add"
                @click="addStage"
                title="Add a stage"
              >+ Stage</button>
            </div>
            <div v-if="!stagesValid" class="nwf-stage-err">
              ⚠ The skeleton isn't valid: name must match <code>[a-z][a-z0-9_]*</code>,
              kind ∈ claude|poll, purpose must be non-empty, names must be unique,
              and there can be at most 8 stages.
            </div>
            <ul class="nwf-stage-list">
              <li v-for="(s, si) in editableStages" :key="si" class="nwf-stage-item">
                <div class="nwf-stage-num">{{ si + 1 }}</div>
                <input
                  v-model="s.name"
                  class="nwf-stage-name"
                  placeholder="stage_name"
                  @input="markStagesTouched"
                />
                <select
                  v-model="s.kind"
                  class="nwf-stage-kind"
                  @change="markStagesTouched"
                >
                  <option value="claude">claude</option>
                  <option value="poll">poll</option>
                </select>
                <input
                  v-model="s.purpose"
                  class="nwf-stage-purpose"
                  placeholder="what it does, in one line"
                  @input="markStagesTouched"
                />
                <div class="nwf-stage-actions">
                  <button
                    type="button"
                    class="nwf-stage-btn"
                    :disabled="si === 0"
                    @click="moveStage(si, -1)"
                    title="Move up"
                  >↑</button>
                  <button
                    type="button"
                    class="nwf-stage-btn"
                    :disabled="si === editableStages.length - 1"
                    @click="moveStage(si, 1)"
                    title="Move down"
                  >↓</button>
                  <button
                    type="button"
                    class="nwf-stage-btn nwf-stage-del"
                    @click="removeStage(si)"
                    title="Delete this stage"
                  >✕</button>
                </div>
              </li>
            </ul>
          </div>

          <p
            v-if="clarifyResult.questions.length > 0"
            class="nwf-intro"
            style="margin-top: 12px;"
          >
            Answer the <b>{{ clarifyResult.questions.length }}</b> scoping
            questions below. The agent's recommendation is pre-selected on each
            one, so scrolling to the bottom and hitting
            <b>Accept defaults + generate</b> means letting it decide all of them.
          </p>

          <div v-if="clarifyResult.questions.length > 0" class="nwf-questions">
            <div v-for="(q, qi) in clarifyResult.questions" :key="q.id" class="nwf-question">
              <div class="nwf-q-title">
                <span class="nwf-q-num">Q{{ qi + 1 }}.</span>
                {{ q.text }}
              </div>
              <div class="nwf-q-options">
                <label
                  v-for="opt in q.options"
                  :key="opt.value"
                  class="nwf-q-option"
                  :class="{ selected: answers[q.id] === opt.value }"
                >
                  <input
                    type="radio"
                    :name="'q-' + q.id"
                    :value="opt.value"
                    v-model="answers[q.id]"
                  />
                  <span>{{ opt.label }}</span>
                  <span v-if="opt.recommended" class="nwf-q-rec">recommended</span>
                </label>
              </div>
              <textarea
                v-model="freeText[q.id]"
                class="nwf-q-freetext"
                rows="2"
                placeholder="anything to add (optional)"
              ></textarea>
            </div>
          </div>

          <div class="nwf-actions">
            <button
              class="btn-primary"
              :disabled="!stagesValid"
              @click="submitClarify(false)"
              :title="!stagesValid ? 'Fix the stage skeleton first' : ''"
            >
              {{ clarifyResult.questions.length > 0 ? 'Generate YAML from my answers' : 'Generate YAML from this skeleton' }}
            </button>
            <button
              v-if="clarifyResult.questions.length > 0"
              class="btn-secondary"
              :disabled="!stagesValid"
              @click="submitClarify(true)"
            >
              Accept defaults + generate
            </button>
            <button class="btn-secondary" @click="step = 'form'">
              ← Back to the requirement
            </button>
          </div>
        </div>

        <!-- ================= Generating spinner ================= -->
        <div v-else-if="step === 'generating'" class="nwf-progress">
          <div class="nwf-spinner">⏳</div>
          <div class="nwf-progress-label">{{ progressLabel }}</div>
          <div class="nwf-progress-time">{{ elapsedSec }}s elapsed (usually 60-300s)</div>
          <div class="nwf-progress-hint">
            The backend is running <code>claude -p …</code> to write the YAML
            and self-check it. You can leave this window open.
          </div>
        </div>

        <!-- ================= Step 3: result ================= -->
        <div v-else-if="step === 'result' && result" class="nwf-result">
          <div v-if="!result.workflow_id" class="nwf-error-card">
            <h4>❌ Generation failed</h4>
            <p class="nwf-error-msg">{{ result.error || 'unknown error' }}</p>
            <details v-if="result.stdout_tail">
              <summary>Tail of Claude's stdout (for debugging)</summary>
              <pre class="nwf-stdout">{{ result.stdout_tail }}</pre>
            </details>
            <div class="nwf-actions">
              <button class="btn-secondary" @click="onDiscard">Try again</button>
              <button class="btn-secondary" @click="emit('close')">Close</button>
            </div>
          </div>

          <div v-else>
            <div class="nwf-verdict-card"
                 :class="failCount > 0 ? 'v-fail' : warnCount > 0 ? 'v-warn' : 'v-pass'">
              <div class="nwf-verdict-header">
                <span class="nwf-verdict-icon">
                  {{ failCount > 0 ? '❌' : warnCount > 0 ? '⚠️' : '✅' }}
                </span>
                <div>
                  <div class="nwf-verdict-title">
                    <code>{{ result.workflow_name }}</code>
                    —
                    {{
                      failCount > 0
                        ? `${failCount} fail, ${warnCount} warn`
                        : warnCount > 0
                          ? `passed with ${warnCount} warn`
                          : 'passed cleanly'
                    }}
                  </div>
                  <div class="nwf-verdict-sub">
                    R9-R19: {{ passCount }} pass · {{ warnCount }} warn · {{ failCount }} fail
                    · generated in {{ result.duration_sec }}s
                  </div>
                </div>
              </div>
              <div class="nwf-yaml-path">
                📄 <code>{{ result.yaml_path }}</code>
              </div>
            </div>

            <div v-if="nonPassRules.length" class="nwf-rules">
              <h5>Rules that need attention</h5>
              <ul>
                <li v-for="r in nonPassRules" :key="r.rule_id"
                    :class="'rule-' + r.status">
                  <b>{{ r.rule_id }}</b>
                  <span class="nwf-rule-status">{{ r.status }}</span>
                  <div class="nwf-rule-reason">{{ r.reason }}</div>
                </li>
              </ul>
            </div>

            <!-- Pass-2 semantic review: 5 categories that R9-R19 can't see. -->
            <div
              v-if="semanticVerdicts.length || semanticError"
              class="nwf-rules nwf-semantic"
            >
              <h5>
                Semantic review (Pass 2)
                <span
                  v-if="semanticIssueCount > 0"
                  class="nwf-sem-count nwf-sem-warn"
                >{{ semanticIssueCount }} to address</span>
                <span
                  v-else-if="semanticVerdicts.length"
                  class="nwf-sem-count nwf-sem-ok"
                >all passed</span>
              </h5>
              <div v-if="semanticError" class="nwf-rule-reason" style="padding: 4px 8px;">
                The Pass-2 semantic review did not complete: {{ semanticError }}
              </div>
              <ul v-else>
                <li
                  v-for="v in semanticVerdicts"
                  :key="v.category"
                  :class="'rule-' + v.status"
                >
                  <b>{{ semanticIcon(v.status) }} {{ semanticLabel(v.category) }}</b>
                  <span class="nwf-rule-status">{{ v.status }}</span>
                  <div class="nwf-rule-reason">{{ v.reason }}</div>
                </li>
              </ul>
            </div>

            <div class="nwf-actions">
              <button
                class="btn-primary"
                :disabled="failCount > 0"
                @click="onLaunch"
                :title="failCount > 0 ? 'Fix the failing rules before launching' : 'Launch mission'"
              >
                🚀 Launch mission
              </button>
              <button
                v-if="failCount > 0 || warnCount > 0 || semanticIssueCount > 0"
                class="btn-secondary"
                @click="onFixWarns"
                title="Fixes via the edit-with-agent path, which preserves the skeleton"
              >
                🔧 Ask the agent to fix it ({{ failCount + warnCount }} structural + {{ semanticIssueCount }} semantic)
              </button>
              <button class="btn-secondary" @click="onDiscard">Discard and restart</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <FilePicker
      :open="showRepoPicker"
      mode="dir"
      title="Pick target repo directory"
      :initial-path="repoPath"
      @close="showRepoPicker = false"
      @pick="onPickRepo"
    />
  </div>
</template>

<style scoped>
.nwf-modal {
  max-width: 760px; width: 92%; max-height: 90vh;
  padding: 0; overflow-y: auto;
}
.nwf-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--border);
}
.nwf-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute); margin-bottom: 3px;
}
.nwf-header h3 { margin: 0; font-size: 17px; color: var(--ink); }
.close-btn {
  background: transparent; border: none; font-size: 22px; padding: 0 6px;
  color: var(--ink-mute); cursor: pointer; box-shadow: none;
}
.close-btn:hover { color: var(--ink); transform: none; }

/* ---- Stepper ---- */
.nwf-stepper {
  display: flex; align-items: center;
  padding: 10px 18px;
  background: var(--canvas);
  border-bottom: 1px solid var(--border);
  font-size: 11.5px;
}
.nwf-step {
  padding: 3px 10px; border-radius: 10px;
  color: var(--ink-mute);
  transition: all 200ms var(--ease-soft);
}
.nwf-step.active {
  background: var(--ink); color: var(--card); font-weight: 500;
}
.nwf-step.done { color: var(--pastel-green-fg); }
.nwf-step-line {
  flex: 1; height: 1px; background: var(--border); margin: 0 6px;
}

.nwf-body { padding: 14px 18px 18px; font-size: 13px; line-height: 1.55; }
.nwf-intro {
  margin: 0 0 12px 0;
  padding: 10px 12px;
  background: var(--canvas);
  border-left: 3px solid var(--ink);
  border-radius: 4px;
  color: var(--ink-2);
  font-size: 12.5px;
}

.nwf-label { display: block; margin-bottom: 12px; }
.nwf-label > span:first-child {
  display: block; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--ink-mute);
  margin-bottom: 5px; font-weight: 600;
}
.nwf-input, .nwf-textarea {
  width: 100%; box-sizing: border-box; font-family: inherit;
}
.nwf-textarea { resize: vertical; }

.nwf-input-row {
  display: flex; gap: 6px; align-items: stretch;
}
.nwf-input-row .nwf-input { flex: 1; }
.nwf-browse-btn {
  white-space: nowrap;
}
.nwf-hint {
  display: block; margin-top: 4px; font-size: 11.5px;
  color: var(--ink-mute);
}

.nwf-actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
.btn-primary {
  background: var(--ink); color: var(--card); border-color: var(--ink);
  font-weight: 500;
}
.btn-primary:disabled { opacity: 0.5; }
.btn-secondary { /* uses global button default */ }

/* ---- Progress ---- */
.nwf-progress { text-align: center; padding: 36px 20px; }
.nwf-spinner { font-size: 30px; opacity: 0.6; animation: nwf-spin 2s linear infinite; }
@keyframes nwf-spin { to { transform: rotate(360deg); } }
.nwf-progress-label { font-size: 14px; font-weight: 500; margin: 12px 0 6px; color: var(--ink); }
.nwf-progress-time { color: var(--ink-mute); font-size: 12px; }
.nwf-progress-hint { color: var(--ink-mute); font-size: 11.5px; margin-top: 10px; }

/* ---- Clarify preview ---- */
.nwf-clarify-preview {
  padding: 10px 12px;
  background: var(--pastel-blue-bg);
  border-left: 3px solid var(--pastel-blue-fg);
  border-radius: 4px;
  margin-bottom: 12px;
}
.nwf-clarify-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.6px;
  color: var(--pastel-blue-fg); opacity: 0.8; margin-bottom: 3px;
}
.nwf-clarify-summary { font-size: 12.5px; color: var(--pastel-blue-fg); }

.nwf-questions { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
.nwf-question {
  padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 5px;
  background: var(--canvas);
}
.nwf-q-title { font-size: 13px; font-weight: 500; margin-bottom: 6px; color: var(--ink); }
.nwf-q-num { color: var(--ink-mute); margin-right: 4px; font-weight: 600; }
.nwf-q-options { display: flex; flex-direction: column; gap: 4px; }
.nwf-q-option {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 10px; border-radius: 4px; cursor: pointer;
  font-size: 12.5px; border: 1px solid transparent;
  transition: all 120ms var(--ease-soft);
  background: var(--card);
}
.nwf-q-option:hover { background: var(--canvas); }
.nwf-q-option.selected {
  background: var(--pastel-blue-bg);
  border-color: var(--pastel-blue-fg);
}
.nwf-q-option input[type="radio"] { margin: 0; }
.nwf-q-rec {
  font-size: 10px; padding: 1px 5px; border-radius: 3px;
  background: var(--pastel-blue-fg); color: var(--card);
  margin-left: auto; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.nwf-q-freetext {
  width: 100%; margin-top: 6px;
  font-size: 12px; font-family: inherit; box-sizing: border-box;
  resize: vertical;
}

/* ---- Result ---- */
.nwf-error-card {
  padding: 12px 14px;
  background: var(--pastel-red-bg);
  border-left: 3px solid var(--pastel-red-fg);
  border-radius: 4px;
}
.nwf-error-card h4 { margin: 0 0 6px; color: var(--pastel-red-fg); font-size: 13px; }
.nwf-error-msg { color: var(--pastel-red-fg); white-space: pre-wrap; font-size: 12.5px; }
.nwf-stdout {
  font-family: 'Geist Mono', 'SF Mono', monospace; font-size: 11px;
  background: var(--canvas); color: var(--ink);
  border: 1px solid var(--border);
  padding: 8px 10px; border-radius: 4px;
  overflow-x: auto; max-height: 260px;
}

.nwf-verdict-card {
  padding: 12px 14px; border-radius: 5px; margin-bottom: 10px;
  border-left-width: 3px; border-left-style: solid;
}
.v-pass { background: var(--pastel-green-bg); border-left-color: var(--pastel-green-fg); }
.v-warn { background: var(--pastel-yellow-bg); border-left-color: var(--pastel-yellow-fg); }
.v-fail { background: var(--pastel-red-bg); border-left-color: var(--pastel-red-fg); }
.nwf-verdict-header { display: flex; gap: 10px; align-items: flex-start; }
.nwf-verdict-icon { font-size: 22px; }
.nwf-verdict-title { font-size: 13.5px; font-weight: 500; }
.v-pass .nwf-verdict-title { color: var(--pastel-green-fg); }
.v-warn .nwf-verdict-title { color: var(--pastel-yellow-fg); }
.v-fail .nwf-verdict-title { color: var(--pastel-red-fg); }
.nwf-verdict-sub { font-size: 11.5px; color: var(--ink-mute); margin-top: 3px; }
.nwf-yaml-path { margin-top: 8px; font-size: 11.5px; padding-left: 32px; color: var(--ink-mute); }

.nwf-rules { margin-top: 10px; }
.nwf-rules h5 {
  margin: 0 0 6px; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--ink-mute); font-weight: 600;
}
.nwf-rules ul { list-style: none; padding: 0; margin: 0; }
.nwf-rules li {
  padding: 6px 10px; margin: 4px 0; border-radius: 4px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-left-width: 3px; border-left-style: solid;
  border-left-color: transparent;
  font-size: 12.5px;
}
/* Verdict border-colors — scoped under .nwf-rules so their specificity
 * (0,2,1) beats the base `.nwf-rules li` (0,1,1) without `!important`. */
.nwf-rules li.rule-fail { border-left-color: var(--pastel-red-fg); }
.nwf-rules li.rule-warn { border-left-color: var(--pastel-yellow-fg); }
.nwf-rule-status {
  display: inline-block; margin-left: 8px; font-size: 10.5px;
  text-transform: uppercase; padding: 1px 5px; border-radius: 3px;
  background: var(--border); color: var(--ink-mute);
}
.nwf-rule-reason { font-size: 12px; margin-top: 3px; color: var(--ink-2); }

/* rule-pass border for semantic verdicts (rule-fail/warn already styled above) */
.nwf-rules li.rule-pass { border-left-color: var(--pastel-green-fg, #6b8e59); }

/* Semantic review card — visually distinct from R9-R19 rules */
.nwf-semantic { margin-top: 14px; padding-top: 10px; border-top: 1px dashed var(--border); }
.nwf-sem-count {
  font-size: 10.5px; margin-left: 8px; padding: 1px 6px; border-radius: 3px;
  text-transform: none; letter-spacing: 0;
}
.nwf-sem-warn { background: var(--pastel-yellow-bg); color: var(--pastel-yellow-fg); }
.nwf-sem-ok { background: var(--pastel-green-bg, #eaf3e0); color: var(--pastel-green-fg, #6b8e59); }

/* Stage preview card — sits between the "What the agent understood" summary and the
   boundary questions. User can rename / delete / add / reorder rows to
   correct the skeleton BEFORE generation locks it in. */
.nwf-stage-preview {
  margin: 10px 0 14px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--canvas);
}
.nwf-stage-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.nwf-stage-sub { font-size: 11.5px; color: var(--ink-mute); margin-top: 2px; line-height: 1.5; }
.nwf-stage-add {
  padding: 4px 10px; font-size: 11.5px; border: 1px dashed var(--ink-mute);
  background: transparent; color: var(--ink); border-radius: 4px; cursor: pointer;
  white-space: nowrap;
}
.nwf-stage-add:hover { background: var(--card); }
.nwf-stage-err {
  margin: 6px 0; padding: 6px 10px; font-size: 11.5px;
  background: var(--pastel-red-bg); color: var(--pastel-red-fg);
  border-radius: 4px;
}
.nwf-stage-list { list-style: none; padding: 0; margin: 0; }
.nwf-stage-item {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) 80px minmax(0, 2fr) auto;
  align-items: center; gap: 6px;
  padding: 4px 0; border-top: 1px dashed var(--border);
}
.nwf-stage-item:first-child { border-top: 0; }
.nwf-stage-num { font-size: 11px; color: var(--ink-mute); text-align: center; }
.nwf-stage-name, .nwf-stage-purpose {
  padding: 3px 6px; font-size: 12.5px; font-family: 'Geist Mono', monospace;
  border: 1px solid transparent; background: transparent; color: var(--ink);
  border-radius: 3px; min-width: 0;
}
.nwf-stage-purpose { font-family: inherit; font-size: 12px; }
.nwf-stage-name:focus, .nwf-stage-purpose:focus {
  border-color: var(--ink-mute); background: var(--card); outline: none;
}
.nwf-stage-kind {
  padding: 3px 4px; font-size: 11px; font-family: 'Geist Mono', monospace;
  background: var(--card); color: var(--ink); border: 1px solid var(--border);
  border-radius: 3px;
}
.nwf-stage-actions { display: flex; gap: 3px; }
.nwf-stage-btn {
  padding: 1px 6px; font-size: 11px; background: transparent;
  border: 1px solid var(--border); color: var(--ink-mute);
  border-radius: 3px; cursor: pointer;
}
.nwf-stage-btn:hover:not(:disabled) { color: var(--ink); border-color: var(--ink-mute); }
.nwf-stage-btn:disabled { opacity: 0.3; cursor: not-allowed; }
.nwf-stage-del:hover { color: var(--pastel-red-fg); border-color: var(--pastel-red-fg); }

code {
  background: var(--canvas); border: 1px solid var(--border);
  padding: 0 5px; border-radius: 3px;
  font-family: 'Geist Mono', 'SF Mono', monospace; font-size: 12px;
}
</style>
