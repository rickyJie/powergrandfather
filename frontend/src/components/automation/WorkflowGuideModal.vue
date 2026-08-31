<script setup lang="ts">
/**
 * WorkflowGuideModal — user-facing help modal explaining the two ways
 * to author a CSM workflow YAML:
 *
 *   A. In-UI wizard (recommended)  — 3-step wizard behind + New workflow
 *   B. DIY claude session           — user opens claude in the target repo
 *                                     and asks it to write the YAML
 *
 * Both paths end up with the same YAML on disk and the same review report.
 * A is faster for common cases; B is what long-time users used before the
 * wizard existed, and still useful when they want to iterate manually or
 * inspect intermediate reasoning.
 */
import { ref } from 'vue'
import { useToast } from '../../composables/useToast'
const toast = useToast()

defineProps<{
  open: boolean
}>()
const emit = defineEmits<{
  (e: 'close'): void
}>()

const PROMPT_TEMPLATE = `# Write a CSM workflow YAML for this repo

## What I want (one sentence)

[Replace what's in [ ] with your own words — the more natural the better. E.g.]

[**Example A**: every Sunday at 22:00, pull the last week of submissions from
the feedback inbox, review each one, make and commit + push any code changes
they call for, then notify me.]

[**Example B**: whenever I push a tag, read the commits that tag covers and
generate a changelog + release note.]

[**Example C**: run \`make quick-eval\` every morning at 9, and summarise the
eval report plus its diff against yesterday into a markdown file.]

## What you need to do

1. **Read the authoring guide** (required — it has the full procedure):

    Read <the CSM repo>/docs/workflow_authoring_guide.md

2. **Understand this repo**: the cwd IS the repo I want to automate. Run
   \`ls\` / \`git log --oneline -5\`, read the README and Makefile — work out what
   this repo does and which commands are worth calling.

3. **Follow the guide's method**:
   - §1 decompose my sentence into a stages array + params + per-stage outputs
   - §4 pick a fitting template (T1/T2/T3) as the skeleton
   - §5-§7 stay clear of the F1-F9 known traps
   - §8 self-check R9-R19 down to 0 warn + 0 fail

4. **Write the YAML into the CSM repo's tasks directory** (a cross-repo write):
   \`<the CSM repo>/tasks/<workflow_name>.workflow.yaml\`
   (workflow_name must match \`[a-z][a-z0-9_]*\`)

5. **Print one confirmation line at the end**:
   \`wrote <the CSM repo>/tasks/<name>.workflow.yaml\`

## Quality bar

- the decomposition must cover every step I described — don't merge them away
- each stage's outputs are **concrete file paths**, not vague prose like
  "produce a report"
- placeholders may only use the vocabulary listed in guide §3; escape anything
  else as \`{{...}}\`
- the R9-R19 self-check must reach 0 fail **and** 0 warn — a warn is a trap too`

const copied = ref(false)
async function copyPrompt() {
  try {
    await navigator.clipboard.writeText(PROMPT_TEMPLATE)
    copied.value = true
    toast.info('Prompt copied to clipboard')
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    toast.warn('Copy failed — please select and copy manually')
  }
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" role="presentation" @click.self="emit('close')">
    <div class="modal wg-modal panel" role="dialog" aria-modal="true" aria-label="Workflow authoring guide">
      <div class="wg-header">
        <div>
          <div class="wg-eyebrow">Workflow authoring</div>
          <h3 class="serif">How do I create a new workflow?</h3>
        </div>
        <button class="close-btn" @click="emit('close')" aria-label="Close">✕</button>
      </div>

      <div class="wg-body">
        <!-- =========== TL;DR: two ways =========== -->
        <section class="wg-tldr">
          <h4>Two ways — pick one:</h4>
          <div class="wg-compare">
            <div class="wg-compare-card wg-compare-a">
              <div class="wg-badge">A · recommended</div>
              <h5>Built-in wizard</h5>
              <div class="wg-muted">
                Hit <b>+ New workflow</b> at the top, give it a repo and a
                one-sentence requirement. The agent asks a few scoping
                questions (its own answer is pre-selected, so you can accept
                them in one click), then writes the YAML and runs the R9-R19
                self-check.
              </div>
              <div class="wg-when">
                <b>When to use it</b>:<br />
                Most of the time — you state the goal, the agent decides the
                details.
              </div>
            </div>
            <div class="wg-compare-card wg-compare-b">
              <div class="wg-badge">B</div>
              <h5>Your own claude session</h5>
              <div class="wg-muted">
                Run <code>claude --dangerously-skip-permissions</code> in the
                target repo yourself, paste it the prompt below, and watch the
                agent decompose the task and write the YAML step by step.
              </div>
              <div class="wg-when">
                <b>When to use it</b>:<br />
                You want to see the agent's reasoning, you expect several
                rounds of iteration, or you want to use a session you already
                have open (and its context).
              </div>
            </div>
          </div>
          <div class="wg-tldr-note wg-muted">
            Both paths end in the same place: the YAML lands in
            <code>&lt;the CSM repo&gt;/tasks/</code>, the database
            gets the same <code>WorkflowDefinition</code>, and you get the same
            review report.
          </div>
        </section>

        <!-- =========== Way A: In-UI wizard =========== -->
        <section class="wg-section wg-way-a">
          <h4>Way A · the built-in wizard (3 steps)</h4>
          <ol class="wg-steps">
            <li>
              <b>Hit <span style="white-space:nowrap;">+ New workflow</span></b>
              and fill in the <b>absolute path of the target repo</b> and a
              <b>one-sentence requirement</b> (naming the workflow is
              optional).
              <div class="wg-muted">
                To automate ops in a repo at <code>/data/your-repo</code>, say
                so in the path field and write the requirement as "every Sunday
                night, pull submissions from the feedback inbox …".
              </div>
            </li>
            <li>
              <b>The agent asks a few scoping questions</b> — answer them, or
              just hit <b>Accept defaults + generate</b>.
              <div class="wg-muted">
                Things like "what if the inbox is empty this week", "what
                min_chars threshold", "which stages are skipped under dry_run".
                Every question comes with the agent's recommended answer
                pre-selected, so accepting is one click; you can also add free
                text.
              </div>
            </li>
            <li>
              <b>Read the result card</b>: verdict plus review report.
              <div class="wg-muted">
                Any warn or fail — hit <b>Ask the agent to fix it</b> for a
                second round. At <b>0 fail + 0 warn</b> you can go straight to
                <b>Launch mission</b> and try it.
              </div>
            </li>
          </ol>
        </section>

        <!-- =========== Way B: DIY claude session =========== -->
        <section class="wg-section wg-way-b">
          <h4>Way B · your own claude session (4 steps)</h4>
          <ol class="wg-steps">
            <li>
              <b>Start claude inside the repo you want to automate</b>
              <div class="wg-muted">
                <code>cd &lt;your repo&gt; &amp;&amp; claude --dangerously-skip-permissions</code>
                <br />
                e.g. <code>cd /data/your-repo</code>. The cwd matters because
                claude has to read your repo before it can tell what's possible.
              </div>
            </li>
            <li>
              <b>Copy the prompt below and write your requirement into it as
              one sentence</b>
              <div class="wg-muted">
                <b>Don't worry about the details — the agent decomposes them</b>.
                It reads the authoring guide, looks at your repo, then decides
                how many stages, which params, what each step produces, and
                whether anything needs polling.
              </div>
            </li>
            <li>
              <b>Once the agent has written the YAML, come back and hit
              <span style="white-space:nowrap;">↻ Reload yaml</span></b>
              <div class="wg-muted">
                The YAML is written <b>across repos, into the CSM repo's tasks
                directory</b>
                (<code>&lt;the CSM repo&gt;/tasks/&lt;name&gt;.workflow.yaml</code>)
                — you don't have to move it yourself.
                <br />
                After reloading, open the workflow row to see the review
                result. For any warn or fail, paste the reason back to claude
                and have it fix by rule id, until it reads 0 warn / 0 fail.
              </div>
            </li>
            <li>
              <b>Hit <b>Launch mission</b> and try it</b>
              <div class="wg-muted">
                Launch with real params. If it reaches the last stage without
                needing you to step in, it works. If it fails, paste the error
                back to claude and keep going.
              </div>
            </li>
          </ol>

          <div class="wg-prompt-block">
            <div class="wg-prompt-label">👇 Copy this and send it to claude</div>
            <div class="wg-prompt">
              <pre>{{ PROMPT_TEMPLATE }}</pre>
              <button class="wg-copy-btn" @click="copyPrompt">
                {{ copied ? '✓ Copied' : '📋 Copy prompt' }}
              </button>
            </div>
          </div>
        </section>

        <!-- =========== FAQ =========== -->
        <section class="wg-section wg-callout">
          <h4>FAQ</h4>
          <p><b>Q: In way A, can I skip the agent's scoping questions if I
            don't like them?</b></p>
          <p class="wg-muted">
            Yes. <b>Accept defaults + generate</b> skips all of them at once
            (which is the same as taking the agent's recommendation on every
            question), or <b>← Back to the requirement</b> lets you rewrite a
            more precise sentence. If clarification fails outright, it falls
            back to one-shot generation rather than getting stuck.
          </p>
          <p style="margin-top:10px;"><b>Q: In way B, does the cwd really have
            to be the repo being automated? Can't it be the CSM repo?</b></p>
          <p class="wg-muted">
            It has to be the target repo — the cwd is how claude reads your
            context. The CSM repo is only where the YAML lives, and claude
            writes there across repos without your help.
          </p>
          <p style="margin-top:10px;"><b>Q: The review reports a fail or a
            warn. Now what?</b></p>
          <p class="wg-muted">
            Way A: hit <b>Ask the agent to fix it</b> on the result card — the
            reason is carried into a second round automatically.<br />
            Way B: paste the reason out of review_report verbatim into your
            claude session and have it fix by rule id. Fails must reach zero
            (CSM rejects the workflow otherwise), and warns <b>should</b> too —
            each one is a past incident written down.
          </p>
          <p style="margin-top:10px;"><b>Q: A mission failed, or hung. What do
            I look at?</b></p>
          <p class="wg-muted">
            Start with the mission's <code>failure_reason</code>, then open
            <code>.workflow/missions/&lt;mission_id&gt;/</code> to see what each
            stage actually wrote. Paste what you find back to claude to fix the
            YAML, or use <code>retry_from_stage</code> to roll back and re-run.
            Guide §6 has the detail.
          </p>
          <p style="margin-top:10px;"><b>Q: When is a workflow the wrong
            tool?</b></p>
          <p class="wg-muted">
            One-off tasks (writing the YAML doesn't pay for itself), tasks
            needing a human decision partway through (workflows run
            unattended), and anything leaning on if/else branching (the MVP
            only supports a linear array). For those three, opening a claude
            session and doing it by hand is faster.
          </p>
        </section>

        <section class="wg-section" style="border-top:1px solid var(--border-soft,#e2e8f0);padding-top:14px;">
          <p class="wg-muted" style="margin:0;">
            📄 The full authoring guide (written for the agent):<br />
            <code>&lt;the CSM repo&gt;/docs/workflow_authoring_guide.md</code>
          </p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wg-modal {
  max-width: 820px;
  width: 92%;
  max-height: 88vh;
  overflow-y: auto;
}
.wg-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--border-soft, #e2e8f0);
}
.wg-eyebrow {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: var(--text-muted, #94a3b8);
  margin-bottom: 4px;
}
.wg-header h3 { margin: 0; font-size: 20px; }
.close-btn {
  background: transparent; border: none; font-size: 18px;
  cursor: pointer; padding: 4px 8px; color: var(--text-muted, #94a3b8);
}
.close-btn:hover { color: var(--text, #0f172a); }

.wg-body {
  padding: 12px 24px 24px;
  font-size: 14px;
  line-height: 1.65;
}
.wg-section { margin-bottom: 22px; }
.wg-section h4 {
  margin: 0 0 8px; font-size: 15px; font-weight: 600;
}
.wg-section p { margin: 6px 0; }

/* ---- TL;DR compare ---- */
.wg-tldr {
  margin-bottom: 22px; padding: 12px 16px 14px;
  background: var(--surface-alt, #f8fafc);
  border-radius: 6px;
}
.wg-tldr h4 { margin: 0 0 10px; font-size: 15px; }
.wg-compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.wg-compare-card {
  padding: 12px 14px; border-radius: 6px;
  border: 1px solid var(--border-soft, #e2e8f0);
  background: var(--card, white);
  position: relative;
}
.wg-compare-a { border-color: #a7f3d0; background: #f0fdf4; }
.wg-compare-b { border-color: #cbd5e1; }
.wg-badge {
  position: absolute; top: 8px; right: 10px;
  font-size: 10.5px; padding: 2px 8px; border-radius: 10px;
  background: #dcfce7; color: #166534; font-weight: 600;
}
.wg-compare-b .wg-badge { background: #f1f5f9; color: #475569; }
.wg-compare-card h5 {
  margin: 0 0 6px; font-size: 14px; font-weight: 600;
}
.wg-when {
  margin-top: 8px; font-size: 12.5px; color: var(--text, #0f172a);
  padding-top: 8px; border-top: 1px dashed var(--border-soft, #cbd5e1);
}
.wg-tldr-note {
  margin-top: 12px; font-size: 12.5px;
}

/* ---- Way sections ---- */
.wg-way-a { border-left: 3px solid #22c55e; padding: 6px 14px 6px 16px; background: #f0fdf4; border-radius: 4px; }
.wg-way-b { border-left: 3px solid #6366f1; padding: 6px 14px 6px 16px; background: #eef2ff; border-radius: 4px; }
.wg-way-a h4 { color: #166534; }
.wg-way-b h4 { color: #3730a3; }

.wg-steps { margin: 6px 0; padding-left: 22px; }
.wg-steps > li { margin: 10px 0; }
.wg-steps > li > .wg-muted { margin-top: 4px; padding-left: 2px; }

.wg-muted { color: var(--text-muted, #64748b); font-size: 13px; }

/* ---- Prompt block (way B only) ---- */
.wg-prompt-block { margin-top: 12px; }
.wg-prompt-label { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.wg-prompt {
  position: relative;
  background: #0f172a; color: #e2e8f0;
  border-radius: 6px; padding: 14px 16px;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12.5px;
}
.wg-prompt pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
.wg-copy-btn {
  position: absolute; top: 8px; right: 8px;
  background: rgba(255, 255, 255, 0.12);
  color: inherit;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 4px; padding: 4px 10px;
  cursor: pointer; font-size: 12px;
}
.wg-copy-btn:hover { background: rgba(255, 255, 255, 0.2); }

.wg-callout {
  background: var(--surface-alt, #f8fafc);
  padding: 14px 18px; border-radius: 6px;
}

code {
  background: var(--code-bg, rgba(0, 0, 0, 0.05));
  padding: 1px 6px; border-radius: 3px; font-size: 13px;
}

/* Narrow: stack the compare cards */
@media (max-width: 640px) {
  .wg-compare { grid-template-columns: 1fr; }
}
</style>
