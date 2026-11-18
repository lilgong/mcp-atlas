# Synthesis rollout on MCPHub (2026-10-15)

Integration branch: `codex/rollout-on-mcphub-20261015`.
First parent/base: upstream `origin/mcphub` at `ff4b6da`.
Merged synthesis lineage: `codex/rollout-reasoning-mcphub-v1` at `8095881`.
Both histories are preserved; the old branch remains the code rollback point.
Do not replace this integration with the independent upstream `origin/rollout`.

## Responsibilities

MCPHub supplies shared/task transport, relay scheduling and current reasoning
compatibility. Latest benchmark corrections, Slack import transformation, Met
pagination shim and usage reporting are retained. Synthesis adds opt-in
exploration controls, safety/tool policy, per-task sandbox/fixture isolation,
runtime identity, completion-owned probes, source-keyed Git recovery, score_one,
telemetry and structured failures. Keep 180-second probe defaults, probe cache
bypass, Hub credential ownership, no Atlas upstream key pooling for Hub calls,
and coverage-judge-v3-consistency. Equivalent reasoning fixes are one merged
implementation, not two chained adapters. Direct-mode key pools remain solely
for explicit fallback compatibility; removing that mode is separate work.

The Hub timeout conflict was formatting/default-number representation only;
upstream 180.0 is retained. Reasoning/request-shape tests isolate keys and pacing,
so simulated responses never depend on production key state. No real model/MCP
calls are used in tests.

## Validation and deployment boundary

- completion/scoring: 426 passed, 24 subtests passed.
- agent environment: 94 passed, 1 skipped (existing environment-dependent test).
- Met shim: 6 passed using the pinned local package from the main checkout,
  mocked fetchJson responses, no Met API calls.
- pipeline integration: 1344 passed, 19 subtests passed.

Upstream tracked benchmark corrections are present, not applied to generated
records. No Slack import executed, no fixture reseeded, no credentials, logs or
results removed, no production environment variables changed. Campaign inputs
remain frozen. This is offline code integration, NOT a live acceptance run.

To deploy the complete runtime, build a fresh image tag, select it in pipeline
`.env` as MCP_AGENT_IMAGE, then runtime down/up/probe. Do not reuse old catalog
snapshots or use restart-completion: image/gateway paths changed too.

```bash
python3 /home/lny/mcp-atlas-rollout/scripts/build_atlas_runtime.py \
  --image mcp-atlas-runtime:rollout-mcphub-20261015
```

Met is currently Hub-routed: its serving Hub runtime must also carry the adapter
for the actual service to benefit; local tests do not prove remote deployment.

Publishing is separate from local integration. After review, normal non-force
push can update rollout (not mcphub):

```bash
git push origin HEAD:rollout
```

The initial integration merge contains mcphub ancestry and equivalent rollout
patches, but cherry-picked equivalents alone do NOT include origin/rollout's
commit ancestry. A subsequent explicit merge of origin/rollout 4e6fc3d resolves
this, preserving the same source tree and allowing non-force publication.
Fetch/review any later upstream movement before publishing.

## Synthesis integration on MCPHub v2

Merged origin/rollout b02fc29 into the synthesis runtime. Configure
MCPHUB_CATALOG separately from optional shared-account MCPHUB_TARGET.
Task leases and runtime setup use catalog only, never the shared account target.
Keep the local joint-author session lifecycle, evidence labels, readable Notion
projection, evaluator task context and measured/unknown cache accounting.
The remote stopped-service handling is retained without disabling model keys.
Rebuild the image and recreate the runtime before using the new Hub transport.

### 2026-11-18: synchronize origin/rollout b13aa74

Merged upstream rollout into the synthesis adaptation branch. Preserve local
bounded author sessions and their close endpoint, usage/cache accounting,
explicit evaluator context policies, and removal of model RPM scheduling.
Upstream adds runtime compatibility for E2B cleanup/quota handling, Gmail quota
handling, Open Library relay routing, Twelve Data market-state results, and
Slack month-boundary date preparation. ClinicalTrials 2.11.3, PubMed budgets and
Overpass stream budgets already had equivalent local patches.

Offline validation: completion tests initially 667 passed / 4 failed (new empty
reasoning test omitted the local key-pool mock); after adding that mock all 18
empty-reasoning tests passed. 45 subtests also passed in the full run. Runtime
compatibility tests: 22 passed. E2B/Gmail Node tests: 11 passed. Google Workspace
patch and Open Library Node tests could not run without their pinned npm
packages; Twelve Data market-state tests require the deployment-only
mcp_server_twelve_data package, absent from the local test venv. No live calls,
image build, runtime reload, or production/configuration changes were performed.
A rebuild/restart is required before new image-side compatibility takes effect.
