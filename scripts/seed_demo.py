"""Seed a throwaway CSM database with clean, fictional demo data.

Why this exists
---------------
The screenshots in `README.md` / `docs/` used to be shot against the
author's live `csm.db`, which meant every image leaked real project
names, real paths, and (in the workflow list) a real person's name. That
is fine for a private branch and unacceptable for the public one.

This script builds a *fictional* dataset — a generic web-shop with a
`webapp` frontend and a `platform-api` backend — so `scripts/shoot_docs.sh`
can boot a disposable backend against it and re-shoot every documentation
image with zero personal data in frame.

Everything here is invented. If you change the UI and the screenshots go
stale, re-run `scripts/shoot_docs.sh` rather than hand-editing images.

Usage (normally you don't call this directly — see scripts/shoot_docs.sh):

    CSM_DB_PATH=/tmp/pgf-demo.db alembic upgrade head
    python scripts/seed_demo.py --db /tmp/pgf-demo.db \
        --session-output-dir /tmp/pgf-demo-output
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make `csm.*` importable when run straight from a checkout without an
# editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from csm.models import (  # noqa: E402
    AgentAlertRule,
    AgentDefinition,
    Budget,
    Instruction,
    McpServer,
    Mission,
    Notification,
    Project,
    RawTokenEvent,
    Run,
    Skill,
    SyncConfig,
    ToolInvocation,
    UsageSnapshot,
    WorkflowDefinition,
    WorkInterval,
)
from csm.models.budget import BudgetAction, BudgetPeriod, BudgetScopeType  # noqa: E402
from csm.models.mission import MissionStatus  # noqa: E402
from csm.models.notification import NotificationType  # noqa: E402
from csm.models.run import RunStatus  # noqa: E402
from csm.models.session import Session, SessionStatus, SessionType  # noqa: E402
from csm.models.work_interval import (  # noqa: E402
    WorkInterval as _WI,
)
from csm.models.work_interval import (
    WorkIntervalKind,
    WorkIntervalSource,
)
from csm.models.workflow_definition import WorkflowReviewStatus  # noqa: E402
from sqlalchemy import create_engine, delete  # noqa: E402
from sqlalchemy.orm import Session as OrmSession  # noqa: E402

_ = _WI  # re-export guard: keep the explicit import for readers

# --------------------------------------------------------------------------
# Fictional universe. Nothing below maps to a real repo, person, or host.
# --------------------------------------------------------------------------

HOME = "/home/dev"
WEBAPP = f"{HOME}/code/webapp"
API = f"{HOME}/code/platform-api"
DESIGN = f"{HOME}/code/design-system"

# Deterministic session ids so the shooter can deep-link to a known row.
SID_CHECKOUT = "11111111-1111-4111-8111-111111111111"
SID_FLAKY = "22222222-2222-4222-8222-222222222222"
SID_AUTH = "33333333-3333-4333-8333-333333333333"
SID_LINT = "44444444-4444-4444-8444-444444444444"
SID_TESTGATE = "55555555-5555-4555-8555-555555555555"
SID_VITE = "66666666-6666-4666-8666-666666666666"
SID_CODEX = "77777777-7777-4777-8777-777777777777"
SID_DIGEST = "88888888-8888-4888-8888-888888888888"


# --------------------------------------------------------------------------
# Terminal transcripts
# --------------------------------------------------------------------------
# `SessionManager.output_snapshot()` falls back to
# `<session_output_dir>/<sid>.ansi` for sessions that are no longer live, so
# writing these files gives the screenshot run a realistic-looking terminal
# without spawning a real `claude` process.

R = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
BLUE = "\x1b[34m"
GREEN_BG = "\x1b[42m\x1b[30m"
RED_BG = "\x1b[41m\x1b[37m"


def _checkout_transcript() -> str:
    return "\r\n".join(
        [
            "",
            f"{GREEN}●{R} {BOLD}Read{R}({CYAN}src/checkout/CartSummary.tsx{R})",
            f"  {DIM}⎿{R}  Read 148 lines",
            "",
            f"{GREEN}●{R} This component mixes tax calculation into rendering. Pulling the",
            "  arithmetic out into a pure function first, so the component only renders",
            "  — that way the tax rules become testable on their own.",
            "",
            f"{GREEN}●{R} {BOLD}Update{R}({CYAN}src/checkout/pricing.ts{R})",
            f"  {DIM}⎿{R}  Added {BOLD}34{R} lines, removed {BOLD}6{R} lines",
            f"    {DIM}41{R}    export function lineSubtotal(item: CartItem): Money {{",
            f"    {DIM}42{R}      return multiply(item.unitPrice, item.quantity)",
            f"    {DIM}43{R}    }}",
            f"  {RED_BG}  44 - export function total(cart: Cart) {{ /* TODO tax */ }}{R}",
            f"  {GREEN_BG}  44 + export function taxFor(cart: Cart, rules: TaxRules): Money {{{R}",
            f"  {GREEN_BG}  45 +   const bracket = rules.brackets.find(b => b.matches(cart.region)){R}",
            f"  {GREEN_BG}  46 +   if (!bracket) throw new UnknownRegionError(cart.region){R}",
            f"  {GREEN_BG}  47 +   return multiply(subtotal(cart), bracket.rate){R}",
            f"  {GREEN_BG}  48 + }}{R}",
            f"    {DIM}49{R}",
            "",
            f"{GREEN}●{R} {BOLD}Update{R}({CYAN}src/checkout/CartSummary.tsx{R})",
            f"  {DIM}⎿{R}  Added {BOLD}8{R} lines, removed {BOLD}29{R} lines",
            "",
            f"{GREEN}●{R} {BOLD}Bash{R}({DIM}npm run test -- checkout --coverage{R})",
            f"  {DIM}⎿{R}  Running… (12s)",
            f"     {DIM}(ctrl+b ctrl+b (twice) to run in background){R}",
            "",
            f"     {GREEN}PASS{R}  src/checkout/pricing.test.ts  {DIM}(23 tests){R}",
            f"     {GREEN}PASS{R}  src/checkout/CartSummary.test.tsx  {DIM}(11 tests){R}",
            f"     {DIM}Coverage: statements 94.1% · branches 88.7% · lines 94.6%{R}",
            "",
            f"{YELLOW}*{R} {YELLOW}Recalibrating…{R} {DIM}(1m 47s · ↓ 2.3k tokens · thought for 9s){R}",
            "",
            f"{DIM}{'─' * 96}{R}",
            "",
            f"{DIM}❯{R} ",
            "",
            f"  {MAGENTA}▶▶ bypass permissions on{R} {DIM}(shift+tab to cycle) · esc to interrupt{R}",
            "",
        ]
    )


def _flaky_transcript() -> str:
    return "\r\n".join(
        [
            "",
            f"{GREEN}●{R} {BOLD}Bash{R}({DIM}npx playwright test --repeat-each=20 login.spec.ts{R})",
            f"  {DIM}⎿{R}  Ran 20 iterations in 3m 04s",
            "",
            f"     {GREEN}17 passed{R} · {RED}3 failed{R}",
            f"     {DIM}all 3 failures on the same assertion:{R}",
            f"     {RED}expect(page.getByRole('alert')).toBeVisible(){R}",
            "",
            f"{GREEN}●{R} Reproduced — 3 in 20, a stable flake. Every failure lands on the",
            "  same assertion, so it isn't random: it's a race. The test asserts",
            "  before the toast has rendered.",
            "",
            f"{GREEN}●{R} {BOLD}Grep{R}({CYAN}pattern: \"waitFor|toBeVisible\", path: e2e/{R})",
            f"  {DIM}⎿{R}  Found 41 matches across 12 files",
            "",
            f"{GREEN}●{R} Root cause confirmed: `login.spec.ts` relies on an implicit wait,",
            "  but the toast component mounts 200ms late. The other 11 specs all call",
            "  `waitFor` explicitly — this one is the only miss.",
            "",
            "  Two options:",
            f"    {BOLD}a){R} add `await expect(...).toBeVisible({{ timeout: 5000 }})` right here",
            f"    {BOLD}b){R} make the toast's mount delay injectable and set it to 0 in tests",
            "",
            f"  {BOLD}b{R} is the more thorough fix but touches product code. Which do you want?",
            "",
            f"{DIM}{'─' * 96}{R}",
            "",
            f"{DIM}❯{R} ",
            "",
        ]
    )


def _lint_transcript() -> str:
    return "\r\n".join(
        [
            f"{DIM}[auto session · mission nightly_refactor · stage lint_sweep]{R}",
            "",
            f"{GREEN}●{R} {BOLD}Bash{R}({DIM}npm run lint -- --format json > .ws/lint.json{R})",
            f"  {DIM}⎿{R}  exit 1 · 63 problems (41 errors, 22 warnings)",
            "",
            f"{GREEN}●{R} {BOLD}Bash{R}({DIM}npm run lint -- --fix{R})",
            f"  {DIM}⎿{R}  47 problems auto-fixed, 16 remain",
            "",
            f"{GREEN}●{R} The remaining 16 each need a judgement call — nothing mechanical",
            "  left. Grouping them by kind into the report, so the next stage's gate",
            "  can decide whether to block.",
            "",
            f"{GREEN}●{R} {BOLD}Write{R}({CYAN}.ws/01-lint/report.md{R})",
            f"  {DIM}⎿{R}  Wrote 88 lines",
            "",
            f"{GREEN}✓{R} stage {BOLD}lint_sweep{R} outputs written",
            f"  {DIM}⎿{R}  {GREEN}✓{R} file_exists      .ws/01-lint/report.md",
            f"  {DIM}⎿{R}  {GREEN}✓{R} min_chars        report.md ≥ 200",
            f"  {DIM}⎿{R}  {GREEN}✓{R} required_sections  ## Auto-fixed / ## Needs a human",
            "",
        ]
    )


def _codex_transcript() -> str:
    return "\r\n".join(
        [
            f"{DIM}codex{R} {BLUE}v0.47.0{R}  {DIM}model: gpt-5.5 · sandbox: workspace-write{R}",
            "",
            f"{BLUE}▌{R} Add return types to every exported function in `src/api/client.ts`",
            "",
            f"{GREEN}●{R} apply_patch {CYAN}src/api/client.ts{R}",
            f"  {DIM}⎿{R}  14 hunks applied",
            "",
            f"{GREEN}●{R} shell {DIM}npx tsc --noEmit{R}",
            f"  {DIM}⎿{R}  exit 0 · no errors",
            "",
            f"{GREEN}●{R} All 14 exported functions are typed now. Three of them were",
            "  inferring `any`; those are narrowed to concrete response types and no",
            "  caller broke.",
            "",
        ]
    )


TRANSCRIPTS: dict[str, str] = {
    SID_CHECKOUT: _checkout_transcript(),
    SID_FLAKY: _flaky_transcript(),
    SID_LINT: _lint_transcript(),
    SID_CODEX: _codex_transcript(),
}


# --------------------------------------------------------------------------
# Workflow YAML bodies (real, schema-valid specs — the reviewer parses them)
# --------------------------------------------------------------------------

NIGHTLY_REFACTOR_YAML = """\
name: nightly_refactor
description: Nightly lint + type check, auto-fix what can be auto-fixed, run the
  test gate, and write up what changed.

parameters:
  - name: repo
    type: string
    default: "/home/dev/code/webapp"
    description: Root of the target repository.
  - name: fail_on_remaining
    type: int
    default: 20
    description: Fail the gate when more than this many items still need a human.

global_timeout: 5400s

stages:
  - name: lint_sweep
    kind: claude
    prompt: |
      Run lint in {params.repo}. Do one `--fix` pass first, then group whatever
      still needs a human judgement by kind.

      Artefact: {ws}/01-lint/report.md. It must contain both an
      `## Auto-fixed` and a `## Needs a human` section; every item in the
      latter carries a file:line.
    outputs:
      - "{ws}/01-lint/report.md"
    validation:
      - file: "{ws}/01-lint/report.md"
        primitives:
          - file_exists
          - min_chars: 200
          - required_sections: ["## Auto-fixed", "## Needs a human"]

  - name: type_check
    kind: claude
    depends_on: [lint_sweep]
    prompt: |
      Read {stages.lint_sweep.outputs[0]} to see what the previous step
      changed, then run the type checker in {params.repo}. Fix type errors the
      lint auto-fix introduced; leave unrelated pre-existing errors alone.

      Artefact: {ws}/02-types/result.json, shaped:
      {{"fixed": <int>, "remaining": <int>, "files": [<string>]}}
    outputs:
      - "{ws}/02-types/result.json"
    validation:
      - file: "{ws}/02-types/result.json"
        primitives:
          - file_exists
          - jsonschema:
              type: object
              required: [fixed, remaining, files]
              properties:
                fixed: {type: integer}
                remaining: {type: integer}
                files: {type: array}

  - name: test_gate
    kind: poll
    depends_on: [type_check]
    poll_interval: 30s
    timeout: 1800s
    check:
      - file: "{ws}/03-test/junit.xml"
        primitives:
          - file_exists
          - regex_match:
              pattern: 'failures="0"'
"""

WEEKLY_DIGEST_YAML = """\
name: weekly_digest
description: Weekly roll-up of commits, closed issues and dependency changes into
  a markdown digest you can send as-is.

parameters:
  - name: days_back
    type: int
    default: 7
    description: How many days back to summarise.

global_timeout: 3600s

stages:
  - name: collect
    kind: claude
    prompt: |
      Collect the last {params.days_back} days of commits, closed issues and
      dependency version changes.
      Artefact: {ws}/01-collect/raw.json
    outputs:
      - "{ws}/01-collect/raw.json"
    validation:
      - file: "{ws}/01-collect/raw.json"
        primitives:
          - file_exists

  - name: write_digest
    kind: claude
    depends_on: [collect]
    prompt: |
      Read {stages.collect.outputs[0]} and turn it into a digest a non-engineer
      can follow. Lead with what changed, why, and who it affects — do not
      paste a list of commit hashes.
    outputs:
      - "{ws}/02-digest/digest.md"
    validation:
      - file: "{ws}/02-digest/digest.md"
        primitives:
          - file_exists
          - required_sections: ["## Highlights", "## Who is affected", "## Next week"]
"""

DOCS_FRESHNESS_YAML = """\
name: docs_freshness
description: Scan docs/ for passages that have drifted from the code and list what
  needs updating. Read-only — it never edits the docs.

parameters:
  - name: docs_dir
    type: string
    default: "docs"

global_timeout: 2400s

stages:
  - name: scan
    kind: claude
    prompt: |
      Read each markdown file under {params.docs_dir} against the current code
      and find the claims that no longer hold — renamed functions, deleted
      endpoints, changed defaults.

      Artefact: {ws}/01-scan/stale.md. Each entry states where in the docs it
      is, what the code actually does now, and the suggested fix.
    outputs:
      - "{ws}/01-scan/stale.md"
    validation:
      - file: "{ws}/01-scan/stale.md"
        primitives:
          - file_exists
          - min_chars: 120
"""

DEP_AUDIT_YAML = """\
name: dep_audit
description: Dependency vulnerability scan with graded upgrade advice; the critical
  ones get called out separately.

parameters:
  - name: severity_floor
    type: string
    default: "moderate"

global_timeout: 3600s

stages:
  - name: audit
    kind: claude
    prompt: |
      Run the dependency audit and drop anything below {params.severity_floor}.
      For each remaining vulnerability, judge whether the affected code path is
      actually reachable here.

      Artefact: {ws}/01-audit/findings.md, which must carry both a
      `## Critical` and a `## Can wait` section.
    outputs:
      - "{ws}/01-audit/findings.md"
    validation:
      - file: "{ws}/01-audit/findings.md"
        primitives:
          - file_exists
          - required_sections: ["## Critical", "## Can wait"]
"""


def _review_report(passed: int) -> dict:
    """A realistic R9-R19 structural review report blob."""
    rules = [
        ("R9", "every stage has a unique name"),
        ("R10", "output paths use only declared placeholders"),
        ("R11", "depends_on points at an existing upstream stage"),
        ("R12", "a poll stage's check uses only known primitives"),
        ("R13", "a claude stage must declare outputs"),
        ("R14", "validation paths stay inside the workspace"),
        ("R15", "every param reference resolves to a ParameterSpec"),
        ("R16", "the stage dependency graph is acyclic"),
        ("R17", "global_timeout is declared and sane"),
        ("R18", "prompts are non-empty and describe their artefacts"),
        ("R19", "output paths do not collide"),
    ]
    return {
        "pass": True,
        "checks": [
            {"rule": rid, "title": title, "status": "pass"} for rid, title in rules[:passed]
        ],
        "warn_count": 0,
        "fail_count": 0,
    }


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def wipe(db: OrmSession) -> None:
    """Clear every table this script writes, so re-runs are idempotent."""
    for model in (
        Notification,
        ToolInvocation,
        RawTokenEvent,
        UsageSnapshot,
        WorkInterval,
        Run,
        Mission,
        WorkflowDefinition,
        Project,
        AgentDefinition,
        AgentAlertRule,
        Budget,
        Instruction,
        McpServer,
        Skill,
        SyncConfig,
        Session,
    ):
        db.execute(delete(model))
    db.commit()


def seed_sessions(db: OrmSession, now: datetime) -> None:
    rows = [
        Session(
            id=SID_CHECKOUT,
            title="Refactor checkout pricing",
            type=SessionType.INTERACTIVE,
            cwd=WEBAPP,
            status=SessionStatus.RUNNING,
            pid=48213,
            started_at=now - timedelta(minutes=54),
            last_activity_ts=now - timedelta(seconds=20),
            unread_count=0,
            agent="claude",
            pinned=True,
            current_tool="Bash",
            last_assistant_msg="Pulled the tax maths into a pure function; the component only renders now.",
        ),
        Session(
            id=SID_FLAKY,
            title="Fix the flaky login e2e",
            type=SessionType.INTERACTIVE,
            cwd=WEBAPP,
            status=SessionStatus.WAITING_INPUT,
            pid=48377,
            started_at=now - timedelta(minutes=38),
            last_activity_ts=now - timedelta(minutes=2),
            unread_count=2,
            agent="claude",
            last_assistant_msg="It's a race. Two ways to fix it — which do you want?",
        ),
        Session(
            id=SID_AUTH,
            title="Chase down the 502 alert",
            type=SessionType.INTERACTIVE,
            cwd=API,
            status=SessionStatus.WAITING_AUTH,
            pid=48501,
            started_at=now - timedelta(minutes=16),
            last_activity_ts=now - timedelta(minutes=1),
            unread_count=1,
            agent="claude",
            current_tool="Bash",
            last_assistant_msg="Needs permission to run kubectl logs.",
        ),
        Session(
            id=SID_LINT,
            title="nightly_refactor · lint_sweep",
            type=SessionType.AUTO,
            cwd=WEBAPP,
            status=SessionStatus.RUNNING,
            pid=48622,
            started_at=now - timedelta(minutes=9),
            last_activity_ts=now - timedelta(seconds=45),
            agent="claude",
            current_tool="Write",
        ),
        Session(
            id=SID_TESTGATE,
            title="nightly_refactor · type_check",
            type=SessionType.AUTO,
            cwd=WEBAPP,
            status=SessionStatus.EXITED,
            started_at=now - timedelta(hours=2, minutes=10),
            ended_at=now - timedelta(hours=1, minutes=42),
            exit_code=0,
            agent="claude",
        ),
        Session(
            id=SID_DIGEST,
            title="weekly_digest · collect",
            type=SessionType.AUTO,
            cwd=API,
            status=SessionStatus.EXITED,
            started_at=now - timedelta(hours=6),
            ended_at=now - timedelta(hours=5, minutes=31),
            exit_code=0,
            agent="claude",
        ),
        Session(
            id=SID_VITE,
            title="Upgrade Vite 5 → 6",
            type=SessionType.INTERACTIVE,
            cwd=DESIGN,
            status=SessionStatus.EXITED,
            started_at=now - timedelta(hours=20),
            ended_at=now - timedelta(hours=19),
            exit_code=0,
            agent="claude",
        ),
        Session(
            id=SID_CODEX,
            title="Add return types to the api client",
            type=SessionType.INTERACTIVE,
            cwd=API,
            status=SessionStatus.EXITED,
            started_at=now - timedelta(hours=3, minutes=20),
            ended_at=now - timedelta(hours=2, minutes=55),
            exit_code=0,
            agent="codex",
        ),
    ]
    db.add_all(rows)
    db.commit()


def seed_workflows(db: OrmSession, now: datetime, tasks_dir: Path) -> dict[str, str]:
    specs = [
        ("nightly_refactor", NIGHTLY_REFACTOR_YAML, 11,
         "Nightly lint + type check, auto-fix what can be, run the test gate, "
         "and write up what changed."),
        ("weekly_digest", WEEKLY_DIGEST_YAML, 11,
         "Weekly roll-up of commits, closed issues and dependency changes into "
         "a markdown digest you can send as-is."),
        ("docs_freshness", DOCS_FRESHNESS_YAML, 10,
         "Scan docs/ for passages that have drifted from the code and list what "
         "needs updating. Read-only."),
        ("dep_audit", DEP_AUDIT_YAML, 10,
         "Dependency vulnerability scan with graded upgrade advice; the critical "
         "ones called out separately."),
    ]
    ids: dict[str, str] = {}
    for i, (name, yaml_body, passed, desc) in enumerate(specs):
        path = tasks_dir / f"{name}.workflow.yaml"
        path.write_text(yaml_body, encoding="utf-8")
        wf = WorkflowDefinition(
            name=name,
            description=desc,
            file_path=str(path),
            yaml_content=yaml_body,
            review_status=WorkflowReviewStatus.PASSED,
            review_report=_review_report(passed),
            reviewed_at=now - timedelta(days=2, hours=i),
            created_at=now - timedelta(days=20 - i * 3),
            updated_at=now - timedelta(days=2, hours=i),
        )
        db.add(wf)
        db.flush()
        ids[name] = wf.id
    db.commit()
    return ids


def seed_missions(db: OrmSession, now: datetime, wf_ids: dict[str, str]) -> None:
    missions = [
        Mission(
            workflow_def_id=wf_ids["nightly_refactor"],
            parameters={"repo": WEBAPP, "fail_on_remaining": 20},
            workspace_path="/home/dev/.csm/missions/nightly_refactor/2026-08-23T02-00",
            status=MissionStatus.RUNNING,
            current_stage="lint_sweep",
            started_at=now - timedelta(minutes=9),
            audit_log=[
                {"ts": (now - timedelta(minutes=9)).isoformat(), "event": "mission.started"},
                {"ts": (now - timedelta(minutes=9)).isoformat(), "event": "stage.started", "stage": "lint_sweep"},
            ],
        ),
        Mission(
            workflow_def_id=wf_ids["nightly_refactor"],
            parameters={"repo": WEBAPP, "fail_on_remaining": 20},
            workspace_path="/home/dev/.csm/missions/nightly_refactor/2026-08-22T02-00",
            status=MissionStatus.SUCCEEDED,
            current_stage="test_gate",
            started_at=now - timedelta(days=1, minutes=12),
            ended_at=now - timedelta(days=1) + timedelta(minutes=31),
        ),
        Mission(
            workflow_def_id=wf_ids["weekly_digest"],
            parameters={"days_back": 7},
            workspace_path="/home/dev/.csm/missions/weekly_digest/2026-08-19T09-00",
            status=MissionStatus.SUCCEEDED,
            current_stage="write_digest",
            started_at=now - timedelta(hours=6),
            ended_at=now - timedelta(hours=5, minutes=12),
        ),
        Mission(
            workflow_def_id=wf_ids["dep_audit"],
            parameters={"severity_floor": "moderate"},
            workspace_path="/home/dev/.csm/missions/dep_audit/2026-08-21T03-00",
            status=MissionStatus.FAILED,
            current_stage="audit",
            started_at=now - timedelta(days=2, hours=3),
            ended_at=now - timedelta(days=2, hours=2, minutes=41),
            failure_reason="stage 'audit' validation failed: required_sections — '## Can wait' missing",
        ),
    ]
    db.add_all(missions)
    db.flush()

    running, ok_yesterday, digest, failed = missions
    db.add_all(
        [
            Run(
                mission_id=running.id, stage_name="lint_sweep", session_id=SID_LINT,
                status=RunStatus.RUNNING, started_at=now - timedelta(minutes=9),
            ),
            Run(
                mission_id=ok_yesterday.id, stage_name="lint_sweep", session_id=SID_TESTGATE,
                status=RunStatus.SUCCEEDED, exit_code=0,
                started_at=now - timedelta(days=1, minutes=12),
                ended_at=now - timedelta(days=1, minutes=1),
            ),
            Run(
                mission_id=ok_yesterday.id, stage_name="type_check",
                status=RunStatus.SUCCEEDED, exit_code=0,
                started_at=now - timedelta(days=1) + timedelta(minutes=1),
                ended_at=now - timedelta(days=1) + timedelta(minutes=18),
            ),
            Run(
                mission_id=ok_yesterday.id, stage_name="test_gate",
                status=RunStatus.SUCCEEDED, exit_code=0,
                started_at=now - timedelta(days=1) + timedelta(minutes=18),
                ended_at=now - timedelta(days=1) + timedelta(minutes=31),
            ),
            Run(
                mission_id=digest.id, stage_name="collect", session_id=SID_DIGEST,
                status=RunStatus.SUCCEEDED, exit_code=0,
                started_at=now - timedelta(hours=6),
                ended_at=now - timedelta(hours=5, minutes=31),
            ),
            Run(
                mission_id=digest.id, stage_name="write_digest",
                status=RunStatus.SUCCEEDED, exit_code=0,
                started_at=now - timedelta(hours=5, minutes=31),
                ended_at=now - timedelta(hours=5, minutes=12),
            ),
            Run(
                mission_id=failed.id, stage_name="audit",
                status=RunStatus.FAILED, exit_code=0,
                review_note="Wrote '## Critical' but skipped '## Can wait'; validation caught it.",
                started_at=now - timedelta(days=2, hours=3),
                ended_at=now - timedelta(days=2, hours=2, minutes=41),
            ),
        ]
    )
    db.commit()


def seed_tokens(db: OrmSession, now: datetime) -> None:
    """24h of token events + tool invocations across fictional projects."""
    projects = [
        (WEBAPP, 0.34),
        (API, 0.27),
        (DESIGN, 0.14),
        (f"{HOME}/code/infra-tools", 0.13),
        (f"{HOME}/code/docs-site", 0.12),
    ]
    models = [("claude-opus-4-6", 0.62), ("claude-sonnet-4-6", 0.31), ("claude-haiku-4-5", 0.07)]
    sids = [SID_CHECKOUT, SID_FLAKY, SID_AUTH, SID_LINT, SID_VITE, SID_CODEX]

    # A deterministic pseudo-random walk — no `random` import so the shot is
    # byte-stable across runs.
    def wobble(i: int, lo: float, hi: float) -> float:
        x = ((i * 2654435761) % 10007) / 10007.0
        return lo + (hi - lo) * x

    events: list[RawTokenEvent] = []
    tools: list[ToolInvocation] = []
    n = 0
    for minute in range(0, 24 * 60, 6):
        ts = now - timedelta(minutes=minute)
        # Busier during the "work day", quiet overnight — gives the trend
        # chart a believable shape instead of a flat band.
        hour_of_day = ts.hour
        activity = 1.0 if 9 <= hour_of_day <= 23 else 0.22
        for pi, (proj, share) in enumerate(projects):
            n += 1
            if wobble(n, 0.0, 1.0) > activity * (0.35 + share):
                continue
            model = models[n % 3][0]
            base = int(120_000 * share * activity * wobble(n, 0.4, 1.9))
            cache_read = int(base * wobble(n + 1, 6.0, 12.0))
            cache_create = int(base * wobble(n + 2, 0.1, 0.5))
            out = int(base * wobble(n + 3, 0.02, 0.09))
            cost = (base * 3 + cache_create * 3.75 + cache_read * 0.3 + out * 15) / 1_000_000
            events.append(
                RawTokenEvent(
                    ts=ts,
                    external_session_id=f"demo-{pi}-{n % 17:02d}",
                    project_path=proj,
                    model=model,
                    input_tokens=base,
                    cache_creation_tokens=cache_create,
                    cache_read_tokens=cache_read,
                    output_tokens=out,
                    estimated_cost_usd=round(cost, 4),
                    csm_session_id=sids[n % len(sids)],
                    source="interactive" if n % 4 else "auto",
                    agent="codex" if n % 11 == 0 else "claude",
                )
            )
            tool_name = ["Bash", "Edit", "Read", "Write", "Grep", "Task", "Glob", "WebSearch"][n % 8]
            tools.append(
                ToolInvocation(
                    ts=ts,
                    tool_name=tool_name,
                    external_session_id=f"demo-{pi}-{n % 17:02d}",
                    project_path=proj,
                    csm_session_id=sids[n % len(sids)],
                    source="interactive" if n % 4 else "auto",
                    input_tokens=int(base * 0.6),
                    cache_creation_tokens=int(cache_create * 0.6),
                    cache_read_tokens=int(cache_read * 0.6),
                    output_tokens=int(out * 0.6),
                    estimated_cost_usd=round(cost * 0.6, 4),
                )
            )
    db.add_all(events)
    db.add_all(tools)

    db.add(
        UsageSnapshot(
            ts=now - timedelta(minutes=14),
            agent="claude",
            session_pct=39,
            session_reset="resets 3:59pm",
            week_pct=47,
            week_reset="resets Sun 12am",
            tier="max_5x",
            subscription_type="max",
            source="scheduled",
            duration_ms=2140,
        )
    )
    db.commit()


def seed_notifications(db: OrmSession, now: datetime) -> None:
    db.add_all(
        [
            Notification(
                type=NotificationType.SESSION_CRASHED,
                session_id=SID_AUTH,
                title="Permission required",
                body="Claude is waiting for your approval to use a tool.",
                created_at=now - timedelta(minutes=1),
                notif_metadata={"session_title": "Chase down the 502 alert", "kind": "permission"},
            ),
            Notification(
                type=NotificationType.NEW_MESSAGE,
                session_id=SID_FLAKY,
                title="1 new message",
                body="It's a race — the test asserts before the toast renders. Two ways to fix it; which do you want?",
                created_at=now - timedelta(minutes=2),
                notif_metadata={"session_title": "Fix the flaky login e2e"},
            ),
            Notification(
                type=NotificationType.AUTO_NEEDS_REVIEW,
                session_id=SID_TESTGATE,
                title="nightly_refactor wants a look",
                body="Supervisor: type_check reports remaining=18, close to the gate's "
                "threshold of 20 — the next run will probably be blocked. Worth going "
                "through the remaining items by hand first.",
                created_at=now - timedelta(hours=1, minutes=40),
                notif_metadata={"mission": "nightly_refactor", "verdict": "needs_review"},
            ),
            Notification(
                type=NotificationType.MISSION_DONE,
                title="weekly_digest finished",
                body="All 3 stages passed; produced digest.md (1.4 KB).",
                created_at=now - timedelta(hours=5, minutes=12),
                read_at=now - timedelta(hours=5),
                notif_metadata={"mission": "weekly_digest"},
            ),
            Notification(
                type=NotificationType.TOKEN_WARNING,
                title="5h spend too high",
                body="412M tokens in the last 5 hours, past the 400M threshold. "
                "Biggest source: webapp (38%) · the Bash tool accounts for 54%.",
                created_at=now - timedelta(hours=3, minutes=8),
                read_at=now - timedelta(hours=3),
                notif_metadata={"rule": "5h spend too high"},
            ),
            Notification(
                type=NotificationType.AUTO_RUN_FAILED,
                title="dep_audit failed",
                body="stage 'audit' validation failed: required_sections — '## Can wait' missing.",
                created_at=now - timedelta(days=2, hours=2, minutes=41),
                read_at=now - timedelta(days=2, hours=2),
                notif_metadata={"mission": "dep_audit"},
            ),
        ]
    )
    db.commit()


def seed_agents(db: OrmSession, now: datetime) -> None:
    db.add_all(
        [
            AgentDefinition(
                name="code-reviewer",
                display_name="Code Reviewer",
                icon="🔍",
                description="Review a diff against the project's conventions: naming, error handling, edge cases, test coverage.",
                cwd=WEBAPP,
                prompt_source=f"{HOME}/.config/prompts/code-reviewer.md",
                prompt_cached="You are a meticulous code reviewer…",
                created_at=now - timedelta(days=31),
            ),
            AgentDefinition(
                name="commit-writer",
                display_name="Commit Writer",
                icon="✍️",
                description="Read the staged diff and write a Conventional Commits message for it.",
                cwd=API,
                prompt_cached="You write commit messages…",
                created_at=now - timedelta(days=24),
            ),
            AgentDefinition(
                name="api-designer",
                display_name="API Designer",
                icon="🧭",
                description="Work back from use cases to a REST / RPC shape and emit an OpenAPI fragment.",
                cwd=API,
                prompt_source="https://prompts.internal.example/api-designer.md",
                prompt_cached="You design HTTP APIs…",
                created_at=now - timedelta(days=12),
            ),
            AgentDefinition(
                name="release-notes",
                display_name="Release Notes",
                icon="📦",
                description="Turn a run of commits into release notes a user can read.",
                cwd=DESIGN,
                prompt_cached="You write release notes…",
                created_at=now - timedelta(days=5),
            ),
        ]
    )
    db.commit()


def seed_budgets_and_alerts(db: OrmSession, now: datetime) -> None:
    db.add_all(
        [
            Budget(
                name="Daily ceiling",
                scope_type=BudgetScopeType.GLOBAL,
                period=BudgetPeriod.DAILY,
                token_limit=800_000_000,
                warn_pct=80.0,
                action=BudgetAction.WARN,
                notify_channel=["inapp", "lark"],
            ),
            Budget(
                name="webapp · 5h window",
                scope_type=BudgetScopeType.PROJECT,
                scope_value=WEBAPP,
                period=BudgetPeriod.WINDOW_5H,
                cost_limit=40.0,
                warn_pct=75.0,
                action=BudgetAction.WARN,
                notify_channel=["inapp"],
                last_state="ok",
            ),
            Budget(
                name="Automation sessions, monthly cap",
                scope_type=BudgetScopeType.SOURCE,
                scope_value="auto",
                period=BudgetPeriod.MONTHLY,
                cost_limit=300.0,
                warn_pct=90.0,
                action=BudgetAction.BLOCK,
                notify_channel=["inapp", "lark"],
            ),
        ]
    )

    script = (
        "def check(window):\n"
        "    total = window['total_tokens']\n"
        "    if total > 400_000_000:\n"
        "        return True, {'metric': 'total_tokens', 'value': total,\n"
        "                      'threshold': 400_000_000}\n"
        "    return False, {}\n"
    )
    db.add_all(
        [
            AgentAlertRule(
                name="5h spend too high",
                nl_description="Alert when total spend over the last 5 hours exceeds 400M tokens",
                threshold_spec={"metric": "total_tokens", "op": ">", "value": 400_000_000},
                check_script=script,
                poll_interval_sec=300,
                cooldown_sec=1800,
                channels=["inapp"],
                escalate=True,
                last_fired_at=now - timedelta(hours=3, minutes=8),
            ),
            AgentAlertRule(
                name="Cache efficiency dropped",
                nl_description="Alert when the cache hit rate is under 30% with more than 50M total",
                threshold_spec={"metric": "cache_hit_ratio", "op": "<", "value": 0.3},
                check_script=script,
                poll_interval_sec=300,
                cooldown_sec=3600,
                channels=["inapp"],
                last_fired_at=now - timedelta(days=4),
            ),
            AgentAlertRule(
                name="One session is burning tokens",
                nl_description="Alert when one session takes >=85% of the window and more than 100M tokens",
                threshold_spec={"metric": "session_share", "op": ">=", "value": 0.85},
                check_script=script,
                poll_interval_sec=180,
                cooldown_sec=900,
                channels=["inapp", "lark"],
                escalate=True,
            ),
            AgentAlertRule(
                name="opus burn warning",
                nl_description="Opus burned more than 5M tokens in the last 5 hours and the cache hit rate is under 30%",
                threshold_spec={"metric": "opus_tokens", "op": ">", "value": 5_000_000},
                check_script=script,
                enabled=False,
                poll_interval_sec=300,
                cooldown_sec=1800,
                channels=["inapp"],
            ),
        ]
    )
    db.commit()


def seed_sync(db: OrmSession, now: datetime) -> None:
    """Sync rows spanning every sentinel state so the matrix shows all glyphs."""
    db.add_all(
        [
            SyncConfig(module="memory", enrolled_agents=["claude", "codex"],
                       enabled=True, sync_mode="agent", tick_interval_minutes=30),
            SyncConfig(module="mcp", enrolled_agents=["claude", "codex"],
                       enabled=True, sync_mode="agent", tick_interval_minutes=30),
            SyncConfig(module="skills", enrolled_agents=["claude"],
                       enabled=True, sync_mode="agent", tick_interval_minutes=60),
        ]
    )
    ok = "a" * 64
    db.add_all(
        [
            Instruction(
                name="commit-style", title="Commit message style",
                body="Conventional Commits. Use the directory name as the scope, and let the body say why, not what.",
                share_scope=["claude", "codex"], priority=10, origin="csm",
                last_synced_hashes={"claude": ok, "codex": ok},
                created_at=now - timedelta(days=40),
            ),
            Instruction(
                name="test-policy", title="Testing policy",
                body="A behaviour change needs a test. A pure refactor doesn't, but say why it's safe.",
                share_scope=["claude", "codex"], priority=8, origin="claude",
                last_synced_hashes={"claude": ok, "codex": f"DIVERGED:{'b' * 64}"},
                created_at=now - timedelta(days=33),
            ),
            Instruction(
                name="no-force-push", title="No force pushing",
                body="Never force push a shared branch. If you need to rewrite history, branch first.",
                share_scope=["claude", "codex"], priority=9, origin="csm",
                last_synced_hashes={"claude": ok, "codex": "UNKNOWN"},
                created_at=now - timedelta(days=21),
            ),
        ]
    )
    db.add_all(
        [
            McpServer(
                name="filesystem", transport="stdio", command="npx",
                args_json=["-y", "@modelcontextprotocol/server-filesystem", HOME],
                enabled_for=["claude", "codex"], origin="csm",
                last_synced_hashes={"claude": ok, "codex": ok},
            ),
            McpServer(
                name="postgres", transport="stdio", command="npx",
                args_json=["-y", "@modelcontextprotocol/server-postgres"],
                env_json={"DATABASE_URL": "postgres://localhost:5432/dev"},
                enabled_for=["claude"], origin="claude",
                last_synced_hashes={"claude": ok, "codex": "UNKNOWN"},
            ),
            McpServer(
                name="sentry", transport="http", url="https://mcp.sentry.example/v1",
                enabled_for=["claude", "codex"], origin="csm",
                last_synced_hashes={"claude": ok, "codex": ok},
            ),
        ]
    )
    db.add_all(
        [
            Skill(
                name="repo-onboarding",
                description="Survey an unfamiliar repo and write up how it's put together.",
                body_md="# repo-onboarding\n\nWalk the directory tree, find the entry points, map the module graph…\n",
                share_scope=["claude"], origin="csm",
                last_synced_hashes={"claude": ok, "codex": "UNSUPPORTED"},
            ),
            Skill(
                name="release-checklist",
                description="Pre-release checklist: migrations, rollback plan, announcement.",
                body_md="# release-checklist\n\n1. Is the migration reversible?\n2. Is it behind a feature flag?\n",
                share_scope=["claude"], origin="claude",
                last_synced_hashes={"claude": ok, "codex": "UNSUPPORTED"},
            ),
        ]
    )
    db.commit()


def seed_worktime(db: OrmSession, now: datetime) -> None:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [
        WorkInterval(kind=WorkIntervalKind.HUMAN, start_ts=midnight + timedelta(hours=9),
                     end_ts=midnight + timedelta(hours=11, minutes=20),
                     source=WorkIntervalSource.HEARTBEAT),
        WorkInterval(kind=WorkIntervalKind.HUMAN, start_ts=midnight + timedelta(hours=13, minutes=30),
                     end_ts=midnight + timedelta(hours=15, minutes=5),
                     source=WorkIntervalSource.HEARTBEAT),
        WorkInterval(kind=WorkIntervalKind.HUMAN, start_ts=now - timedelta(minutes=27),
                     end_ts=None, source=WorkIntervalSource.HEARTBEAT),
        WorkInterval(kind=WorkIntervalKind.AGENT, session_id=SID_VITE,
                     start_ts=now - timedelta(hours=20), end_ts=now - timedelta(hours=19),
                     source=WorkIntervalSource.EVENT),
        WorkInterval(kind=WorkIntervalKind.AGENT, session_id=SID_TESTGATE,
                     start_ts=now - timedelta(hours=2, minutes=10),
                     end_ts=now - timedelta(hours=1, minutes=42),
                     source=WorkIntervalSource.EVENT),
        WorkInterval(kind=WorkIntervalKind.AGENT, session_id=SID_CHECKOUT,
                     start_ts=now - timedelta(minutes=54), end_ts=None,
                     source=WorkIntervalSource.EVENT),
        WorkInterval(kind=WorkIntervalKind.AGENT, session_id=SID_LINT,
                     start_ts=now - timedelta(minutes=9), end_ts=None,
                     source=WorkIntervalSource.EVENT),
    ]
    db.add_all(rows)
    db.commit()


def write_transcripts(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for sid, text in TRANSCRIPTS.items():
        (out_dir / f"{sid}.ansi").write_text(text, encoding="utf-8")


# Statuses that `SessionManager.startup_reap_orphans` rewrites on boot,
# keyed by the seeded session. Every live-looking status is in its filter
# (RUNNING / WAITING_INPUT / WAITING_AUTH / …) and the seeded pids belong to
# no living process, so booting the demo backend turns the whole fleet
# CRASHED — an empty Active tab and a "Session ended" hero. That reaper is
# correct: on a real restart those processes ARE gone. So rather than weaken
# it, `--restore-live` re-applies the intended statuses AFTER the backend is
# up. The periodic reap tick only downgrades ORPHANED rows, so these stick.
LIVE_STATUSES = {
    SID_CHECKOUT: SessionStatus.RUNNING,
    SID_FLAKY: SessionStatus.WAITING_INPUT,
    SID_AUTH: SessionStatus.WAITING_AUTH,
    SID_LINT: SessionStatus.RUNNING,
}


def restore_live(db_path: Path, pids: list[int] | None = None) -> int:
    """Re-apply the intended live statuses after the backend has booted.

    Status alone is not enough to keep a session looking alive. TWO reapers
    check pid liveness and rewrite a live-looking row to CRASHED:
    `SessionManager.startup_reap_orphans` on boot, and `GET /api/sessions`
    on every single list request. Both are correct — a row claiming RUNNING
    with a dead pid IS a zombie — which is why the demo hands them pids of
    real processes the harness keeps alive for the duration of the shoot.
    Without that, the very request that renders the Sessions page is the one
    that marks the whole fleet crashed, and the hero shot shows an empty
    Active tab.
    """
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    restored = 0
    with OrmSession(engine) as db:
        for i, (sid, status) in enumerate(LIVE_STATUSES.items()):
            row = db.get(Session, sid)
            if row is None:
                continue
            row.status = status
            row.ended_at = None
            if pids:
                # Cycle if the caller supplied fewer pids than sessions —
                # several rows sharing one live pid is fine here; the reapers
                # only ask "is this pid alive".
                row.pid = pids[i % len(pids)]
            restored += 1
        db.commit()
    return restored


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the demo SQLite file")
    ap.add_argument("--session-output-dir",
                    help="where to write the fabricated .ansi transcripts")
    ap.add_argument("--tasks-dir",
                    help="where to write the demo workflow YAMLs")
    ap.add_argument("--restore-live", action="store_true",
                    help="only re-apply live session statuses (run AFTER the "
                         "demo backend has booted and reaped them)")
    ap.add_argument("--live-pids", default="",
                    help="comma-separated pids of processes that will stay "
                         "alive for the shoot; assigned to the live demo "
                         "sessions so the pid-liveness reapers leave them be")
    args = ap.parse_args()

    db_path = Path(args.db).resolve()
    if args.restore_live:
        pids = [int(x) for x in args.live_pids.split(",") if x.strip()]
        print(json.dumps({"restored": restore_live(db_path, pids)}))
        return
    if not args.session_output_dir or not args.tasks_dir:
        ap.error("--session-output-dir and --tasks-dir are required when seeding")
    tasks_dir = Path(args.tasks_dir).resolve()
    tasks_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.utcnow().replace(microsecond=0)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with OrmSession(engine) as db:
        wipe(db)
        seed_sessions(db, now)
        wf_ids = seed_workflows(db, now, tasks_dir)
        seed_missions(db, now, wf_ids)
        seed_tokens(db, now)
        seed_notifications(db, now)
        seed_agents(db, now)
        seed_budgets_and_alerts(db, now)
        seed_sync(db, now)
        seed_worktime(db, now)

    write_transcripts(Path(args.session_output_dir).resolve())

    print(json.dumps({
        "db": str(db_path),
        "tasks_dir": str(tasks_dir),
        "session_output_dir": str(Path(args.session_output_dir).resolve()),
        "sessions": len(TRANSCRIPTS),
        "workflows": list(wf_ids),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
