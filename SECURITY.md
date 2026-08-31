# Security Policy

## Scope

PowerGrandFather is a single-user, single-machine tool. Its threat model
is:

- **You** are the only expected user; there is no authentication.
- **Loopback is both the intended and the default bind.** `settings.host`
  is `127.0.0.1`; reaching the console from another machine is meant to go
  through an SSH tunnel. Setting `CSM_HOST=0.0.0.0` opts into LAN exposure
  and is unsafe on an untrusted network.
- **Any process on your machine that can dial `127.0.0.1:8000`** can
  spawn arbitrary child processes via `POST /api/sessions` (the endpoint
  accepts an `argv` override). Do not run PowerGrandFather on a machine
  where you don't trust every process.

Given the above, the following are **not** considered vulnerabilities:

- No login / role separation.
- The API accepts and executes user-supplied argv when spawning sessions.

## What is a vulnerability

- Anything that lets a **remote** caller (not on `127.0.0.1`) execute
  code, read files, or read the SQLite DB.
- A path-traversal, SQLi, or shell-injection bug exploitable from the
  public API surface without an `argv` override.
- Any way for a low-privilege user on the same machine to escalate to
  arbitrary code execution as the user running the backend.

## Reporting

Please **do not open a public GitHub issue** for a security bug.

Use GitHub's private vulnerability reporting: **Security → Report a
vulnerability** on this repository. That channel is private to the
maintainers until an advisory is published, and it keeps the report,
the fix, and the CVE request in one place.

Please include:

- A short description of the impact.
- Steps to reproduce (or a proof-of-concept).
- Your assessment of severity (low / medium / high / critical).

We aim to acknowledge within 3 business days and issue a fix within
14 days for high/critical issues.

## Disclosure

We follow **coordinated disclosure**: once a fix is released, the
reporter is credited (with permission) in the release notes.
