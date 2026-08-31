"""Backend-specific argv builders for `SessionManager.create_session`.

The main flow in `manager.py::create_session` is Claude-heavy legacy code —
extracting a full `SessionSpawner` protocol / ABC would require touching
many branches with real regression risk. Instead this module keeps
backend-specific *argv construction* separate; the main function still
owns process spawn, DB commit, reader loop, etc.

Contract:
    - `build_claude_argv(...)` mirrors the pre-existing behaviour exactly.
      The main function keeps calling it in the Claude path; nothing about
      the Claude side changes.
    - `build_codex_argv(...)` is new. Emits a codex-cli invocation with:
        * `-C <cwd>` (working directory)
        * `-s workspace-write` (default sandbox mode; caller can override)
        * `--dangerously-bypass-approvals-and-sandbox` when
          `allow_dangerous=True` (mirrors user policy of
          --dangerously-skip-permissions for claude — CSM is the single
          source of truth for permission decisions).
        * `-c model_reasoning_effort="xhigh"` unless the caller already
          supplied an explicit reasoning-effort config override.
        * `--csm-no-trust-workspace` STRIPPED (CSM-only marker) and
          surfaced as `CodexArgvResult.trust_workspace=False`. Negative on
          purpose: trust defaults ON so clients that never send argv (the
          mobile dialog) still get it. codex's parser rejects unknown args,
          so the marker must never reach the process.
        * NO `--session-id` (codex doesn't support it — session id is
          discovered post-hoc from the rollout file's `session_meta`
          record; the tailer writes it back to Session.codex_rollout_path).
        * NO `--append-system-prompt` (codex has no such flag as of
          v0.145.0; upstream issue openai/codex#11588). Callers that need
          system-prompt injection must use AGENTS.md in cwd or the first
          user message — this is a documented shortcoming, not something
          we can paper over here.

    - Neither builder writes any files. Hook-config writing (`.codex/hooks.json`)
      is P5, done by the caller after argv is built.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClaudeArgvResult:
    argv: list[str]
    external_session_id: str | None  # set only for `claude` binary paths


# CSM-only marker, never passed to codex. Deliberately a NEGATIVE flag:
# pre-trusting is the default so that clients which know nothing about it
# still get it. The mobile New Session dialog sends only {cwd, agent,
# initial_prompt} — no argv at all — and that is precisely the surface where
# the trust modal is invisible and does the most damage. A positive
# opt-in marker would have skipped it entirely.
#
# It lives in argv because that is the surface the user sees and edits (the
# New Session "Command" field), the same way the enforced `--dangerously-*`
# flag is visible there.
NO_TRUST_WORKSPACE_FLAG = "--csm-no-trust-workspace"


@dataclass
class CodexArgvResult:
    argv: list[str]
    # Caller must pre-trust `cwd` in the codex config before spawning.
    # Codex refuses to start a turn in a directory absent from its
    # `[projects]` table: it opens a folder-trust modal that the CSM chat
    # surface never renders, so the session hangs and the first message is
    # consumed answering a dialog the user cannot see.
    trust_workspace: bool = True
    # codex has no --session-id; the real id is read from the rollout file
    # after spawn. This field is always None at build time.
    codex_session_id: None = None
    # True iff `initial_prompt` was actually appended to `argv` (only for
    # real `codex` binary; strict pass-through leaves it out). Callers
    # inspect this to decide whether to also fall back to PTY-write.
    prompt_appended: bool = False


def build_codex_argv(
    base_argv: list[str],
    cwd: str,
    *,
    initial_prompt: str | None = None,
    sandbox_mode: str = "workspace-write",
    allow_dangerous: bool = True,
    extra_args: list[str] | None = None,
    resume_from: str | None = None,
) -> CodexArgvResult:
    """Build a codex CLI argv given the base (typically `["codex"]`) + cwd.

    The default sandbox is `workspace-write` — matches codex's own default
    for interactive TUI. `allow_dangerous=True` adds
    `--dangerously-bypass-approvals-and-sandbox` so CSM sessions never
    block on codex's own approval prompts (parallel to claude's
    `--dangerously-skip-permissions`).

    Non-codex `base_argv[0]` (e.g. `["bash", "-i"]` for tests) is passed
    through UNCHANGED — `initial_prompt` and codex-only flags are dropped.
    Rationale: appending an arbitrary prompt string to `["bash", "-i"]`
    turns it into `["bash", "-i", "<prompt>"]` which bash interprets as a
    script path (or worse, leaks the prompt to `/proc/<pid>/cmdline`).
    Callers that need to inject content into a non-codex argv must do so
    explicitly.
    """
    argv = list(base_argv)
    # Strip the CSM marker before anything else: codex's clap parser rejects
    # unknown arguments outright, so leaking it would turn a UI toggle into a
    # session that refuses to start.
    trust_workspace = NO_TRUST_WORKSPACE_FLAG not in argv
    if not trust_workspace:
        argv = [a for a in argv if a != NO_TRUST_WORKSPACE_FLAG]
    if not argv or argv[0] != "codex":
        # test/dev override — strict pass-through, no injection.
        return CodexArgvResult(argv=argv, trust_workspace=trust_workspace)

    if resume_from:
        # `codex resume [OPTIONS] <SESSION_ID> [PROMPT]`. Keep all managed
        # policy/config flags as resume-subcommand options and append the
        # external id only after those flags have been assembled.
        argv = [argv[0], "resume", *argv[1:]]

    # -C <cwd> is codex's own "set working root" flag; keeps codex from
    # trying to guess the workspace from process cwd (which we ALSO set
    # via subprocess spawn — belt and suspenders).
    if "-C" not in argv and "--cd" not in argv:
        argv = [argv[0], "-C", cwd] + argv[1:]

    if "-s" not in argv and "--sandbox" not in argv:
        argv = argv + ["-s", sandbox_mode]

    if allow_dangerous and "--dangerously-bypass-approvals-and-sandbox" not in argv:
        argv = argv + ["--dangerously-bypass-approvals-and-sandbox"]

    if extra_args:
        argv = argv + list(extra_args)

    # Codex has no dedicated reasoning-effort CLI flag; the supported
    # command-line form is a config override. Preserve an explicit caller
    # choice (including `--config=...`) and otherwise start at xhigh.
    has_reasoning_effort = any(
        (
            arg in {"-c", "--config"}
            and i + 1 < len(argv)
            and argv[i + 1].split("=", 1)[0] == "model_reasoning_effort"
        )
        or arg.startswith("--config=model_reasoning_effort=")
        for i, arg in enumerate(argv)
    )
    if not has_reasoning_effort:
        argv = argv + ["-c", 'model_reasoning_effort="xhigh"']

    if resume_from:
        argv.append(resume_from)

    prompt_appended = False
    if initial_prompt is not None:
        # codex exec / non-interactive takes the prompt as trailing positional.
        # For interactive `codex` the same positional acts as the initial user
        # turn; both branches accept it here.
        argv = argv + [initial_prompt]
        prompt_appended = True

    return CodexArgvResult(
        argv=argv,
        prompt_appended=prompt_appended,
        trust_workspace=trust_workspace,
    )
