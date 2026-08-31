# Contributing to PowerGrandFather

Thanks for taking the time to look at this. PowerGrandFather started as a
single-user tool, and every accepted contribution should keep that shape:
one process, one SQLite file, one browser tab.

## Ways to contribute

- **Bug reports** — anything that breaks the "launch it and it works" flow
  described in the README. Please include your OS, Python version, Node
  version, and the last 20 lines of `csm.log`.
- **Small fixes** — typos, docstring clarifications, one-file bug fixes.
  Open a PR, no issue needed.
- **New features** — please open an issue first to discuss scope. The tool
  deliberately avoids scope creep; features that pull in a new datastore
  (Redis, Postgres, message brokers) will generally be declined — see
  `docs/decisions/0002-single-process-monolith.md`.
- **Docs** — architecture notes, gotcha writeups, tutorials. Docs live in
  `docs/`; the authoring manual for workflow YAMLs is
  `docs/workflow_authoring_guide.md`.

## Getting a dev environment

```bash
# Backend
conda create -n csm python=3.11 -y
conda activate csm
pip install -e ".[dev]"
alembic upgrade head

# Frontend
cd frontend && npm install && cd ..
```

Run the backend in one terminal, the vite dev server in another:

```bash
./scripts/dev.sh   # spawns both
```

## Before you push

- `ruff check .` and `ruff format .` should be clean.
- `pytest` should pass. `CSM_CLAUDE_ARGV='bash -i'` should be in your env
  so tests don't burn real Claude tokens (any test that spawns `claude`
  will use that argv instead).
- Frontend build should succeed: `cd frontend && npm run build`.
- Do not commit private paths, personal emails, or secrets. Enable the
  bundled pre-commit hook once per clone:

  ```bash
  git config core.hooksPath scripts/git-hooks
  ```

  It refuses commits containing a curated list of known-bad patterns
  (private paths, personal identifiers, internal business names) and
  runs `ruff check` on staged Python files. Extend the pattern list in
  `scripts/git-hooks/pre-commit` if you notice a new class of leak.

## Commit style

- One logical change per commit. Small commits are strictly better than
  large ones.
- Message subject: `<area>: <what changed>`, e.g. `feat(auto): ...` /
  `fix(sessions): ...` / `docs(readme): ...`. Body optional but appreciated
  for anything non-obvious.
- Reference the issue number if there is one.

## PR review

We aim to respond within a week. Small PRs (< 100 lines diff) usually
get merged same day if CI is green. Larger PRs may go through one or two
rounds of feedback.

## Code of conduct

Be respectful. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
