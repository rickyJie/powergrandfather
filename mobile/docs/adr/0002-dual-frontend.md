# ADR-0002: Dual frontend projects (desktop + mobile)

- **Status**: Accepted (2026-08-14)
- **Deciders**: llj

## Context

The mobile UI needs to cover the same underlying data as the desktop UI
but with fundamentally different interaction primitives:

| Desktop | Mobile |
|---|---|
| Sidebar navigation | Bottom tabbar |
| xterm PTY terminal | Text message stream (no xterm) |
| Modal dialogs | Full-screen popups + drawers |
| Mouse pointer + keyboard | Touch + virtual keyboard |
| 1600px+ layout | 375-500px portrait viewport |

Two paths considered:

1. **Responsive single codebase** — extend the existing desktop
   `frontend/` to serve both form factors via CSS breakpoints and
   conditional rendering.
2. **Two independent Vue projects** — `frontend/` for desktop stays as
   is, new `mobile/frontend/` for mobile, sharing only API contracts and
   types via Vite path aliases.

## Decision

**Two independent Vue projects** under `frontend/` and `mobile/frontend/`,
sharing the desktop `api/*.ts` and `types/*.ts` layers via Vite
`@shared-api` / `@shared-types` aliases. No npm workspace, no shared
package — the alias approach keeps the main repo `package.json`
untouched and there is no cross-project build coupling.

## Rationale

- **xterm cannot become responsive**: The xterm component is the highest
  risk regression surface in desktop; wrapping it in a conditional was
  ruled out for stability.
- **Interaction models are too different**: The View layer would end up
  with `if (isMobile)` scattered everywhere, which is worse than two
  purpose-built codebases.
- **API contracts are stable**: The `api/*.ts` layer has been unchanged
  for months; sharing it via alias is safe. If the contract breaks,
  both frontends need updating anyway.
- **Main repo zero-touch**: A workspace would require modifying root
  `package.json`, which the user explicitly asked to avoid. Aliases
  avoid this entirely.

## Consequences

- Two frontends must be kept in sync when backend contracts change.
  Mitigation: shared types via alias make TypeScript errors surface in
  both projects immediately.
- No code sharing at the view / component / store layer. This is
  deliberate — mobile primitives (Vant) differ from desktop (custom /
  headless UI).
- Slightly larger overall repo. Acceptable for a single-user product.
- `mobile/frontend/tests/` (via symlink) reuses `mobile/frontend/node_modules`
  so vitest can resolve deps.

## Alternatives considered

| Option | Why not |
|---|---|
| Responsive single codebase | Risk of regression on the stable desktop; xterm makes it worse. |
| Extract `packages/api-client` npm workspace | Requires modifying main repo `package.json`; alias is enough for now. |
| Cross-mount identical routes with UA sniffing | Fragile; harder to test; adds runtime cost. |
