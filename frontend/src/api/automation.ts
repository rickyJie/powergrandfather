import { http } from './client'

/*
 * Wire types for the automation module.
 *
 * These mirror the backend's hand-written serializers — the endpoints declare
 * no `response_model`, so these declarations ARE the contract and there is no
 * generator keeping them honest. Each type below names the function it was
 * derived from; when you change that function, change the type in the same
 * commit.
 *
 * Enum-valued fields serialize as `Enum.value` (lowercase) — see README
 * "API wire format reference".
 */

/**
 * Envelope shared by this module's list endpoints.
 *
 * `count` is optional because `GET /api/missions` (`missions.py::list_missions`)
 * returns `{items}` alone while `/api/workflows`, `/api/runs` and
 * `/api/schedules` include it. Every current caller reads `.items`, so making
 * it required only bought a type that lied.
 */
export type ListResponse<T> = { count?: number; items: T[] }

// ---- Review reports (workflow authoring) --------------------------------

/** Structural pass R9-R19 — `workflow/reviewer.py` `RuleVerdict.to_dict`. */
export type RuleVerdict = {
  rule_id: string // "R9" … "R19"
  status: 'pass' | 'warn' | 'fail'
  reason: string // empty on pass
}

/** Semantic pass-2 — `authoring/semantic_reviewer.py` `SemanticVerdict.to_dict`. */
export type SemanticVerdict = {
  category: string
  status: 'pass' | 'warn' | 'fail'
  reason: string
}

/** `SemanticReviewResult.to_dict`, or the crash stub the generator substitutes. */
export type SemanticVerdicts = {
  verdicts: SemanticVerdict[]
  error: string | null
  duration_sec: number
}

/**
 * `workflow/reviewer.py` `ReviewResult.to_dict`, plus the `semantic_verdicts`
 * key the generator splices in afterwards (absent when pass-2 never ran, which
 * is the case for every workflow authored before it shipped).
 */
export type WorkflowReviewReport = {
  status: 'passed' | 'rejected'
  rules: RuleVerdict[]
  semantic_verdicts?: SemanticVerdicts
}

/**
 * What `review_report` can actually be.
 *
 * When the emitted YAML doesn't even parse, `authoring/generator.py:281`
 * short-circuits before `review_workflow` runs and returns
 * `{"error": "schema: …"}` — no `status`, no `rules`. `api/workflows.py`
 * passes that through verbatim, so a consumer that assumes `.rules` exists
 * reads `undefined` on exactly the failure the user most needs explained.
 */
export type SchemaErrorReport = { error: string }
export type AnyReviewReport = WorkflowReviewReport | SchemaErrorReport

/** Narrowing helper — `report.rules` is only safe behind this. */
export function isStructuralReport(
  r: AnyReviewReport | null | undefined,
): r is WorkflowReviewReport {
  return !!r && Array.isArray((r as WorkflowReviewReport).rules)
}

/**
 * R9-R19 verdicts, or `[]` when the report is the schema-error shape.
 *
 * Four components open-coded `report?.rules || []` and then filtered it three
 * or four times each. Every one of them silently produced zeros on a
 * schema-error report — which is the one case where the user is staring at a
 * failure and wants to know why.
 */
export function reviewRules(r: AnyReviewReport | null | undefined): RuleVerdict[] {
  return isStructuralReport(r) ? r.rules : []
}

/** Pass-2 semantic verdicts, or `[]` when pass-2 never ran / the YAML didn't parse. */
export function semanticVerdictsOf(
  r: AnyReviewReport | null | undefined,
): SemanticVerdict[] {
  const v = isStructuralReport(r) ? r.semantic_verdicts?.verdicts : undefined
  return Array.isArray(v) ? v : []
}

/** Why pass-2 produced nothing, when it failed rather than being skipped. */
export function semanticErrorOf(r: AnyReviewReport | null | undefined): string | null {
  return (isStructuralReport(r) ? r.semantic_verdicts?.error : null) ?? null
}

/** pass / warn / fail tallies, the shape every review badge wants. */
export function reviewRuleCounts(
  r: AnyReviewReport | null | undefined,
): { pass: number; warn: number; fail: number } {
  const rules = reviewRules(r)
  return {
    pass: rules.filter((x) => x.status === 'pass').length,
    warn: rules.filter((x) => x.status === 'warn').length,
    fail: rules.filter((x) => x.status === 'fail').length,
  }
}

// ---- Core resources -----------------------------------------------------

/** `workflow/schema.py` `ParameterSpec`, flattened by `workflows.py` `_parse_parameters`. */
export type WorkflowParameter = {
  name: string
  type: 'string' | 'int' | 'float' | 'bool'
  /** Already normalized: true only when required AND lacking a usable default. */
  required: boolean
  default: unknown
  description: string | null
}

/** `models/workflow_definition.py` `WorkflowReviewStatus`. */
export type WorkflowReviewStatus = 'pending' | 'passed' | 'rejected' | 'error'

/** `models/mission.py` `MissionStatus`. */
export type MissionStatus =
  | 'pending' | 'running' | 'paused' | 'cancelled' | 'succeeded' | 'failed'

/** `models/run.py` `RunStatus`. */
export type RunStatus =
  | 'pending' | 'running' | 'succeeded' | 'failed' | 'needs_review'

/** One row of `GET /api/workflows` — `workflows.py` `list_workflows`. */
export type Workflow = {
  id: string
  name: string
  description: string | null
  file_path: string | null
  project_id: string | null
  /** Resolved server-side so the UI can group without a second fetch. */
  project_name: string | null
  review_status: WorkflowReviewStatus
  review_report: WorkflowReviewReport | null
  reviewed_at: string | null
  archived_at: string | null
  /** Derived from the most recent mission; null when never run. */
  last_run_at: string | null
  last_status: MissionStatus | null
  parameters: WorkflowParameter[]
}

/** `missions.py` `_mission_dict`. */
export type Mission = {
  id: string
  workflow_def_id: string | null
  status: MissionStatus
  current_stage: string | null
  parameters: Record<string, unknown>
  workspace_path: string | null
  started_at: string | null
  ended_at: string | null
  failure_reason: string | null
  audit_log: unknown[]
  stages_completed: number
  /** null when the workflow def carries no compiled stage list. */
  stages_total: number | null
}

/**
 * One stage execution inside a mission — `automation.py` `_run_dict`.
 * The table is `stage_execution`; the class kept the name `Run` for compat,
 * and so does this type, to match the `/api/runs` route it comes from.
 */
export type Run = {
  id: string
  mission_id: string | null
  stage_name: string | null
  schedule_entry_id: string | null
  session_id: string | null
  status: RunStatus
  started_at: string | null
  ended_at: string | null
  exit_code: number | null
  parameters: Record<string, unknown>
  review_note: string | null
}

/** `automation.py` `_se_dict`. */
export type Schedule = {
  id: string
  workflow_def_id: string | null
  cron: string | null
  /** ISO 8601, UTC, no timezone suffix. */
  run_at: string | null
  kind: 'once' | 'recurring'
  enabled: boolean
  parameters: Record<string, unknown>
  next_run_at: string | null
  last_run_at: string | null
}

/**
 * `GET /api/workflows/{name}` — `workflows.py` `get_workflow`. Carries
 * `yaml_content` (which the list endpoint omits for payload size) but drops
 * the list-only derived fields (project_name, last_run_at, archived_at).
 */
export type WorkflowDetail = Pick<
  Workflow,
  'id' | 'name' | 'description' | 'file_path' | 'review_status' | 'review_report'
  | 'reviewed_at' | 'parameters'
> & { yaml_content: string | null }

/** Shared shape of `generateWorkflow` / `editWithAgent`. */
export type AuthoringResult = {
  workflow_id: string | null
  workflow_name: string | null
  yaml_path: string | null
  review_status: string
  /** May be the schema-error shape — narrow with `isStructuralReport`. */
  review_report: AnyReviewReport | null
  stdout_tail: string
  duration_sec: number
  error: string | null
}

export const automationApi = {
  reloadWorkflows: async () =>
    (await http.post('/api/workflows/reload')).data,

  // Round-1 clarify: spawns a short (~15-40s) claude that skims the repo,
  // then returns boundary-condition questions (max 5) the user should
  // answer before we commit to a YAML shape. If `needs_clarify` is false
  // the frontend skips Step 2 and calls generate directly.
  clarifyWorkflow: async (body: {
    repo_path: string
    requirement: string
    workflow_name?: string
  }): Promise<{
    clarification_id: string | null
    needs_clarify: boolean
    stage_preview: string
    // Agent-proposed stage skeleton — user reviews/adjusts on the preview
    // card before generate locks it in. Empty when the agent's response
    // was malformed / legacy.
    stages: { name: string; kind: 'claude' | 'poll'; purpose: string }[]
    questions: {
      id: string
      text: string
      options: { value: string; label: string; recommended: boolean }[]
    }[]
    duration_sec: number
    error: string | null
    stdout_tail: string
  }> => {
    const r = await http.post('/api/workflows/clarify', body, {
      timeout: 200_000, // backend clarify has a 180s hard cap; give axios 20s slack
    })
    return r.data
  },

  // Round-2 generate: spawns `claude -p` with cwd=repo_path, splices the
  // guide + requirement + (optional) clarification answers, waits up to
  // 10 min, then returns review report.
  generateWorkflow: async (body: {
    repo_path: string
    requirement: string
    workflow_name?: string
    clarification_id?: string
    answers?: Record<string, string>
    free_text?: Record<string, string>
    // User-adjusted stage skeleton from the preview card. When present,
    // the generate agent is hard-locked to this decomposition — cannot
    // invent new stages. Absent = agent decides freely (legacy path).
    confirmed_stages?: { name: string; kind: 'claude' | 'poll'; purpose: string }[]
  }): Promise<AuthoringResult> => {
    // Long timeout — generation can take up to 10 min on cold cache.
    const r = await http.post('/api/workflows/generate', body, {
      timeout: 700_000, // 700s > backend 600s cap so backend times out first with a clear error
    })
    return r.data
  },
  // Schedules
  listSchedules: async (): Promise<ListResponse<Schedule>> =>
    (await http.get('/api/schedules')).data,
  createSchedule: async (body: {
    workflow_def_id: string
    cron?: string
    run_at?: string  // ISO 8601, UTC
    parameters?: Record<string, unknown>
  }) =>
    (await http.post('/api/schedules', body)).data,
  updateSchedule: async (id: string, body: { cron?: string; run_at?: string }) =>
    (await http.patch(`/api/schedules/${id}`, body)).data,
  enableSchedule: async (id: string) => (await http.post(`/api/schedules/${id}/enable`)).data,
  disableSchedule: async (id: string) => (await http.post(`/api/schedules/${id}/disable`)).data,
  deleteSchedule: async (id: string) => (await http.delete(`/api/schedules/${id}`)).data,

  // Runs (stage executions inside a workflow mission)
  listRuns: async (params?: { limit?: number }): Promise<ListResponse<Run>> =>
    (await http.get('/api/runs', { params })).data,
  getRun: async (id: string) => (await http.get(`/api/runs/${id}`)).data,
  retryRun: async (id: string) => (await http.post(`/api/runs/${id}/retry`)).data,
  cancelRun: async (id: string) => (await http.post(`/api/runs/${id}/cancel`)).data,
  getOutputRaw: async (runId: string, outputId: string): Promise<string> => {
    const r = await http.get(`/api/runs/${runId}/outputs/${outputId}/raw`, { responseType: 'text' })
    return r.data as string
  },

  // Natural-language edit of an existing workflow. Spawns claude with
  // cwd=CSM tasks dir, agent reads current YAML + splices feedback +
  // writes new version back. Same result shape as generateWorkflow.
  editWithAgent: async (
    name: string,
    feedback: string,
  ): Promise<AuthoringResult> => {
    const r = await http.post(
      `/api/workflows/${encodeURIComponent(name)}/edit-with-agent`,
      { feedback },
      { timeout: 700_000 },
    )
    return r.data
  },

  // Start an INTERACTIVE claude session pre-loaded to iterate on the
  // named workflow's YAML. Returns { session_id, cwd, title }; caller
  // routes to /sessions/<session_id>.
  startDebugSession: async (name: string): Promise<{
    session_id: string
    cwd: string
    title: string
  }> => {
    const r = await http.post(
      `/api/workflows/${encodeURIComponent(name)}/debug-session`,
    )
    return r.data
  },

  // Workflows + Missions (P2: primary automation entry)
  listWorkflows: async (): Promise<ListResponse<Workflow>> =>
    (await http.get('/api/workflows')).data,
  getWorkflow: async (name: string): Promise<WorkflowDetail> =>
    (await http.get(`/api/workflows/${name}`)).data,
  updateWorkflowYaml: async (name: string, yaml_content: string) =>
    (await http.put(`/api/workflows/${name}`, { yaml_content })).data,
  deleteWorkflow: async (name: string) =>
    (await http.delete(`/api/workflows/${name}`)).data,
  archiveWorkflow: async (name: string) =>
    (await http.post(`/api/workflows/${name}/archive`)).data,
  moveWorkflow: async (name: string, project_id: string | null) =>
    (await http.post(`/api/workflows/${encodeURIComponent(name)}/move`, { project_id })).data,
  listMissions: async (params?: { limit?: number }): Promise<ListResponse<Mission>> =>
    (await http.get('/api/missions', { params })).data,
  launchMission: async (workflow_name: string, params?: Record<string, unknown>) =>
    (await http.post('/api/missions/launch', { workflow_name, params: params || {} })).data,
  cancelMission: async (mission_id: string) =>
    (await http.post(`/api/missions/${mission_id}/cancel`)).data,
  retryMission: async (
    mission_id: string,
    stage: string,
    mode: 'rerun' | 'revalidate' = 'rerun',
  ) =>
    (
      await http.post(`/api/missions/${mission_id}/retry`, null, {
        params: { stage, mode },
      })
    ).data,
}
