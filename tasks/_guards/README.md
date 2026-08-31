# Shared guard helpers for real_*.workflow.yaml

Purpose: make the four "real production workflow" simulations safe to run on the
same machine as their live production instances. Every guard here is opt-in via
environment variable and defaults to the **safest** behaviour (mock / skip /
refuse), so a workflow that forgets to set an env var will not accidentally
mutate a production repo, push to a shared remote, or send a Lark message to a
real user.

## Guard inventory

| Guard | Script | Default | Env override to enable real-world side-effect |
|---|---|---|---|
| Git worktree lifecycle | `worktree_setup.sh`, `worktree_cleanup.sh` | Uses an isolated worktree; original working copy never mutated | n/a — always isolated |
| Git push guard | `git_push_guard.sh` | Refuses to push (prints "would push …") | `CSM_ALLOW_PUSH=1` |
| Lark notify | `lark_notify.sh` | Writes the message to a file under mission ws | `CSM_LARK_MODE=real` |
| Tmux socket per-mission | `tmux_socket_name.sh` | Prefixes socket with `csm-<mission_id>-` | n/a — always isolated |

All scripts are meant to be invoked from inside a `claude` stage's prompt.
The workflow YAML tells the claude stage to `bash tasks/_guards/<script>.sh …`
and pass the arguments it needs.

## Environment contract

The workflow launcher (`POST /api/missions/launch`) is expected to inject the
following env vars into the child claude process. Any missing var falls back to
the safe default.

```
CSM_ALLOW_PUSH        # "1" to allow real git push; anything else = refuse
CSM_LARK_MODE         # "real" to actually call lark-cli; anything else = mock
CSM_CLEARML_SUFFIX    # string appended to ClearML project name (default: "-csm-test")
CSM_TLAUNCH_PRIORITY  # "low" | "normal" | "high" (default: "low" for simulations)
CSM_MISSION_ID        # unique per mission — used for tmux socket / branch names
CSM_MISSION_WS        # absolute path to mission workspace
```

## Why guards instead of prompt-level warnings

Prompts drift, prompts get truncated, prompts are read imperfectly by claude.
Shell scripts are deterministic — the guard either allows the action or does
not, and the workflow YAML can `validate` on the guard's exit code + written
log file. Real-world side-effect operations should never rely on "the prompt
said don't push".

## Test recipe

For any real_*.workflow.yaml under test:

1. Launch with default env — verify no external side effect fires.
2. Launch with `CSM_ALLOW_PUSH=1` — verify push happens only to the
   dedicated `csm-test/*` branch.
3. Launch with `CSM_LARK_MODE=real` — verify only messages routed to a
   dedicated test chat/bot go out; a real user private chat never receives.
