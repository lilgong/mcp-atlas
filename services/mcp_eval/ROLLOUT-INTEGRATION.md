# Rollout integration

`rollout` is based on `mcphub-v2`. `redis` is a separate platform branch from
that same base. `main` retains direct MCP execution; `mcphub` is the archived
v1 integration. Public fixes belong in the shared baseline before being applied
to either platform branch.

## Runtime behavior

Rollout retains synthesis sessions, exploration and tool policies, runtime
identity, completion-owned probes, source-keyed Git recovery, usage telemetry,
structured failures, model-key failover and evaluator compatibility.
MCPHub v2 supplies namespace routing, scoped task callbacks, shared scheduling,
credential ownership and audit logs. Atlas owns task containers and fixtures.
Task leases renew independently; temporary control-plane failures retry only
within the confirmed lease or setup budget. Tool calls are never replayed by
lease recovery. A model service explicitly reporting STOPPED terminates the run
without disabling or rotating model keys.

Hub calls bypass Atlas upstream key pools and local tool-result caches. Direct
mode retains its existing key pools and rate policies. Model-visible tool names,
schemas, benchmark records and fixture isolation remain unchanged.

## Build and configuration

```bash
make build-atlas-runtime ATLAS_RUNTIME_IMAGE=mcp-atlas-runtime:rollout
```

Select the new image in the calling platform and recreate its runtime before
probing or evaluating. Building or changing branches does not update a running
container. Configure the reachable `MCPHUB_URL`, `MCPHUB_CATALOG`, `MCPHUB_TOKEN`
and optional shared-account `MCPHUB_TARGET` as described in
[Hub task routing](HUB-TASK-ROUTING.md). Task callbacks do not use the shared-account
selector. Never copy upstream keys into platform configuration for Hub-managed MCPs.
