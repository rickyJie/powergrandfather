<script setup lang="ts">
/**
 * AgentEditModal — natural-language iteration on an existing workflow.
 *
 * User writes a sentence of feedback ("make min_chars smaller",
 * "insert a dry-run branch before commit_push", ...). Backend spawns
 * claude which reads the current YAML, applies the feedback, and writes
 * a new version. Result view shows the review verdict — user can accept
 * (re-fetch parent detail) or discard (nothing changes since we're just
 * closing).
 */
import { apiErrorDetail, apiErrorMessage } from '../../lib/apiError'
import { reviewRuleCounts, reviewRules } from '../../api/automation'
import { computed, ref, watch } from 'vue'
import { automationApi } from '../../api/automation'
import { useToast } from '../../composables/useToast'

const toast = useToast()

const props = defineProps<{
  open: boolean
  workflowName: string | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  /** Emitted after successful edit; parent should re-fetch workflow detail. */
  (e: 'edited'): void
}>()

const feedback = ref('')
const submitting = ref(false)
const elapsedSec = ref(0)
let elapsedTimer: number | null = null
type Result = Awaited<ReturnType<typeof automationApi.editWithAgent>>
const result = ref<Result | null>(null)

watch(() => props.open, (isOpen) => {
  if (isOpen) {
    feedback.value = ''
    submitting.value = false
    result.value = null
    elapsedSec.value = 0
  } else {
    if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null }
  }
})

async function submit() {
  if (!props.workflowName || !feedback.value.trim()) return
  submitting.value = true
  result.value = null
  elapsedSec.value = 0
  if (elapsedTimer !== null) clearInterval(elapsedTimer)
  elapsedTimer = window.setInterval(() => { elapsedSec.value += 1 }, 1000)
  try {
    const res = await automationApi.editWithAgent(
      props.workflowName,
      feedback.value.trim(),
    )
    result.value = res
  } catch (e) {
    // Raw detail, not the message: the backend puts the whole Result here
    // on failure so review_report + stdout_tail survive the 500.
    const detail = apiErrorDetail(e)
    result.value = (detail && typeof detail === 'object') ? detail as Result : {
      workflow_id: null,
      workflow_name: props.workflowName,
      yaml_path: null,
      review_status: 'generation_failed',
      review_report: null,
      stdout_tail: '',
      duration_sec: 0,
      error: apiErrorMessage(e),
    } as Result
  } finally {
    if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null }
    submitting.value = false
  }
}

const counts = computed(() => reviewRuleCounts(result.value?.review_report))
const failCount = computed(() => counts.value.fail)
const warnCount = computed(() => counts.value.warn)
const passCount = computed(() => counts.value.pass)
const nonPassRules = computed(() =>
  reviewRules(result.value?.review_report).filter((r) => r.status !== 'pass'),
)

function onAccept() {
  toast.success(`Workflow ${result.value?.workflow_name} updated`)
  emit('edited')
  emit('close')
}
function onIterate() {
  // Keep the feedback (user might want to add more), reset only result.
  result.value = null
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal panel aem-modal" role="dialog" aria-modal="true" aria-label="Edit workflow with agent">
      <div class="aem-header">
        <div>
          <div class="aem-eyebrow">Agent edit</div>
          <h3 class="serif aem-title">Have an agent edit <code>{{ workflowName }}</code></h3>
        </div>
        <button class="aem-close" @click="emit('close')" aria-label="Close">×</button>
      </div>

      <div class="aem-body">
        <!-- Form -->
        <div v-if="!submitting && !result">
          <div class="aem-intro">
            Describe the change in a sentence. The agent reads the current
            YAML, applies your feedback, writes it back to the same path and
            re-runs the R9-R19 self-check. <b>It changes only what you asked
            for</b> — every other stage is left alone.
          </div>
          <div class="aem-field">
            <span class="aem-label">What to change</span>
            <textarea
              v-model="feedback"
              rows="5"
              placeholder="e.g. drop review_each's min_chars to 0; add a dry_run branch before commit_push; change fetch_feedback's days_back default to 14."
              autofocus
            ></textarea>
            <div class="aem-hint">The more specific the better — name stages, fields, thresholds or params.</div>
          </div>
          <div class="aem-actions">
            <button class="primary" :disabled="!feedback.trim()" @click="submit">
              Have the agent edit it
            </button>
            <button @click="emit('close')">Cancel</button>
          </div>
        </div>

        <!-- Progress -->
        <div v-else-if="submitting" class="aem-progress">
          <div class="aem-progress-label">The agent is reading the YAML, editing it and re-running the self-check…</div>
          <div class="aem-progress-time">{{ elapsedSec }}s elapsed (usually 30-120s)</div>
        </div>

        <!-- Result -->
        <div v-else-if="result" class="aem-result">
          <div v-if="!result.workflow_id" class="aem-error-card">
            <div class="aem-verdict-eyebrow">Edit failed</div>
            <div class="aem-error-msg">{{ result.error || 'unknown error' }}</div>
            <details v-if="result.stdout_tail" class="aem-detail">
              <summary>Tail of Claude's stdout</summary>
              <pre class="aem-stdout">{{ result.stdout_tail }}</pre>
            </details>
            <div class="aem-actions">
              <button @click="onIterate">Rewrite the feedback and retry</button>
              <button @click="emit('close')">Close</button>
            </div>
          </div>

          <div v-else>
            <div class="aem-verdict-card"
                 :class="failCount > 0 ? 'v-fail' : warnCount > 0 ? 'v-warn' : 'v-pass'">
              <div class="aem-verdict-eyebrow">
                {{ failCount > 0 ? 'fail' : warnCount > 0 ? 'warn' : 'passed' }}
              </div>
              <div class="aem-verdict-title">
                <code>{{ result.workflow_name }}</code>
                — R9-R19: {{ passCount }} pass · {{ warnCount }} warn · {{ failCount }} fail
              </div>
              <div class="aem-verdict-sub">took {{ result.duration_sec }}s</div>
            </div>

            <div v-if="nonPassRules.length" class="aem-rules">
              <div class="aem-label">Rules that need attention</div>
              <ul>
                <li v-for="r in nonPassRules" :key="r.rule_id" :class="'rule-' + r.status">
                  <b>{{ r.rule_id }}</b>
                  <span class="aem-rule-status">{{ r.status }}</span>
                  <div class="aem-rule-reason">{{ r.reason }}</div>
                </li>
              </ul>
            </div>

            <div class="aem-actions">
              <button class="primary" @click="onAccept">Accept this version</button>
              <button @click="onIterate">Edit again</button>
              <button @click="emit('close')">Close</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.aem-modal {
  max-width: 720px; width: 92%; max-height: 90vh;
  padding: 0; overflow-y: auto;
}
.aem-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--border);
}
.aem-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--ink-mute); margin-bottom: 3px;
}
.aem-title { margin: 0; font-size: 17px; color: var(--ink); }
.aem-close {
  background: transparent; border: none; font-size: 22px; padding: 0 6px;
  color: var(--ink-mute); cursor: pointer; box-shadow: none;
}
.aem-close:hover { color: var(--ink); transform: none; }

.aem-body { padding: 14px 18px 18px; font-size: 13px; }

.aem-intro {
  padding: 10px 12px; margin-bottom: 12px;
  background: var(--canvas);
  border-left: 3px solid var(--ink);
  border-radius: 4px;
  font-size: 12.5px; color: var(--ink-2);
}

.aem-field { margin-bottom: 10px; }
.aem-label {
  display: block; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--ink-mute);
  margin-bottom: 5px; font-weight: 600;
}
.aem-field textarea {
  width: 100%; font-family: inherit; box-sizing: border-box;
  resize: vertical;
}
.aem-hint {
  margin-top: 4px; font-size: 11.5px; color: var(--ink-mute);
}

.aem-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }

.aem-progress { text-align: center; padding: 40px 20px; }
.aem-progress-label { font-size: 14px; font-weight: 500; color: var(--ink); margin-bottom: 6px; }
.aem-progress-time { color: var(--ink-mute); font-size: 12px; }

.aem-verdict-card {
  padding: 10px 14px; border-radius: 5px; margin-bottom: 10px;
  border-left-width: 3px; border-left-style: solid;
}
.aem-verdict-eyebrow {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
  font-weight: 600; margin-bottom: 4px;
}
.aem-verdict-title { font-size: 13.5px; font-weight: 500; }
.aem-verdict-sub { font-size: 11.5px; color: var(--ink-mute); margin-top: 3px; }
.v-pass {
  background: var(--pastel-green-bg); border-left-color: var(--pastel-green-fg);
}
.v-pass .aem-verdict-eyebrow, .v-pass .aem-verdict-title { color: var(--pastel-green-fg); }
.v-warn {
  background: var(--pastel-yellow-bg); border-left-color: var(--pastel-yellow-fg);
}
.v-warn .aem-verdict-eyebrow, .v-warn .aem-verdict-title { color: var(--pastel-yellow-fg); }
.v-fail {
  background: var(--pastel-red-bg); border-left-color: var(--pastel-red-fg);
}
.v-fail .aem-verdict-eyebrow, .v-fail .aem-verdict-title { color: var(--pastel-red-fg); }

.aem-error-card {
  padding: 12px 14px; background: var(--pastel-red-bg);
  border-left: 3px solid var(--pastel-red-fg); border-radius: 4px;
}
.aem-error-card .aem-verdict-eyebrow { color: var(--pastel-red-fg); }
.aem-error-msg { color: var(--pastel-red-fg); white-space: pre-wrap; font-size: 12.5px; }
.aem-detail { margin-top: 8px; }
.aem-detail summary { cursor: pointer; font-size: 11.5px; color: var(--ink-mute); }
.aem-stdout {
  font-family: 'Geist Mono', 'SF Mono', monospace; font-size: 11px;
  background: var(--canvas); color: var(--ink);
  padding: 8px 10px; border-radius: 4px;
  border: 1px solid var(--border);
  overflow-x: auto; max-height: 240px;
}

.aem-rules { margin-top: 10px; margin-bottom: 8px; }
.aem-rules ul { list-style: none; padding: 0; margin: 0; }
.aem-rules li {
  padding: 6px 10px; margin: 5px 0; border-radius: 4px;
  background: var(--canvas);
  border: 1px solid var(--border);
  border-left-width: 3px; border-left-style: solid;
  border-left-color: transparent;
  font-size: 12.5px;
}
/* Scoped under .aem-rules so specificity (0,2,1) beats base
 * `.aem-rules li` (0,1,1) without needing !important. */
.aem-rules li.rule-fail { border-left-color: var(--pastel-red-fg); }
.aem-rules li.rule-warn { border-left-color: var(--pastel-yellow-fg); }
.aem-rule-status {
  display: inline-block; margin-left: 8px; font-size: 10.5px;
  text-transform: uppercase; padding: 1px 6px; border-radius: 3px;
  background: var(--border); color: var(--ink-mute);
}
.aem-rule-reason { font-size: 12px; margin-top: 3px; color: var(--ink-2); }

code {
  background: var(--canvas); border: 1px solid var(--border);
  padding: 0 5px; border-radius: 3px;
  font-family: 'Geist Mono', 'SF Mono', monospace; font-size: 12px;
}
</style>
