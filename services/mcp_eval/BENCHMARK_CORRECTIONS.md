# Reviewed benchmark corrections

The tracked baseline retains the upstream task IDs, prompts and trajectories. Reviewed
claim and tool-catalog corrections are committed separately from runtime behavior changes. The integrity
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

## Static tool-catalog corrections — 2026-10-15

These previously ran on every Slack CSV derivation. They now live directly in the
tracked baseline so direct baseline runs and Slack-derived runs use the same tools.

- Only `ENABLED_TOOLS` changes, across 131 tasks; all 500 tasks remain.
- 57 occurrences of
  `clinicaltrialsgov-mcp-server_clinicaltrials_list_studies` become the verified
  successor `clinicaltrialsgov-mcp-server_clinicaltrials_search_studies`.
- Remove 217 references to servers already retired by the runtime:
  `rijksmuseum-server` (93), `balldontlie` (36), `f1-mcp-server` (48), `anili` (40).
- Tool objects retain parameter metadata. Prompts, claims, trajectories, task IDs
  and other fields remain byte-for-byte equivalent when parsed as CSV.
- `prepare_slack_import.py` now only derives Slack-bound dates and dependent year
  arithmetic; it passes the baseline tool catalog through unchanged.
- With the same date shift, its derived CSV is byte-for-byte identical to the old
  converter's output before this migration.
- Previous reviewed SHA-256:
  `4850307c5597d22f2e3cc8e83b047500e519a1554a04f6ecaa96629daa8f9d22`.
- Current reviewed SHA-256:
  `267250cf0d10b2b1222447db6d4a466e1623003f996a37552fc0206039d2b12d`.
