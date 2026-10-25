# OSM routing profiles

The pinned `osm-mcp-server==0.1.1` tool contract is retained. Its route client
is adapted to select `routing.openstreetmap.de/routed-car`, `routed-bike`, or
`routed-foot` according to the requested mode. The generic OSRM demo endpoint
is not a walking/cycling fallback: changing its URL profile can still return
driving routes. A failed profile-specific request is returned as a tool error.
Successful HTTP responses are also checked for OSRM success and compatible
step modes; pushing a bicycle, walking sections, and ferries are allowed.

No durations are recomputed, scaled, or hard-coded. Tool names, descriptions,
input schemas and result fields remain unchanged. Overpass selection, Relay
credentials and other OSM tools are unchanged. The public routing service's
usage policy limits requests to one per second; shared deployments should
respect that policy through their common egress scheduling.

**Benchmark compatibility:** walking/cycling results can differ from Atlas's
original reference trajectories/GT, some of which used driving routes labelled
as walking. This patch deliberately does not modify benchmark data or scoring.
Historical scores are not guaranteed to remain comparable on affected tasks.

Direct mode requires rebuilding the runtime image and using it for new tool
processes. Hub mode requires the updated `osm-mcp-server.mcpb` and reloading the
OSM connection; no Hub or Relay restart is needed. Other benchmark deployments
must update their own imported copies.

Verification: `python -m pytest services/agent-environment/tests/test_osm_route_profiles.py`.
See https://routing.openstreetmap.de/about.html and
https://github.com/Project-OSRM/osrm-backend/blob/master/docs/http.md.
