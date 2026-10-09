# Optional MCPHub task routing

Direct mode is preserved: `MCP_TOOL_TRANSPORT=direct` performs no Hub calls.
Shared-only Hub mode is also preserved unless `MCPHUB_TASK_ROUTING=true` is set.
The ten isolated MCPs (filesystem, git, memory, mongodb, cli-mcp-server,
desktop-commander, mcp-code-executor, mcp-server-code-runner, arxiv, pubmed)
continue to execute in task containers owned by Atlas.

For full Hub routing, configure task targets in Hub, and set these values in the
**completion service's** environment before starting a new evaluation process:

```dotenv
HOST=0.0.0.0
MCP_TOOL_TRANSPORT=hub
MCPHUB_URL=http://host.docker.internal:3002
MCPHUB_CONFIG_URL=http://127.0.0.1:3002
MCPHUB_TOKEN=<existing system all-access operator Bearer Key>
MCPHUB_TASK_ROUTING=true
MCPHUB_TARGET=atlas/official
MCPHUB_TASK_CALLBACK_URL=http://host.docker.internal:3500
MCPHUB_MANAGE_RATE_LIMITS=true
MCPHUB_MANAGE_RELAY=true
```

`3500` is an example: use the port of your **existing completion service**.
The callback URL must reach that exact process from the Hub container.
The completion service must bind a reachable interface (for host-gateway callbacks,
use `HOST=0.0.0.0` on the existing port). It must not
point at the shared agent environment on 2984. For multiple completion workers,
each process needs its own reachable callback URL. No extra port or Docker socket
is added to Hub. The current bridge registry is process-local: do not load-balance
its callbacks across unrelated completion workers.

`MCPHUB_CONFIG_URL` is the Hub root reachable from the completion host. If omitted,
the adapter uses `MCPHUB_URL`, which must then be reachable from that process too.
The shared container uses `MCPHUB_URL`; these addresses may differ.

`MCPHUB_SERVERS` remains the list of selected **shared** MCPs in the shared agent
environment. Do not put task-isolated MCPs in that list. Set `MCPHUB_TARGET` there
as well for shared MCPs configured with targets.
Call statistics aggregate all completed calls per MCP over the selected month or
UTC date range. No model, benchmark or evaluation-run labels are required.

To use a different workspace/fixture, select its exact configured path, for example
`MCPHUB_TARGET=atlas/syn-1`. Never mix workspace credentials in a rotating pool.
Missing targets and expired leases fail rather than falling back to another target
or the direct backend. Five-minute leases renew every two minutes and are revoked
before containers are destroyed. Existing canonical tool names are preserved.

For task MCPs without any rate policy, Hub forwarding adds no arbitrary concurrency
cap. For arxiv/pubmed relay, Atlas fetches the generic Hub runtime snapshot before
creating the task containers. Relay configuration changes apply to newly started
task containers; already-running task containers keep their startup snapshot. Arxiv and PubMed keep
separate relay environments even when they share one task-network container.

Other benchmarks can implement the same tool-http-v1 registration and callback
contract in MCPHub's target-connections documentation. Hub does not depend on Atlas.
