import type { TranscriptEvent } from "@/api/ws-events";

// Detect when the session is blocked on an in-terminal picker whose options we
// can render as tappable buttons (rather than making the user drive a raw TUI).
//
// Source of options:
//   - AskUserQuestion: options are STRUCTURED in the tool_use input. A single
//     tool_use can bundle 1-4 questions; the CLI renders them as a tab bar and
//     walks through them one screen at a time, then shows a final "✔ Submit"
//     review screen that needs one more Enter to actually submit.
//   - ExitPlanMode: fixed approve/keep-planning choices (the plan text is in
//     the input, but the choice list is the CLI's, not the tool's).
//
// Answering has no dedicated API — we write raw key sequences to the PTY,
// calibrated against the real picker (hint line: "Enter to select · Tab/Arrow
// keys to navigate · Esc to cancel"). The multi-question state machine lives in
// ChatView because the transcript emits NOTHING between sub-questions (a single
// tool_use_result lands only after the final Submit), so progress is tracked
// client-side.

const DOWN = "[B"; // ESC [ B — down arrow
const ENTER = "\r";
const SPACE = " ";

/** Move the cursor from the first option down to `index`, then confirm. Used
 *  for single-select questions and plan approval: Enter picks the highlighted
 *  row and auto-advances to the next question (or the Submit screen). */
export function arrowSelectKeys(index: number): string {
  return DOWN.repeat(Math.max(0, index)) + ENTER;
}

/** Key sequence for ONE multi-select question. The picker starts at row 0 with
 *  Space toggling the highlighted checkbox; after ticking every chosen row we
 *  drop to the "Next" row (which sits just past the real options + the injected
 *  "Type something" row) and press Enter to advance. `indices` are toggled in
 *  ascending order so the cursor only ever moves downward. */
export function multiSelectKeys(indices: number[], optionCount: number): string {
  const sorted = [...new Set(indices)].filter((i) => i >= 0).sort((a, b) => a - b);
  let cursor = 0;
  let out = "";
  for (const target of sorted) {
    out += DOWN.repeat(target - cursor) + SPACE;
    cursor = target;
  }
  // "Type something" is injected at index optionCount, "Next" at optionCount+1.
  const nextRow = optionCount + 1;
  out += DOWN.repeat(Math.max(0, nextRow - cursor)) + ENTER;
  return out;
}

/** Final Enter on the "✔ Submit → Submit answers" review screen that closes a
 *  MULTI-question AskUserQuestion. Single-question prompts submit on the pick
 *  itself and never reach this screen. */
export const submitAnswersKeys = ENTER;

export interface ChoiceOption {
  label: string;
  desc?: string;
  /** Raw key sequence to pick this option in a SINGLE-select question. Unused
   *  for multi-select (the caller batches the whole selection via
   *  `multiSelectKeys`). */
  keys: string;
}

/** One picker screen (one sub-question). */
export interface ChoiceStep {
  question: string;
  options: ChoiceOption[];
  /** Multi-select questions render checkboxes: Space toggles, "Next" advances,
   *  instead of a single Enter picking-and-advancing. */
  multiSelect: boolean;
}

export interface PendingChoice {
  kind: "ask" | "plan";
  toolId: string;
  /** One entry per sub-question. `plan` always has exactly one step. Only a
   *  multi-question (`steps.length > 1`) AskUserQuestion has the trailing
   *  Submit screen — track that in ChatView via the step cursor. */
  steps: ChoiceStep[];
}

interface ToolStart {
  type: "tool_use_start";
  tool: string;
  tool_id: string;
  input: unknown;
}

export function detectPendingChoice(
  events: TranscriptEvent[]
): PendingChoice | null {
  const answered = new Set<string>();
  for (const e of events) {
    if (e.type === "tool_use_result") answered.add(e.tool_id);
  }
  // The most recent tool_use is what's blocking now; only it matters.
  let last: ToolStart | null = null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "tool_use_start") {
      last = events[i] as ToolStart;
      break;
    }
  }
  if (!last || answered.has(last.tool_id)) return null;

  if (last.tool === "AskUserQuestion") {
    const input = last.input as
      | {
          questions?: Array<{
            question?: string;
            multiSelect?: boolean;
            options?: Array<{ label?: string; description?: string }>;
          }>;
        }
      | undefined;
    // Keep EVERY question with options, not just the first. A single
    // AskUserQuestion routinely bundles 2-4 questions (~43% of real calls),
    // which the CLI presents as consecutive pickers. Dropping all but
    // questions[0] left the user able to answer only the first one — the tool
    // call then stalled forever because the remaining pickers never surfaced.
    const steps: ChoiceStep[] = (input?.questions ?? [])
      .filter((q) => q?.options?.length)
      .map((q) => ({
        question: q.question || "Select an option",
        multiSelect: !!q.multiSelect,
        options: (q.options ?? []).map((o, idx) => ({
          label: o.label || `Option ${idx + 1}`,
          desc: o.description,
          // Each sub-question is a fresh picker whose cursor starts at option
          // 0, so per-step down-to-index navigation is correct.
          keys: arrowSelectKeys(idx),
        })),
      }));
    if (steps.length) {
      return { kind: "ask", toolId: last.tool_id, steps };
    }
  }

  if (last.tool === "ExitPlanMode") {
    return {
      kind: "plan",
      toolId: last.tool_id,
      steps: [
        {
          question: "Approve this plan?",
          multiSelect: false,
          options: [
            { label: "Yes, proceed", keys: arrowSelectKeys(0) },
            { label: "No, keep planning", keys: arrowSelectKeys(1) },
          ],
        },
      ],
    };
  }

  return null;
}
