import { describe, it, expect } from "vitest";
import {
  detectPendingChoice,
  arrowSelectKeys,
  multiSelectKeys,
  submitAnswersKeys,
} from "../../../../frontend/src/lib/pendingChoice";
import type { TranscriptEvent } from "../../../../frontend/src/api/ws-events";

// Derive the raw key bytes from the exported helper so the tests never hand-type
// the ESC control sequence (which JSON-escaping mangles). arrowSelectKeys(1) is
// "<down><enter>", so its head is one down-arrow and its tail is Enter.
const ENTER = arrowSelectKeys(0); // just "\r"
const DOWN = arrowSelectKeys(1).slice(0, -ENTER.length); // one down-arrow
const SPACE = " ";

const toolStart = (tool: string, tool_id: string, input: unknown): TranscriptEvent =>
  ({ type: "tool_use_start", ts: "t", tool, tool_id, input } as never);
const toolResult = (tool_id: string): TranscriptEvent =>
  ({ type: "tool_use_result", ts: "t", tool_id, ok: true, preview: "" } as never);
const asst = (text: string): TranscriptEvent =>
  ({ type: "assistant_text", ts: "t", text } as never);

describe("detectPendingChoice", () => {
  it("returns null when there is no interactive tool_use", () => {
    expect(detectPendingChoice([asst("hi")])).toBeNull();
    expect(detectPendingChoice([toolStart("Bash", "b1", {})])).toBeNull();
  });

  it("extracts AskUserQuestion options from the tool input", () => {
    const input = {
      questions: [
        {
          question: "Pick one",
          options: [
            { label: "Alpha", description: "the a" },
            { label: "Beta" },
          ],
        },
      ],
    };
    const pc = detectPendingChoice([toolStart("AskUserQuestion", "q1", input)]);
    expect(pc).not.toBeNull();
    expect(pc!.kind).toBe("ask");
    expect(pc!.steps).toHaveLength(1);
    expect(pc!.steps[0].question).toBe("Pick one");
    expect(pc!.steps[0].multiSelect).toBe(false);
    expect(pc!.steps[0].options.map((o) => o.label)).toEqual(["Alpha", "Beta"]);
    expect(pc!.steps[0].options[0].desc).toBe("the a");
    // option 0 = just Enter; option 1 = one down + Enter
    expect(pc!.steps[0].options[0].keys).toBe(arrowSelectKeys(0));
    expect(pc!.steps[0].options[1].keys).toBe(arrowSelectKeys(1));
  });

  it("keeps EVERY sub-question of a multi-question AskUserQuestion", () => {
    const input = {
      questions: [
        { question: "Q1", options: [{ label: "A" }, { label: "B" }] },
        { question: "Q2", options: [{ label: "C" }] },
        { question: "Q3", options: [{ label: "D" }, { label: "E" }, { label: "F" }] },
      ],
    };
    const pc = detectPendingChoice([toolStart("AskUserQuestion", "q1", input)]);
    expect(pc!.steps).toHaveLength(3);
    expect(pc!.steps.map((s) => s.question)).toEqual(["Q1", "Q2", "Q3"]);
    // Each sub-question is a fresh picker → per-step index navigation resets.
    expect(pc!.steps[2].options[2].keys).toBe(arrowSelectKeys(2));
  });

  it("carries the multiSelect flag per sub-question", () => {
    const input = {
      questions: [
        { question: "Q1", multiSelect: true, options: [{ label: "A" }] },
        { question: "Q2", options: [{ label: "B" }] },
      ],
    };
    const pc = detectPendingChoice([toolStart("AskUserQuestion", "q1", input)]);
    expect(pc!.steps.map((s) => s.multiSelect)).toEqual([true, false]);
  });

  it("drops sub-questions that carry no options", () => {
    const input = {
      questions: [
        { question: "Q1", options: [{ label: "A" }] },
        { question: "Q2 (free text)", options: [] },
      ],
    };
    const pc = detectPendingChoice([toolStart("AskUserQuestion", "q1", input)]);
    expect(pc!.steps).toHaveLength(1);
    expect(pc!.steps[0].question).toBe("Q1");
  });

  it("offers fixed approve/keep choices for ExitPlanMode", () => {
    const pc = detectPendingChoice([toolStart("ExitPlanMode", "p1", { plan: "do x" })]);
    expect(pc!.kind).toBe("plan");
    expect(pc!.steps).toHaveLength(1);
    expect(pc!.steps[0].options).toHaveLength(2);
  });

  it("is cleared once the tool has a result (answered)", () => {
    const events = [
      toolStart("AskUserQuestion", "q1", { questions: [{ options: [{ label: "A" }] }] }),
      toolResult("q1"),
    ];
    expect(detectPendingChoice(events)).toBeNull();
  });

  it("only considers the MOST RECENT tool_use", () => {
    const events = [
      toolStart("AskUserQuestion", "q1", { questions: [{ options: [{ label: "A" }] }] }),
      toolStart("Bash", "b1", {}), // newer, non-interactive → no pending choice
    ];
    expect(detectPendingChoice(events)).toBeNull();
  });
});

describe("arrowSelectKeys", () => {
  it("builds down-arrows + Enter", () => {
    expect(arrowSelectKeys(0)).toBe(ENTER);
    expect(arrowSelectKeys(2)).toBe(DOWN + DOWN + ENTER);
  });
});

describe("multiSelectKeys", () => {
  it("ticks each chosen row top-down then drops to Next + Enter", () => {
    // 4 real options → "Type something" at row 4, "Next" at row 5. Ticking rows
    // 0 and 2: Space at 0, down×2 + Space at 2, down×3 to reach Next, Enter.
    expect(multiSelectKeys([0, 2], 4)).toBe(
      SPACE + DOWN.repeat(2) + SPACE + DOWN.repeat(3) + ENTER
    );
  });
  it("sorts + dedups indices so the cursor only moves downward", () => {
    expect(multiSelectKeys([2, 0, 2], 4)).toBe(multiSelectKeys([0, 2], 4));
  });
  it("with nothing ticked just walks to Next and confirms", () => {
    // 3 real options → Next at row 4; no toggles, just down×4 + Enter.
    expect(multiSelectKeys([], 3)).toBe(DOWN.repeat(4) + ENTER);
  });
});

describe("submitAnswersKeys", () => {
  it("is a bare Enter for the final Submit screen", () => {
    expect(submitAnswersKeys).toBe(ENTER);
  });
});
