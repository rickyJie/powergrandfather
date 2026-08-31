<script setup lang="ts">
/**
 * WorkflowList — left-rail of the Automation page. Renders workflows
 * grouped into **projects** (user-managed) plus fallback **heuristic
 * buckets** (auto-derived from the workflow's `repo_root` parameter
 * last path segment).
 *
 * Grouping precedence:
 *   1. If workflow.project_id is set → group under that project's name
 *   2. Else → group by repo_root heuristic (last path segment)
 *   3. Else → `misc`
 *
 * Project group heads support: rename (inline), archive (kebab).
 * Auto-group heads show a "Convert to project" chip → parent opens the
 * NewProjectModal prefilled with the bucket's workflow_names.
 *
 * Row-level: kebab menu → "Move to…" dropdown lists all active
 * projects + "Uncategorized" (project_id → NULL).
 */
import { computed, ref } from 'vue'
import { reviewRuleCounts, reviewRules, semanticVerdictsOf } from '../../api/automation'
import type { Mission, Schedule, Workflow } from '../../api/automation'
import type { Project } from '../../api/projects'

const props = defineProps<{
  workflows: Workflow[]
  missions: Mission[]
  schedules: Schedule[]
  projects: Project[]
  selectedWorkflowId: string | null
}>()

const emit = defineEmits<{
  (e: 'select', wfId: string): void
  (e: 'open-detail', wf: Workflow): void
  (e: 'new-project'): void
  (e: 'convert-auto-group', payload: { name: string; workflow_names: string[] }): void
  // Auto-bucket name collides with an existing Project name — instead of
  // trying to create-with-clash (409) we batch-reparent into the existing
  // project. Parent handler calls projectsApi.absorb().
  (e: 'merge-auto-group', payload: { project_id: string; workflow_names: string[] }): void
  (e: 'rename-project', projectId: string, newName: string): void
  (e: 'archive-project', projectId: string): void
  (e: 'move-workflow', workflowName: string, projectId: string | null): void
}>()

function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function fmtRel(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Date.now() - t
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff/60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff/3_600_000)}h ago`
  return `${Math.floor(diff/86_400_000)}d ago`
}

function verdictCounts(wf: Workflow) {
  return reviewRuleCounts(wf.review_report)
}

function reviewBadge(wf: Workflow): { label: string; cls: string; title: string } {
  const c = verdictCounts(wf)
  if (c.fail > 0) return { label: `${c.fail} fail`, cls: 'badge-fail', title: 'Has fail rules — cannot launch mission' }
  if (c.warn > 0) return { label: `${c.warn} warn`, cls: 'badge-warn', title: 'Has warn rules — recommended to fix' }
  if (wf.review_status === 'passed') return { label: 'passed', cls: 'badge-ok', title: 'All R9-R19 pass' }
  return { label: wf.review_status || 'pending', cls: 'badge-neutral', title: 'Review pending' }
}

// Composite quality score 0-10. Blends:
//   - Structural (R9-R19) pass ratio, weight 0.4
//   - Semantic (Pass-2) pass ratio, weight 0.4
//   - First-mission signal, weight 0.2 (succeeded=+1, failed=-1, none=0)
//
// Returns null when there's no data at all (no rules, no verdicts, no runs)
// so the UI can hide the badge cleanly instead of showing 0/10 misleadingly.
function qualityScore(wf: Workflow): { score: number; label: string; cls: string; title: string } | null {
  const rules = reviewRules(wf.review_report)
  const verdicts = semanticVerdictsOf(wf.review_report)
  const missions = props.missions.filter((m) => m.workflow_def_id === wf.id)
  if (rules.length === 0 && verdicts.length === 0 && missions.length === 0) return null

  const rulesPass = rules.filter((r) => r.status === 'pass').length
  const rulesTotal = rules.length || 1
  const structural = rulesPass / rulesTotal

  const semanticPass = verdicts.filter((v) => v.status === 'pass').length
  const semanticTotal = verdicts.length || 1
  const semantic = verdicts.length > 0 ? semanticPass / semanticTotal : structural

  const firstMission = missions
    .sort((a, b) => (a.started_at || '').localeCompare(b.started_at || ''))[0]
  let runSignal = 0
  if (firstMission?.status === 'succeeded') runSignal = 1
  else if (firstMission?.status === 'failed') runSignal = -1

  const raw = 0.4 * structural + 0.4 * semantic + 0.2 * ((runSignal + 1) / 2)
  const score = Math.round(raw * 10)
  const cls = score >= 8 ? 'q-good' : score >= 5 ? 'q-mid' : 'q-low'
  const title = [
    `structural R9-R19: ${rulesPass}/${rulesTotal} pass`,
    `semantic Pass-2: ${verdicts.length ? `${semanticPass}/${verdicts.length}` : 'not run'}`,
    `first run: ${firstMission?.status || 'N/A'}`,
  ].join(' · ')
  return { score, label: `Q ${score}/10`, cls, title }
}

function workflowMissionStats(wfId: string) {
  const list = props.missions.filter((m) => m.workflow_def_id === wfId)
  return {
    total: list.length,
    ok: list.filter((m) => m.status === 'succeeded').length,
    fail: list.filter((m) => m.status === 'failed').length,
    running: list.filter((m) => m.status === 'running').length,
  }
}

function workflowNextRun(wfId: string) {
  return props.schedules
    .filter((s) => s.workflow_def_id === wfId && s.enabled)
    .sort((a, b) => (a.next_run_at || '').localeCompare(b.next_run_at || ''))[0] || null
}

function repoRootLabel(wf: Workflow): string | null {
  const p = (wf.parameters || []).find((x) => x.name === 'repo_root')
  const def = p?.default
  if (typeof def !== 'string' || !def) return null
  const parts = def.split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : null
}

// A group is either a user-owned project (kind='project') or an
// auto-derived bucket (kind='auto'). Rendering diverges only at the
// head — the workflow row markup is identical in both.
type Group = {
  kind: 'project' | 'auto'
  id: string          // project.id for project, `auto:<label>` for auto
  name: string
  workflows: Workflow[]
  projectId: string | null   // present on kind='project' rows for row-level move
}

const grouped = computed<Group[]>(() => {
  const projByName = new Map<string, Project>(props.projects.map(p => [p.name, p]))
  // First bucket: known projects (always show even when empty, so users
  // can see they exist and drag workflows in later).
  const projMap = new Map<string, Group>()
  for (const p of props.projects) {
    projMap.set(p.id, {
      kind: 'project',
      id: p.id,
      name: p.name,
      workflows: [],
      projectId: p.id,
    })
  }
  const autoMap = new Map<string, Group>()

  for (const wf of props.workflows) {
    if (wf.project_id && projMap.has(wf.project_id)) {
      projMap.get(wf.project_id)!.workflows.push(wf)
      continue
    }
    // project_id points to an archived / stale project → fall through
    // to heuristic bucketing. project_name is the resolved display name
    // from the /workflows response; if absent we compute the label.
    const label = wf.project_name || repoRootLabel(wf) || 'misc'
    if (!autoMap.has(label)) {
      autoMap.set(label, {
        kind: 'auto',
        id: `auto:${label}`,
        name: label,
        workflows: [],
        projectId: null,
      })
    }
    autoMap.get(label)!.workflows.push(wf)
  }

  // Sort workflows within each group by name for stability.
  const sortWorkflows = (g: Group) => g.workflows.sort((a, b) => a.name.localeCompare(b.name))
  const projectGroups = Array.from(projMap.values())
    .map(g => { sortWorkflows(g); return g })
    .sort((a, b) => a.name.localeCompare(b.name))
  const autoGroups = Array.from(autoMap.values())
    .map(g => { sortWorkflows(g); return g })
    .sort((a, b) => {
      if (a.name === 'misc') return 1
      if (b.name === 'misc') return -1
      return a.name.localeCompare(b.name)
    })
  // Projects first, then auto buckets (visually communicates "here's
  // what you own, here's what you haven't organized yet").
  void projByName  // reserved for future look-up-by-name flows
  return [...projectGroups, ...autoGroups]
})

const collapsed = ref<Set<string>>(
  new Set(JSON.parse(localStorage.getItem('csm.wf.groups.collapsed') || '[]')),
)
function toggleGroup(id: string) {
  if (collapsed.value.has(id)) collapsed.value.delete(id)
  else collapsed.value.add(id)
  localStorage.setItem(
    'csm.wf.groups.collapsed',
    JSON.stringify([...collapsed.value]),
  )
  collapsed.value = new Set(collapsed.value)
}
function isGroupOpen(id: string): boolean { return !collapsed.value.has(id) }

function lastStatusChip(wf: Workflow): { label: string; cls: string } | null {
  if (!wf.last_status) return null
  switch (wf.last_status) {
    case 'succeeded': return { label: '✓ ok',       cls: 'chip-ok' }
    case 'failed':    return { label: '✗ failed',   cls: 'chip-fail' }
    case 'running':   return { label: '● running',  cls: 'chip-running' }
    case 'cancelled': return { label: 'cancelled',  cls: 'chip-neutral' }
    default:          return { label: wf.last_status, cls: 'chip-neutral' }
  }
}

function handleRowClick(wf: Workflow) {
  emit('select', wf.id)
  emit('open-detail', wf)
}

// Rename UX: click ✎ on a project head → head swaps to an inline input
// (bound to `renameDraft`). Enter commits, Esc cancels.
const renamingId = ref<string | null>(null)
const renameDraft = ref('')
function startRename(g: Group) {
  if (g.kind !== 'project') return
  renamingId.value = g.id
  renameDraft.value = g.name
}
function commitRename(g: Group) {
  const next = renameDraft.value.trim()
  if (!next || next === g.name) {
    renamingId.value = null
    return
  }
  emit('rename-project', g.id, next)
  renamingId.value = null
}
function cancelRename() {
  renamingId.value = null
  renameDraft.value = ''
}

// Kebab menus are ad-hoc positioned popovers — one per row, one per
// group head. We track the currently-open menu by a synthetic key so
// only one shows at a time and outside-clicks dismiss it.
const openMenu = ref<string | null>(null)
function toggleMenu(key: string, ev: Event) {
  ev.stopPropagation()
  openMenu.value = openMenu.value === key ? null : key
}
function closeMenu() { openMenu.value = null }

// Case-sensitive lookup of active project by its display name — used to
// detect the "auto-bucket collides with an existing Project" case. When an
// auto-derived group's name (repo_root last segment) exactly matches an
// active project's name, the "Convert to project" action would 409 on the
// duplicate-name check server-side; we instead offer a merge into the
// existing project.
const projectByName = computed<Map<string, Project>>(() => {
  const m = new Map<string, Project>()
  for (const p of props.projects) {
    if (p.archived_at) continue
    m.set(p.name, p)
  }
  return m
})
function collidingProject(g: Group): Project | null {
  if (g.kind !== 'auto') return null
  return projectByName.value.get(g.name) || null
}

function convertAutoGroup(g: Group, ev: Event) {
  ev.stopPropagation()
  emit('convert-auto-group', {
    name: g.name,
    workflow_names: g.workflows.map(w => w.name),
  })
}

function mergeAutoGroup(g: Group, target: Project, ev: Event) {
  ev.stopPropagation()
  emit('merge-auto-group', {
    project_id: target.id,
    workflow_names: g.workflows.map(w => w.name),
  })
}

function archiveProject(g: Group, ev: Event) {
  ev.stopPropagation()
  closeMenu()
  emit('archive-project', g.id)
}

function moveWorkflowTo(wf: Workflow, projectId: string | null, ev: Event) {
  ev.stopPropagation()
  closeMenu()
  if ((wf.project_id ?? null) === projectId) return
  emit('move-workflow', wf.name, projectId)
}

// "New project with this workflow" — parent opens NewProjectModal
// prefilled with a suggested name (repo_root label or workflow name)
// and workflow_names=[wf.name] so save-create-move happens atomically.
function createProjectWithWorkflow(wf: Workflow, ev: Event) {
  ev.stopPropagation()
  closeMenu()
  const suggested = repoRootLabel(wf) || wf.name
  emit('convert-auto-group', { name: suggested, workflow_names: [wf.name] })
}
</script>

<template>
  <div class="panel wfl-panel" @click="closeMenu">
    <div class="wfl-header">
      <h3>Workflows ({{ workflows.length }})</h3>
      <button
        class="wfl-new-project"
        @click="emit('new-project')"
        title="Create a new project bucket"
      >+ Project</button>
    </div>

    <div v-if="workflows.length === 0" class="wfl-empty">
      <p>No workflows yet.</p>
      <p class="wfl-empty-hint">
        Hit <b>+ New workflow</b> at the top to create your first one.
      </p>
    </div>

    <ul v-else class="wfl-groups">
      <li v-for="g in grouped" :key="g.id" class="wfl-group">
        <div class="wfl-group-head" @click="toggleGroup(g.id)">
          <span class="wfl-group-caret">{{ isGroupOpen(g.id) ? '▾' : '▸' }}</span>

          <!-- Project group head (with inline rename) -->
          <template v-if="g.kind === 'project'">
            <template v-if="renamingId === g.id">
              <input
                class="wfl-rename-input"
                v-model="renameDraft"
                autofocus
                @click.stop
                @keyup.enter="commitRename(g)"
                @keyup.esc="cancelRename()"
                @blur="commitRename(g)"
              />
            </template>
            <template v-else>
              <span class="wfl-group-name">{{ g.name }}</span>
            </template>
          </template>

          <!-- Heuristic auto-group head — no chip; italic name signals
               "not user-owned", the chip clutter came out of tag polish -->
          <template v-else>
            <span class="wfl-group-name wfl-group-auto" title="Auto-derived from repo_root parameter">{{ g.name }}</span>
          </template>

          <span class="wfl-group-count">{{ g.workflows.length }}</span>

          <!-- Actions -->
          <div class="wfl-group-actions" @click.stop>
            <template v-if="g.kind === 'auto' && g.workflows.length > 0">
              <!-- Name collides with an existing project → "merge into" -->
              <button
                v-if="collidingProject(g)"
                class="wfl-chip-btn wfl-chip-merge"
                @click="mergeAutoGroup(g, collidingProject(g)!, $event)"
                :title="`Project «${g.name}» already exists — merge these ${g.workflows.length} workflow(s) into it instead of creating a duplicate`"
              >⇥ Merge into project «{{ g.name }}» ({{ g.workflows.length }})</button>
              <!-- No collision → offer the normal convert flow -->
              <button
                v-else
                class="wfl-chip-btn"
                @click="convertAutoGroup(g, $event)"
                :title="`Turn this auto-detected bucket into a real Project and move all ${g.workflows.length} workflow(s) into it in one shot`"
              >Convert to project ({{ g.workflows.length }})</button>
            </template>

            <template v-if="g.kind === 'project' && renamingId !== g.id">
              <button
                class="wfl-icon-btn"
                @click.stop="startRename(g)"
                title="Rename project"
              >✎</button>
              <button
                class="wfl-icon-btn wfl-icon-danger"
                @click.stop="archiveProject(g, $event)"
                title="Archive project — workflows fall back to their auto-derived bucket"
              >🗄</button>
            </template>
          </div>
        </div>

        <ul v-if="isGroupOpen(g.id)" class="wfl-list">
          <li v-if="g.workflows.length === 0" class="wfl-empty-row">
            No workflows — add one via ⋯ → Move to → {{ g.name }} on any other row
          </li>
          <li
            v-for="wf in g.workflows"
            :key="wf.id"
            :class="{ 'wfl-row': true, 'wfl-selected': wf.id === selectedWorkflowId }"
            @click="handleRowClick(wf)"
          >
            <div class="wfl-main">
              <div class="wfl-name-row">
                <code class="wfl-name">{{ wf.name }}</code>
                <span
                  class="wfl-badge"
                  :class="reviewBadge(wf).cls"
                  :title="reviewBadge(wf).title"
                >
                  {{ reviewBadge(wf).label }}
                </span>
                <span
                  v-if="qualityScore(wf)"
                  class="wfl-badge wfl-quality"
                  :class="qualityScore(wf)!.cls"
                  :title="qualityScore(wf)!.title"
                >
                  {{ qualityScore(wf)!.label }}
                </span>
              </div>
              <div v-if="wf.description" class="wfl-desc">
                {{ wf.description }}
              </div>
              <div class="wfl-stats">
                <template v-if="workflowMissionStats(wf.id).total > 0">
                  <span class="wfl-stat" title="Total missions">
                    {{ workflowMissionStats(wf.id).total }} runs
                  </span>
                  <span class="wfl-stat wfl-ok" v-if="workflowMissionStats(wf.id).ok">
                    ✓ {{ workflowMissionStats(wf.id).ok }}
                  </span>
                  <span class="wfl-stat wfl-fail" v-if="workflowMissionStats(wf.id).fail">
                    ✗ {{ workflowMissionStats(wf.id).fail }}
                  </span>
                  <span class="wfl-stat wfl-running" v-if="workflowMissionStats(wf.id).running">
                    ● {{ workflowMissionStats(wf.id).running }}
                  </span>
                </template>
                <span
                  v-if="wf.last_run_at"
                  class="wfl-stat wfl-last"
                  :title="`Last run at ${fmtLocal(wf.last_run_at)}`"
                >
                  {{ fmtRel(wf.last_run_at) }}
                </span>
                <span
                  v-if="lastStatusChip(wf)"
                  class="wfl-chip"
                  :class="lastStatusChip(wf)!.cls"
                >
                  {{ lastStatusChip(wf)!.label }}
                </span>
                <span
                  v-if="workflowNextRun(wf.id)"
                  class="wfl-stat wfl-next"
                  :title="`Next scheduled run: ${fmtLocal(workflowNextRun(wf.id)?.next_run_at)}`"
                >
                  next {{ fmtLocal(workflowNextRun(wf.id)?.next_run_at) }}
                </span>
              </div>
            </div>

            <!-- Per-row kebab menu → Move to… -->
            <div class="wfl-row-actions" @click.stop>
              <button
                class="wfl-icon-btn wfl-row-kebab"
                @click.stop="toggleMenu(`row:${wf.id}`, $event)"
                title="More actions"
              >⋯</button>
              <div
                v-if="openMenu === `row:${wf.id}`"
                class="wfl-menu wfl-menu-row"
                @click.stop
              >
                <div class="wfl-menu-label">Move to…</div>
                <button
                  v-for="p in projects"
                  :key="p.id"
                  :disabled="wf.project_id === p.id"
                  @click="moveWorkflowTo(wf, p.id, $event)"
                >
                  {{ p.name }}
                  <span v-if="wf.project_id === p.id" class="wfl-menu-check">✓</span>
                </button>
                <button
                  v-if="projects.length === 0"
                  disabled
                  class="wfl-menu-hint"
                >No projects yet — create one with + Project</button>
                <div class="wfl-menu-sep" />
                <button
                  class="wfl-menu-create"
                  @click="createProjectWithWorkflow(wf, $event)"
                  title="Create a new project and put this workflow into it"
                >
                  <span>+ New project with this workflow</span>
                </button>
                <div class="wfl-menu-sep" />
                <button
                  :disabled="!wf.project_id"
                  @click="moveWorkflowTo(wf, null, $event)"
                >
                  Uncategorized (auto-group)
                  <span v-if="!wf.project_id" class="wfl-menu-check">✓</span>
                </button>
              </div>
            </div>

            <div class="wfl-chevron" aria-hidden>›</div>
          </li>
        </ul>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.wfl-panel { padding: 0; overflow-y: auto; height: 100%; }
.wfl-header {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; background: var(--card); z-index: 2;
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.wfl-header h3 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.2px; }
.wfl-new-project {
  font-size: 11.5px;
  padding: 3px 9px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--ink);
  cursor: pointer;
  transition: border-color 120ms;
}
.wfl-new-project:hover { border-color: var(--ink); }

.wfl-empty { padding: 24px 20px; text-align: center; color: var(--ink-mute); }
.wfl-empty-hint { font-size: 13px; margin-top: 8px; }
.wfl-empty-row {
  padding: 8px 14px 8px 22px;
  font-size: 11.5px; color: var(--ink-faint);
  font-style: italic;
  border-bottom: 1px solid var(--border);
}

.wfl-groups { list-style: none; margin: 0; padding: 0; }
.wfl-group { border-bottom: 1px solid var(--border); }
.wfl-group:last-child { border-bottom: 0; }
.wfl-group-head {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 14px;
  background: var(--canvas);
  cursor: pointer;
  font-size: 12px; font-weight: 600;
  color: var(--ink-mute);
  letter-spacing: 0.3px;
  user-select: none;
  position: relative;
}
.wfl-group-head:hover { color: var(--ink); }
.wfl-group-caret { font-size: 10px; width: 12px; display: inline-block; }
.wfl-group-name {
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  color: var(--ink);
}
.wfl-group-auto {
  font-style: italic;
  color: var(--ink-mute);
  border-bottom: 1px dashed var(--border-strong);
  padding-bottom: 1px;
}
.wfl-group-count {
  margin-left: auto;
  font-size: 11px; font-weight: 500;
  color: var(--ink-faint);
  padding: 1px 7px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 999px;
}
.wfl-group-actions {
  display: flex; align-items: center; gap: 4px;
  position: relative;
}
.wfl-chip-btn {
  font-size: 10.5px;
  padding: 2px 8px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 3px;
  color: var(--ink);
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 120ms;
}
.wfl-chip-btn:hover { border-color: var(--ink); }
.wfl-icon-btn {
  padding: 2px 6px;
  background: transparent;
  border: none;
  color: var(--ink-mute);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  border-radius: 3px;
}
.wfl-icon-btn:hover { background: var(--card); color: var(--ink); }
.wfl-icon-danger:hover { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }
.wfl-rename-input {
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  padding: 2px 6px;
  border: 1px solid var(--ink);
  border-radius: 3px;
  background: var(--card);
  color: var(--ink);
  min-width: 120px;
}

.wfl-menu {
  position: absolute;
  top: 100%;
  right: 8px;
  z-index: 5;
  min-width: 180px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.12);
  padding: 4px;
  display: flex; flex-direction: column;
  font-size: 12.5px;
}
.wfl-menu button {
  text-align: left;
  padding: 6px 10px;
  background: transparent;
  border: none;
  color: var(--ink);
  cursor: pointer;
  border-radius: 3px;
  font-size: 12.5px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px;
}
.wfl-menu button:hover:not(:disabled) { background: var(--canvas); }
.wfl-menu button:disabled { color: var(--ink-faint); cursor: not-allowed; }
.wfl-menu-label {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-faint); padding: 4px 10px 2px;
}
.wfl-menu-sep {
  height: 1px; background: var(--border); margin: 4px 0;
}
.wfl-menu-check { color: var(--ink-faint); font-size: 11px; }
.wfl-menu-hint { font-style: italic; }
.wfl-menu-create { color: var(--accent-soft-fg); font-weight: 500; }
.wfl-menu-row { top: auto; bottom: 100%; margin-bottom: 4px; }

.wfl-list { list-style: none; margin: 0; padding: 0; }
.wfl-row {
  padding: 10px 14px 10px 22px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  display: flex; gap: 8px; align-items: center;
  transition: background 160ms var(--ease-soft);
  position: relative;
}
.wfl-row:last-child { border-bottom: 0; }
.wfl-row:hover { background: var(--canvas); }
.wfl-selected { background: var(--canvas); }
.wfl-selected .wfl-name { color: var(--ink); }
.wfl-row-actions { position: relative; }
.wfl-row-kebab { opacity: 0.4; }
.wfl-row:hover .wfl-row-kebab { opacity: 0.9; }

.wfl-main { flex: 1; min-width: 0; }
.wfl-name-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.wfl-name {
  font-size: 13px; font-weight: 500;
  background: transparent; padding: 0;
  color: var(--ink);
}
.wfl-desc {
  font-size: 12px; color: var(--ink-mute); margin-top: 4px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.4;
}
.wfl-stats {
  display: flex; gap: 10px; margin-top: 4px; font-size: 11.5px; flex-wrap: wrap;
  align-items: center;
}
.wfl-stat { color: var(--ink-mute); }
.wfl-ok { color: #16a34a; }
.wfl-fail { color: #dc2626; }
.wfl-running { color: #1e40af; }
.wfl-next { color: #7c3aed; }
.wfl-last { color: var(--ink-faint); font-family: 'Geist Mono', monospace; }

.wfl-chip {
  font-size: 10.5px;
  padding: 1px 7px;
  border-radius: 3px;
  font-weight: 500;
  white-space: nowrap;
}
.chip-ok { background: #dcfce7; color: #166534; }
.chip-fail { background: #fee2e2; color: #991b1b; }
.chip-running { background: #dbeafe; color: #1e40af; }
.chip-neutral { background: var(--canvas); color: var(--ink-mute); border: 1px solid var(--border); }

.wfl-badge {
  font-size: 11px; padding: 1px 7px; border-radius: 3px;
  white-space: nowrap;
  font-weight: 500;
}
.badge-ok { background: #dcfce7; color: #166534; }
.badge-warn { background: #fef3c7; color: #92400e; }
.badge-fail { background: #fee2e2; color: #991b1b; }
.badge-neutral { background: var(--canvas); color: var(--ink-mute); }

/* Quality-score composite badge: 0.4 * structural + 0.4 * semantic + 0.2 * first-run.
   Distinct visual so it doesn't blend into the R9-R19 verdict pill. */
.wfl-quality { font-family: 'Geist Mono', monospace; letter-spacing: 0.02em; }
.q-good { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
.q-mid  { background: #fefce8; color: #a16207; border: 1px solid #fde68a; }
.q-low  { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }

.wfl-chevron {
  font-size: 22px;
  color: var(--ink-mute);
  opacity: 0.5;
  line-height: 1;
  padding-left: 4px;
}
.wfl-row:hover .wfl-chevron { opacity: 0.9; }
</style>
