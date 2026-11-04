# Optional MCPHub task routing

Direct mode is preserved: `MCP_TOOL_TRANSPORT=direct` performs no Hub calls.
Shared-only Hub mode is also preserved unless `MCPHUB_TASK_ROUTING=true` is set.
The ten isolated MCPs (filesystem, git, memory, mongodb, cli-mcp-server,
desktop-commander, mcp-code-executor, mcp-server-code-runner, arxiv, pubmed)
continue to execute in task containers owned by Atlas.

For selected task Hub routing, configure task targets in Hub, and set these values in the
**completion service's** environment before starting a new evaluation process:

```dotenv
HOST=0.0.0.0
MCP_TOOL_TRANSPORT=hub
MCPHUB_URL=http://192.168.0.22:3003
MCPHUB_CATALOG=atlas
MCPHUB_TOKEN=<Atlas group calling Key>
MCPHUB_TASK_ROUTING=true
MCPHUB_TASK_SERVERS=arxiv,pubmed
MCPHUB_TARGET=official
MCPHUB_TASK_CALLBACK_URL=http://host.docker.internal:3500
MCPHUB_MANAGE_RATE_LIMITS=true
MCPHUB_MANAGE_RELAY=true
```

`3500` is an example: use the port of your **existing completion service**.
The callback URL must reach that exact process from the Hub container.
The completion service must bind a reachable interface (for host-gateway callbacks,
use `HOST=0.0.0.0` on the existing port). It must not
point at the shared agent environment on 2984. For multiple completion workers,
each process needs its own reachable callback URL. Remote access on this completion port is restricted to authenticated task callbacks;
evaluation and health APIs remain loopback-only. No extra port or Docker socket
is added to Hub. The current bridge registry is process-local: do not load-balance
its callbacks across unrelated completion workers.

The v2 example uses the LAN listener on port 3003. On another deployment,
set the URL to that Hub host and configure a reachable bind address. `MCPHUB_CATALOG=atlas` selects `/mcp/atlas/{server}` for shared
MCPs, `catalog=atlas` for runtime setup and task registration. Logical tool names
remain unchanged. There is no automatic fallback to public registrations.
An empty catalog explicitly selects v2 public resources. The calling key must
be created in the selected Hub deployment. This branch targets v2.

`MCPHUB_URL` is the single Hub root used by both the completion service and the
shared runtime. Use an address reachable from both, such as the Hub host's LAN IP.
The shared runtime uses host networking, so a same-host loopback address also works.

`MCPHUB_SERVERS` remains the list of selected **shared** MCPs in the shared agent
environment. Do not put task-isolated MCPs in that list. Set `MCPHUB_TARGET` there
as well for shared MCPs configured with targets.
Call statistics aggregate all completed calls per MCP over the selected month or
UTC date range. No model, benchmark or evaluation-run labels are required.

To use a different workspace/fixture, select its exact configured path, for example
`MCPHUB_TARGET=syn-1`. Never mix workspace credentials in a rotating pool.
This selects shared account targets only. Task startup and lease registration do
not send an account target selector. Hub resolves the service's dedicated default
task entry. Atlas configures no task-target variable. An absent or ambiguous default
fails closed. Every run generates a unique task ID, lease and callback credential,
even for concurrent runs of the same benchmark task. Atlas also gives each run its
own task container and filesystem. Shared remote account data is not copied or
isolated by this mechanism.
Missing targets and expired leases fail rather than falling back to another target
or the direct backend. Five-minute leases renew every two minutes and are revoked
before containers are destroyed. Existing canonical tool names are preserved.

Each task connection renews independently. Network failures and HTTP 408/429/500/502/503/504
retry with bounded backoff within its last confirmed lease. A rejected or expired
connection revokes only its own callback; no tool call is replayed or redirected
to a direct backend. Cancelling a task also cancels its renewal workers.
Runtime configuration GETs use at most three attempts within 30 seconds per server
and configuration stage. Authentication and invalid configuration fail immediately.
These waits consume the existing evaluation time budget; they do not extend it.
Runtime logs include setup attempts and lease results with status, duration and
error type, without recording credentials or response bodies.

For task MCPs without any rate policy, Hub forwarding adds no arbitrary concurrency
cap. For arxiv/pubmed relay, Atlas fetches the generic Hub runtime snapshot before
creating the task containers. Relay configuration changes apply to newly started
task containers; already-running task containers keep their startup snapshot. Arxiv and PubMed keep
separate relay environments even when they share one task-network container.

Other benchmarks can implement the same tool-http-v1 registration and callback
contract in MCPHub's target-connections documentation. Hub does not depend on Atlas.

Task targets may explicitly publish `startupEnvKeys` from their parent MCP environment.
For PubMed this is `NCBI_API_KEY`, `NCBI_EMAIL`, `NCBI_TOOL`; workers retrieve the
scoped `startupEnv` at startup, without writing provider credentials to Atlas `.env`.
The task target's optional `callbackOrigins` list can restrict the completion origin,
for example `http://host.docker.internal:3500`; an empty list adds no origin
restriction, while callback credentials are still required. Shared and task calls both use
Hub audit and scheduling; file ownership and fixture copies stay with Atlas.
