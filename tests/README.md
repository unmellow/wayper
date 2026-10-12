# Lean regression suite

Run the complete suite; there is no separate optional or legacy suite:

```sh
uv run pytest -q
cd wayper/electron && npm test
```

Keep tests for observable contracts: file safety and recovery, feedback labels,
CLI/API behavior, platform integration, startup/shutdown, and concurrency or
cache regressions that can cause repeated actions to stall or use stale data.

Prefer one representative workflow over repeated tests of its internal helpers.
Use temporary libraries and mock external services, not the behavior under test.
Performance tests should check bounded work, lock contention or request
coalescing rather than wall-clock thresholds.

Do not lock down CSS declarations, DOM structure, exact UI copy, icon pixels,
or incidental call counts. Check visual changes with a renderer screenshot.
Add a regression when it protects a concrete user-visible failure, not merely
because a new helper was introduced.

Electron uses Node's built-in test runner with per-file isolation and automatic
`tests/*.test.js` discovery. A smoke test also checks all top-level JS syntax.
