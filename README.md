<div align="center">

# PowerGrandFather

**Local-first mission control for Claude Code and Codex CLI**

<sub>Monitor parallel CLI agents · run validated workflows on schedule · step in from your phone</sub>

![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![Node 18+](https://img.shields.io/badge/node-18+-339933?logo=nodedotjs&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Vue 3](https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-only-003B57?logo=sqlite&logoColor=white)
![Single Process](https://img.shields.io/badge/deploy-single--process-blue)
![License MIT](https://img.shields.io/badge/license-MIT-black)

<sub>[中文说明](README.zh-CN.md)</sub>

</div>

![A session streaming, next to the rest of the fleet](docs/screenshots/demo.gif)

---

> *"That just sounds like slavery with extra steps."*

Five tmux windows. You cycle through them to find out who finished, who
crashed, and who has been sitting there for ten minutes waiting for you to
answer a yes/no question. Those are the extra steps.

PowerGrandFather collects those windows into one page — who is running, who
is stuck, who is burning money — and then does the part that matters: it lets
the agents **work while you are not watching**. Put a job on a cron, go to
bed, and only get woken for the things that genuinely need you.

## What you get

- **One screen for N sessions** — no window cycling; crashed and
  waiting-on-you are marked in the list
- **Work that happens overnight** — describe a multi-step job, put it on a
  cron, read the result in the morning
- **A way back in from your phone** — install it to the home screen, answer
  permission prompts from bed
- **Somewhere the money went** — broken down by repo, model and tool, with a
  warning before you hit the ceiling

## Proven in daily use

This is not a weekend dashboard demo. Before the public release, the
maintainer dogfooded PowerGrandFather for two months on a private instance:

| Internal dogfooding, through 2026-08-31 | Result |
|---|---:|
| Managed Claude Code and Codex sessions | 356 |
| Cumulative agent time | 350+ hours |
| Workflow stage executions | 95 / 103 succeeded |
| Feedback items | 59 / 75 resolved |

These are aggregate figures from the maintainer's own instance, not telemetry
collected from public users. See [how the numbers were counted](docs/dogfooding.md).

## Quick start

Public CI currently covers Ubuntu. Other platforms are not yet part of the
compatibility guarantee. You need Git, Python 3.11+, Node 18+, conda, and
Claude Code and/or Codex CLI already installed and signed in.

```bash
git clone https://github.com/rickyJie/powergrandfather.git
cd powergrandfather
conda create -n csm python=3.11 -y
conda activate csm
pip install -e .
alembic upgrade head
npm --prefix frontend ci
npm --prefix frontend run build
./scripts/start.sh
```

Open `http://localhost:8000`. Stop it with `./scripts/stop.sh`.

> **Naming note:** PowerGrandFather is the product name. The Python package,
> conda environment and `CSM_*` settings retain the original internal name,
> **CSM (Claude Session Manager)**, for compatibility. They are the same app.

<details>
<summary>Stuck? Let an agent install it</summary>

Paste this into a Claude Code or Codex session from the repository root:

> Read `LLMS.md` §0 in this repo's root and install PowerGrandFather by
> following it in order. Verify each step with the check command in §0, and
> tell me the URL when it is up.

It handles the conda environment, alembic multiple heads, port conflicts and
frontend build failures on its own.

To confirm by hand:

```bash
curl -sf http://localhost:8000/api/health -H 'X-CSM-Client: 1' | head -c 80
curl -sf http://localhost:8000/ | grep -q 'id="app"' && echo "SPA OK"
```

</details>

<details>
<summary><strong>Why "PowerGrandFather"?</strong></summary>

Rick Sanchez is literally "powerful grandfather". One man, one spaceship, one
portal gun, running N cross-dimensional experiments at once — no team, no
Redis, no permission. This tool is built on that principle: **one process,
one SQLite file, N agents**. You're Morty. Grandpa watches the sessions.

</details>

---

## Why this exists

**A CLI agent is a coworker you can only talk to through a terminal window.**
That's fine for one. At three, you start losing track. At eight, the terminal
multiplexer becomes the bottleneck: the state you need — who's blocked, what
this cost, what it did last night — isn't in any of those windows, because a
terminal has no memory and no way to tell you anything when you're not
looking at it.

Everything here follows from taking that seriously.

### It runs when you don't

The interesting thing an agent can do isn't answer faster. It's **finish a
job you're not present for**. That requires more than putting a prompt on a
timer:

- a job is **stages**, not one prompt — each with declared outputs
- every stage's output is **checked before the next one starts** (does the
  file exist, does the JSON match the schema, does the report actually have
  the sections it claims)
- when something dies mid-flight — the process vanishes, an event is lost, a
  stage hangs — a reaper reconciles it, retries what's retryable and buries
  what isn't, so nothing sits in "running" forever
- when it's done, **a cheap model reads the result first** and decides whether
  this is worth waking you for

That last one is the whole point. An automation that pages you every time is
just a noisier cron.

### Subtraction, not addition

The obvious architecture for the above is Postgres for state, Redis for the
queue, Airflow for the schedule, a message broker between the workers, and
Kubernetes to hold it up.

There is one user. You.

So: **one uvicorn process, one SQLite file.** No broker, no external
scheduler, no second datastore. A typed event stream and one in-memory pub/sub
bus connect the modules. `./scripts/start.sh`
is the whole deployment story, and `csm.db` is the whole backup story. The
reasoning is written down in [ADR-0002](docs/decisions/0002-single-process-monolith.md)
if you want to argue with it.

The cost of this is real and stated up front: no multi-user, no auth model, no
horizontal anything. Those aren't on the roadmap; they're the trade.

### It was built by the thing it manages

The first version of this tool was not written by hand. It was written by
Claude Code, cron-driven and unattended, over a 27-hour window — about 9-10
hours of actual work — producing the skeleton, the end-to-end fixes and the
feature passes. Four P0/P1 bugs came up along the way; it found, reverted and
redid all four itself. The public repository is a sanitized source snapshot,
so its short commit history is not the development history; the publishable
timeline and methodology are summarized in the
[dogfooding notes](docs/dogfooding.md).

Since then every feature has been built by using PowerGrandFather to manage
the Claude sessions that build PowerGrandFather. That's not a marketing line,
it's just the development process — and it's why the ergonomics are shaped the
way they are. Anything annoying about running eight agents at once got fixed,
because it was annoying to the person running eight agents at once to build
this.

---

## How it compares

There are good tools in this space and they are not trying to be this one.
Facts below were checked against each project's own README on 2026-08-31; the
judgement calls are mine.

**[Orca](https://github.com/stablyai/orca)** — a native desktop ADE (Electron,
MIT). Its idea is **git worktree isolation**: every agent task gets its own
worktree and branch, so you can fan one prompt across several agents and merge
whichever result you like. 30+ CLI agents, a background PTY daemon, GitHub and
Linear integration, mobile companions. It has no scheduling or unattended
execution — Orca is for driving a fleet **while you sit there**.

**[claudecodeui / CloudCLI](https://github.com/siteboon/claudecodeui)** — a
responsive web UI for Claude Code, Cursor CLI and Codex (AGPL-3.0). Built
remote-first: self-host it or use their managed cloud, and it works properly
on a phone. It reads `~/.claude` rather than keeping its own store, and a
Scheduler plugin runs workspace-scoped scheduled prompts.

**Where this one is different** is the depth of the unattended loop, not its
existence. A scheduled prompt is one step that runs on a timer. A mission here
is a validated multi-stage pipeline with per-stage output contracts, crash
recovery, and a review agent between the result and your notification. That
plus a real event bus and history — which is what makes token attribution,
budgets and natural-language alerts possible at all — is the trade this
project made in exchange for being narrower everywhere else.

| | PowerGrandFather | Orca | claudecodeui |
|---|---|---|---|
| **Shape** | one process + a browser tab | native desktop app | web UI, self-host or managed cloud |
| **Built for** | your desk, all day | your desk, all day | phone / tablet / remote |
| **Unattended work** | multi-stage missions, per-stage validation, auto-recovery, review-before-notify | — | scheduled prompts (plugin) |
| **Parallel isolation** | in place, in your real repos | **a worktree + branch per task** | in place |
| **History** | SQLite + event bus: past runs, token attribution, budgets, NL alerts | usage + rate-limit readout | reads `~/.claude` |
| **Agents** | Claude Code, Codex | **30+** | Claude Code, Cursor, Codex |
| **Integrations** | Lark, Prometheus | **GitHub, Linear** | — |

### Pick another tool if

- you want **git worktree fan-out** — run the same prompt five ways and diff
  the results. Orca does this well; this project doesn't do it at all
- you use **many different agent CLIs** and want one UI over all of them
- **your phone or tablet is the primary surface**, not a companion to a desk
- you want a **managed, hosted** thing rather than a process on your own box
- you need **more than one user**

### Pick this one if

- you have **several sessions open at once**, most days
- you want jobs that **run overnight** and only interrupt you when a reviewer
  agent thinks they should
- you want to see **what happened last week** and **what will happen tonight**,
  not just what's on screen now
- you care where the tokens went, **per repo, per model, per tool**
- you'd rather run **one process against one SQLite file** than install a stack

### What this deliberately doesn't do

Multi-user or any auth model beyond a shared token · git worktree isolation ·
a hosted/cloud version · a quota-percentage metric ([ADR-0001](docs/decisions/)
explains why) · replacing your editor.

---

## First three things

**① Open a session** — Sessions → `+ New session`, pick a directory, pick
Claude Code or Codex, hit enter. It's a real terminal, identical to typing
`claude` in iTerm — except now something is watching it for you.

**② Let it tell you when things break** — nothing to configure. Crashes,
permission prompts and finished turns all light the bell in the corner. To
push those to your phone, put a Lark chat id in `Settings › Lark`.

**③ Write an automation** — Automation → `+ New workflow`, and describe what
you want in plain language ("every night, scan docs for paragraphs that have
drifted from the code and list them"). The agent asks a few scoping questions,
then writes the YAML. `Launch mission` to try it once; `Add schedule` when
you're happy.

---

# Features

## 🖥 Sessions

> *"There's an infinite number of realities, Morty."*
> Infinitely many parallel universes; one screen.

**What it does** — real terminals running Claude Code or Codex in your
browser, as many as you like. The list shows whether each one is working,
waiting on you, or dead, and which files it has touched recently. Reconnecting
doesn't lose the screen.

![Session list](docs/screenshots/sessions-hero.png)

**Getting started**

1. `+ New session` → choose a working directory → choose an agent
2. The rail groups by directory automatically; drag sessions into a Project,
   or pin the ones you care about
3. To pick up an old session: the History tab, then `/resume`
4. `Stop` exits gracefully (up to 15s); `Kill` when it won't

**When you'll want it** — the moment you're running more than two sessions and
have started forgetting which window is doing what.

<details>
<summary>Also in here</summary>

- **File paths in the terminal are clickable** — hover to linkify, click to
  preview. Code gets syntax highlighting and line numbers, Markdown renders
  (including maths), images display inline, and there's a Diff view for what
  changed this run
- **Unread markers** — sessions with new output go red; you can also flag one
  manually as "come back to this"
- **Crash detection** — when the process dies the row flips to `crashed` and a
  notification fires, without you noticing first
- **Proxy environment carried through** — sniffed once at startup, then
  attached to every session you open

</details>

## 🎯 Automation

> *"What, so everyone's supposed to sleep every single night?"*
> You sleep. Grandpa doesn't.

**What it does** — describe a multi-step job (run lint → fix what's fixable →
run tests → write a report), put it on a cron, and it runs itself every night.
Each step's output is checked before the next one starts — does the file
exist, is it long enough, does it have the sections it's supposed to — and a
failure stops the line. When it finishes, a small model reads the result and
**only notifies you if a human is actually needed**.

![Workflows](docs/screenshots/workflows.png)

**Getting started**

1. Automation → `+ New workflow`
2. Describe what you want in plain language; the agent asks a few questions to
   pin down the edges
3. It writes the YAML and reviews its own work; failures trigger a rewrite
4. `Launch mission` for a trial run — inspect each stage's output
5. Happy → `Add schedule` with a cron expression

![Workflow detail](docs/screenshots/workflow-detail.png)

**When you'll want it** — anything you already do by hand on a rhythm: nightly
regression, weekly digest, pre-release checklist.

<details>
<summary>Also in here</summary>

- **Not right? Have the agent fix it** — the workflow detail page has a
  one-shot `agent edit`; say what to change. For anything gnarlier, open a
  full debug session and iterate over several turns
- **Dry run first** — `preview` renders the prompts without spending anything
- **Wait for external systems** — besides "have an agent do this", there's
  "wait for this": poll a shell check until it passes, for training jobs, CI,
  or anything else you don't control
- **It recovers from its own failures** — vanished processes, lost events and
  stuck stages get reconciled every 30 seconds, retried or buried; no mission
  hangs in "running" forever
- **Hand-written YAML works too** — drop it in `tasks/` and hit `Reload`. The
  format is documented in [`docs/workflow_authoring_guide.md`](docs/workflow_authoring_guide.md)

> *"I'm not looking for judgment, just a yes or no."*
> That's the only question the review agent answers: does a human need to see
> this one.

</details>

## 🧑‍💻 Agent deck

> *"Sometimes science is more art than science, Morty."*
> Prompt tuning, basically. Once it's good, stop retyping it.

**What it does** — save the agents you use constantly (code reviewer, commit
message writer, API designer) as cards, so the long prompt you tuned doesn't
get pasted again every time. Each card has its own conversation, and the
history stays.

![Agent deck](docs/screenshots/agent-deck.png)

**Getting started**

1. Agents → `+ New Agent`
2. Name, icon, working directory
3. The prompt can be pasted, or given as a file path or URL — then `Refresh`
   pulls the latest
4. Click the card to start talking

**When you'll want it** — when there are two or three prompts you've now
pasted more than five times.

## 🔔 Notifications

> *"Wubba lubba dub dub!"*
> Which in the show actually means "I am in great pain, please help me" —
> exactly what a crashed session is saying.

**What it does** — crashes, permission prompts, failed automations, finished
turns waiting on a reply, and token ceilings all collect in one bell. Push
them to Lark, and tapping the notification on your phone lands you in the
session that needs you.

![Notifications](docs/screenshots/notifications.png)

**Getting started**

1. The panel is on by default; `j` / `k` move through unread, `Esc` closes
2. To reach your phone: `Settings › Lark`, add a chat id, send the test message
3. For the links in those messages to open: set `CSM_PUBLIC_BASE_URL`

**When you'll want it** — as soon as you start walking away from the desk but
still want to know how it's going.

<details>
<summary>It tries not to nag</summary>

- **One notification per turn** — the same event arriving from two sources is
  deduplicated
- **Permission prompts clear themselves** — grant the permission and the
  "waiting for you" notification marks itself read
- **It won't ask twice** — especially in multi-agent sync: say once that two
  sides are allowed to differ and it stops asking
- **Desktop notifications** — if the browser allows it, the important ones
  surface through the OS

</details>

## 📊 Tokens and budgets

> *"Don't think about it."*
> The one thing here you can't not think about.

**What it does** — shows where the tokens went, split by repo, by model and by
tool, with 24-hour trends and cache hit rates. Set budgets and get warned
before you cross them. Write alert rules in plain language and an agent turns
them into a check script.

![Tokens](docs/screenshots/tokens.png)

**Getting started**

1. Tokens → pick a window (1h / 5h / 24h / 7d / 30d); the three tables below
   are repo ranking, tool spend and per-session detail
2. For a budget: Budgets → new, choose a scope (global / a project / a model)
   and a period (rolling 5h / day / week / month)
3. For something smarter: bottom of the Tokens page → `+ Custom rule` → write
   "opus burned more than 5M tokens in the last 5 hours and the cache hit rate
   is under 30%" → it generates a script and **dry-runs it against your
   current data first** → save once you've seen what it would have said

![Agent alerts](docs/screenshots/agent-alerts.png)

**When you'll want it** — the first time a bill makes you say "how much?"

<details>
<summary>Also in here</summary>

- **Alerts that explain themselves** — tick `have an agent diagnose the cause`
  and, when it fires, it takes another pass over the diagnostic data. You get
  "session abc123 burned 480K tokens looping Bash in that directory, opus is
  80% of it, sonnet would be 3× cheaper" instead of "threshold exceeded"
- **Presets** — if you don't want to write one: 5h spend too high, cache
  efficiency dropped, one session burning tokens. Click enable
- **CSV export** — `↓CSV` on the Tokens page
- **Prometheus** — `/api/metrics` is in the scrape format
- **Absolute numbers only, no "percent of quota"** — there's no stable
  official quota API, and an estimated percentage lies exactly when it matters
  most, so it isn't built ([ADR-0001](docs/decisions/))

</details>

## 🔄 Multi-agent sync

> *"Peace among worlds."*
> The Council of Ricks agrees internally before anything else happens.

**What it does** — keeps your instructions (the CLAUDE.md family), MCP servers
and skills consistent between Claude Code and Codex. Change one, the others
follow; where they genuinely disagree, it brings the conflict to you.

![Sync](docs/screenshots/sync.png)

**Getting started**

1. `Settings › Sync`
2. **Sync** tab: source, target, tick what to migrate, go
3. **Conflicts** tab: differences land here and you pick a winner — including
   "leave them different"
4. **Log** tab: what synced, and when

**When you'll want it** — when you're using two or more CLI agents and have
already copy-pasted config between them.

<details>
<summary>Two things worth knowing</summary>

- **No API key needed** — decisions in automatic mode run through the CLI
  you're already logged into, so this works even if you only have a Codex
  subscription and no separate API credit
- **It remembers what you said** — decide once that two sides should differ
  and it stops asking; but if the other side later changes, the situation has
  changed, and it raises it again

</details>

## 🧰 Odds and ends

> *"I turned myself into a pickle, Morty!"*
> Some things exist because they could.

| | |
|---|---|
| **File preview** | Paths in the terminal open. Syntax highlighting with line numbers, rendered Markdown (with maths), full-screen HTML preview, inline images, plus a Diff view |
| **Backup** | `Settings › Backup` packages the database and workflow YAMLs in one click, safe to run while things are live. Unpack it on another machine to restore |
| **Worktime** | Two numbers in the top bar: today and all-time, with human time and agent time counted separately. Note it **accumulates** — 3 agents running a minute each is 3 minutes; it measures investment, not wall time |
| **Projects** | Sessions and workflows can both be filed under a project, which helps once there are a lot of them |

![File preview](docs/screenshots/file-preview.png)

---

## 📱 On your phone

> A notification is the portal gun — tap it and land in the session that needs
> you.

There's a separate mobile UI at `/m/`, and it's a real PWA: add it to the home
screen and you get fullscreen, an icon and no address bar, like a native app.
**One build serves iOS and Android** — nothing to publish, nothing to sign.

**Getting started**

```bash
./mobile/scripts/start_with_mobile.sh     # serves the desktop and mobile UIs together
```

Then:

1. **Set a token** — `export CSM_ACCESS_TOKEN=$(openssl rand -hex 24)`
2. **Get a real certificate** — iOS requires trusted HTTPS for PWA install and
   **rejects self-signed**. The least work is Caddy with a one-line
   `reverse_proxy 127.0.0.1:8000` and Let's Encrypt
3. **Open** `https://<your domain>/m/?token=<that token>` — the token goes
   into a cookie, so you only type it once
4. **Add to home screen** — iOS: Safari share → "Add to Home Screen";
   Android: Chrome menu → "Install app"

The phone build is scoped as a **session companion**: send and receive
messages, watch output arrive, answer permission prompts and multiple-choice
questions. It deliberately has no token charts or workflow authoring — those
stay on the desktop.

<details>
<summary>Don't want to expose anything? Use an SSH tunnel (Android and iOS)</summary>

If you'd rather not run a reverse proxy or open a port, let the phone reach a
backend bound to `127.0.0.1:8000` through SSH local forwarding (`-L`) and
browse it there. Both platforms have a workable answer:

- **Android** — the `pgf-connector` client in this repo bundles the SSH tunnel,
  brings it up automatically and wraps a WebView, so you fill in a host and
  go rather than typing `ssh -L` by hand. It's a convenience wrapper, not the
  main path; the HTTPS route above works on Android too.
- **iOS** — use the open-source **sshview**
  ([GitHub](https://github.com/lithium0003/sshview) ·
  [App Store](https://apps.apple.com/us/app/sshview/id1620680161), free,
  LGPL-2.1). It is exactly "SSH port forwarding plus a built-in web viewer":
  set the server type to WebBrowser, forward `8000`, and CSM renders in its
  browser.
  > iOS suspends the tunnel soon after the app backgrounds or the screen
  > locks. Treat it as a **foreground-use + reconnect** tool; don't expect it
  > to survive in the background.

**Phone troubleshooting**

| Symptom | Cause |
|---|---|
| Won't load | The backend is bound to `127.0.0.1` only; you need a proxy or a tunnel |
| No "Install app" option | Not trusted HTTPS, so the PWA can't install |
| 401 | A token is set but the link doesn't carry `?token=` |
| Permanently disconnected | The tunnel dropped — check the backend is still alive |
| Stale UI | Service worker cache; hard refresh or reinstall |

</details>

---

## ⚙️ Configuration

Environment variables, all prefixed `CSM_`:

| Variable | Default | What it does |
|---|---|---|
| `CSM_HOST` / `CSM_PORT` | `127.0.0.1` / `8000` | Bind address. Local-only by default |
| `CSM_ACCESS_TOKEN` | empty | Access token. **Mandatory the moment anything but this machine can reach it** |
| `CSM_PUBLIC_BASE_URL` | empty | External address used in notification links (needed for phone links to open) |
| `CSM_DB_PATH` | `csm.db` | Database location. One backend per database |
| `CSM_CLAUDE_ARGV` | empty | Set to `bash -i` in tests to stand in for the real `claude` and not burn tokens |

The full list is in the configuration section of [`LLMS.md`](LLMS.md).

> [!WARNING]
> *"To live is to risk it all."* — not worth it here, though.
>
> **This is a single-user local tool with no permission model.** It binds
> `127.0.0.1` by default; for remote access the least-effort answer is an SSH
> tunnel (VS Code port forwarding or `ssh -L`). If you explicitly bind
> `0.0.0.0` and put it on a LAN, **anyone on that network can spawn arbitrary
> processes through it and read your entire filesystem**. If you do it anyway,
> set `CSM_ACCESS_TOKEN` first, and not on a network you don't trust.
>
> <sub>*If you expose it on the LAN and the person at the next desk kills you,
> that's not on Grandpa, Morty.*</sub>

---

## ❓ FAQ

<details>
<summary><strong>Does it call the Claude API for me? Will it cost extra?</strong></summary>

No. It runs the `claude` / `codex` binaries already on your machine, with your
login and your allowance. CSM holds no API key of its own.

The one exception is the "does a human need to see this" judgement after an
automation finishes, which uses a cheap small model — fractions of a cent per
run, and it can be turned off.

</details>

<details>
<summary><strong>Can several people share one instance?</strong></summary>

No. There are no users and no isolation; one database belongs to one person.
That's a deliberate trade — see
[ADR-0002](docs/decisions/0002-single-process-monolith.md).

</details>

<details>
<summary><strong>Why won't a second backend start?</strong></summary>

One database allows one backend. Two processes fighting over the same SQLite
file corrupt unread counts and session bindings, so startup takes a lock and
refuses the second one, naming the process that holds it.

If you really want two, give them different `CSM_DB_PATH` values.

</details>

<details>
<summary><strong>If I close the browser, do sessions die?</strong></summary>

No. Sessions are independent processes forked by the backend; the browser is
just a display. Close the tab, restart the browser, connect from a different
machine — they're all still running, and a 1 MB scrollback buffer means you
can still read what you missed.

</details>

<details>
<summary><strong>Where is my data, and how do I move it?</strong></summary>

All of it is one SQLite file, `csm.db` in the project root, plus the workflow
YAMLs under `tasks/`. `Settings › Backup` downloads both as one archive;
unpack it elsewhere, run `alembic upgrade head`, and you're restored.

</details>

<details>
<summary><strong>Is it Claude Code only?</strong></summary>

Codex CLI works too — pick it from the dropdown when opening a session. They
behave the same in the UI. To wire up a different CLI, see
[`docs/backends/adding_a_new_adapter.md`](docs/backends/).

</details>

---

## 📚 More documentation

| What you want | Where |
|---|---|
| **How it works inside** (event stream / state machines / lifecycle / extension points) | [`LLMS.md`](LLMS.md) — written for models and for people |
| Architecture diagram, module graph, data flow | [`docs/architecture.md`](docs/architecture.md) |
| curl examples for every endpoint | [`docs/USAGE.md`](docs/USAGE.md) |
| How to write a workflow YAML | [`docs/workflow_authoring_guide.md`](docs/workflow_authoring_guide.md) |
| Adding a new CLI backend | [`docs/backends/`](docs/backends/) |
| Why it's built this way | [`docs/decisions/`](docs/decisions/) |
| Known rough edges | [`docs/known_issues.md`](docs/known_issues.md) |

<sub>The screenshots above can be re-shot with `./scripts/shoot_docs.sh` — it
seeds fictional data, boots a throwaway backend, shoots, and tears the whole
thing down without touching your real database.</sub>

---

## License

MIT — see [LICENSE](LICENSE).

<div align="center">
<sub><em>"Peace among worlds, Morty."</em></sub>
</div>
