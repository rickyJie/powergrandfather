/**
 * `useCreateSessionForm()` — form state + submit for the "New session" dialog.
 *
 * Extracted from Sessions.vue (M9.4) so the dialog's ~250 LOC of form
 * plumbing live in a testable composable and the view stays about
 * orchestration only.
 *
 * Contract:
 *   - Owns local reactive state for every field in the dialog.
 *   - Owns the "argv default follows selected agent unless user edited"
 *     rule via `argvDirty`.
 *   - Exposes `submit(handler)` — handler receives the built payload
 *     (typed) and returns the created SessionRow on success.
 *   - Exposes `reset()` for post-cancel / post-submit teardown.
 *   - Does NOT own the modal open/close state — that stays in the view.
 *
 * All adapter-name branching lives INSIDE useEffectiveAgent + useBackend
 * (which both return null when metadata isn't loaded, so this composable
 * has no fallback strings hardcoded).
 */
import { computed, ref, watch } from 'vue'
import { useBackend } from './useBackend'
import { useEffectiveAgent } from './useEffectiveAgent'
import type { CreateSessionPayload, SessionRow } from '../api/sessions'

export function useCreateSessionForm() {
  // --- basic fields ---
  const cwd = ref('/tmp')
  const title = ref('')
  const prompt = ref('')
  const sessionProjectId = ref<string>('')

  // --- agent + argv (schema-driven) ---
  const explicitAgent = ref<string | null>(null)  // null = follow user default
  const argv = ref('')
  const argvDirty = ref(false)
  const effectiveAgent = useEffectiveAgent(explicitAgent)
  const backend = useBackend(effectiveAgent)

  // When the effective agent changes AND the user hasn't manually edited
  // argv, reset argv to the backend-declared default. Guards `argv`
  // against being clobbered mid-edit. `default_argv` comes straight
  // from `Backend.default_argv` (schema-driven), so gemini or any
  // future adapter automatically works — no _argvForAgent switch.
  watch(
    [effectiveAgent, backend],
    () => {
      if (argvDirty.value) return
      if (backend.value?.default_argv) argv.value = backend.value.default_argv
    },
    { immediate: true },
  )

  function markArgvDirty() { argvDirty.value = true }
  function setExplicitAgent(name: string | null) {
    explicitAgent.value = name
    // Any explicit agent change resets the argv-dirty flag so the new
    // agent's default_argv takes effect — user chose a fresh path.
    argvDirty.value = false
  }

  const submitting = ref(false)

  function _buildPayload(): CreateSessionPayload {
    const parts = argv.value.trim() ? argv.value.trim().split(/\s+/) : undefined
    return {
      cwd: cwd.value,
      title: title.value || undefined,
      type: 'interactive',
      argv: parts,
      session_project_id: sessionProjectId.value || undefined,
      agent: explicitAgent.value || undefined,
      initial_prompt: prompt.value || undefined,
    }
  }

  async function submit(
    handler: (payload: CreateSessionPayload) => Promise<SessionRow>,
  ): Promise<SessionRow | null> {
    if (!cwd.value || submitting.value) return null
    submitting.value = true
    try {
      return await handler(_buildPayload())
    } finally {
      submitting.value = false
    }
  }

  function reset() {
    title.value = ''
    prompt.value = ''
    argvDirty.value = false
    // cwd / project / agent kept — sensible for "create another".
  }

  const canSubmit = computed(() => !!cwd.value && !submitting.value)

  return {
    // fields
    cwd, title, prompt, sessionProjectId,
    explicitAgent, argv, argvDirty,
    // derived
    effectiveAgent, backend, canSubmit, submitting,
    // ops
    setExplicitAgent, markArgvDirty, submit, reset,
  }
}
