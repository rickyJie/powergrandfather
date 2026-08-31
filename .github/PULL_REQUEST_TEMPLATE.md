<!--
Thanks for the PR! Small ones move faster; a 20-line PR is almost always
better than a 200-line PR. If you're changing something architectural,
please open an issue first so we can align on shape.
-->

## Summary

<!-- What does this change? One or two sentences. Reference the issue if
there is one (`Closes #NN`). -->

## Test plan

- [ ] `ruff check .` clean
- [ ] `pytest` passes locally (with `CSM_CLAUDE_ARGV='bash -i'` in env)
- [ ] Frontend builds (`cd frontend && npm run build`)
- [ ] Manual smoke: <describe the flow you clicked through>

## Screenshots

<!-- For any UI change, before/after screenshots make review much faster. -->

## Notes for the reviewer

<!-- Anything non-obvious about the approach, or trade-offs you want a
second opinion on. -->
