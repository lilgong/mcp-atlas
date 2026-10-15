# Reviewed benchmark corrections

The tracked baseline retains the upstream tasks, prompts and trajectories. Reviewed
claim corrections are committed separately from runtime behavior changes. The integrity
check protects the resulting baseline hash; it does not silently rewrite data.

## Marketing budget difference — 2026-10-15

- Task: `688a441c1a0d2e5c93853e93`.
- Field: `GTFA_CLAIMS`, third claim only.
- Incorrect difference: `$27,279`; corrected difference: `$26,279`.
- Evidence: the original trajectory returns `39261` for January 2020 and `65540`
  for January 2021. `65540 - 39261 = 26279`, and `26279 / 39261 × 100`
  rounds to `66.93%`, matching the unchanged percentage claim.
- No prompt, trajectory, tool configuration, task count or other claim is changed.
- The derived `MCP-Atlas.csv` remains ignored by Git. The conversion regression test
  verifies that repeated derivation retains the correction without altering this
  task's prompt or trajectory.
- Original upstream SHA-256:
  `065f423ffd1425185d23ed01a1d1ad8ed8c6355749868521a07faaa13ec4c0ad`.
- Reviewed baseline SHA-256:
  `4850307c5597d22f2e3cc8e83b047500e519a1554a04f6ecaa96629daa8f9d22`.
