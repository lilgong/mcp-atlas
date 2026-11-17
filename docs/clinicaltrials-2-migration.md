# Native ClinicalTrials MCP 2.11.3 candidate

The upstream package is used without a formatter, schema or tool-call patch.
The registration name remains clinicaltrialsgov-mcp-server. Other MCPs keep
Node 22; ClinicalTrials alone starts using the digest-pinned Node 24.22.3
binary at /opt/clinicaltrials-node/bin/node. Its dependencies and lockfile are isolated
in clinicaltrials-runtime and installed at /opt/clinicaltrials-runtime, preserving
other MCP dependencies.

## Native tool migration

| Old tool | Replacement grants |
| --- | --- |
| clinicaltrials_get_study | clinicaltrials_get_study_record, clinicaltrials_search_studies |
| clinicaltrials_analyze_trends | clinicaltrials_search_studies, clinicaltrials_get_study_count |
| clinicaltrials_compare_studies | clinicaltrials_get_study_record, clinicaltrials_search_studies |
| clinicaltrials_find_eligible_studies | clinicaltrials_find_eligible |

Fifty tasks need these replacements. All 63 ClinicalTrials-enabled tasks also
receive clinicaltrials_get_field_definitions for native field-name discovery.
The 500 task IDs, prompts, claims and historical reference trajectories are
unchanged. Reference trajectories describe the old environment and are not
rewritten as if produced using 2.x. Slack date derivation passes through the
reviewed catalog. scripts/migrate_clinicaltrials_catalog.py reproduces this
idempotent grants-only migration for other input CSVs; it is not a runtime shim.

## Behavior differences

- get_study_record accepts a single nctId; summaryOnly is removed. Retrieve
  batches using search_studies with nctIds and explicit fields, or single lookups.
- Search text includes all studies on the returned page. Explicit fields are
  rendered by the native formatter; omitting fields uses its compact index.
- filter becomes advancedFilter; country/state/city filters use locationQuery.
  New models receive the native schema; old replay scripts need new arguments.
- Search/count exclude unknown-enrollment sentinel 99999999 by default. Pass
  includeUnknownEnrollment=true to include it. This is upstream behavior and
  cohort counts can differ from 1.x. No local override is installed.
- Trends/comparisons use combinations of search, count and detail operations.
  New and old benchmark scores are not automatically interchangeable.
- Hub continues to forward content and structuredContent; Atlas presents content
  to the model. No generic structuredContent-to-text conversion is introduced.

## Candidate rollout

Build with a separate tag:

```bash
make build-atlas-runtime ATLAS_RUNTIME_IMAGE=mcp-atlas-runtime:clinicaltrials-2.11.3-candidate
```

Do not retag latest, replace deployed MCPBs, reload registrations or restart
active evaluations during validation. The candidate MCPB carries its own Node
24 binary and does not require upgrading Hub worker images.

Validate single details, multi-ID field projection, complete pagination, counts,
and representative original tasks. Then deploy the same version for native
evaluation, Hub evaluation and data generation, and derive MCP-Atlas.csv from
the migrated baseline. Existing result CSVs remain unchanged.
