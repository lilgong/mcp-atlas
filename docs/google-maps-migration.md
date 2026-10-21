# Google Maps API compatibility

The runtime still pins `@modelcontextprotocol/server-google-maps@0.6.2`.
`run_node_mcp.cjs` loads its original tool declarations and handlers with a
server-local fetch adapter (`google_maps_compat.cjs`). It does not modify installed
packages or global fetch. All seven tool names, descriptions and input schemas
remain identical to the pinned upstream MCP; the MCP result envelopes and existing
JSON field names are retained.

Enable these services in the Google Cloud project and allow them in the API key's
API restrictions (`GOOGLE_MAPS_API_KEY` remains the only credential variable):

| Service | Tools |
| --- | --- |
| Geocoding API | `maps_geocode`, `maps_reverse_geocode` |
| Maps Elevation API | `maps_elevation` |
| Places API (New) | `maps_search_places`, `maps_place_details` |
| Routes API | `maps_directions`, `maps_distance_matrix` |

Enabling a service in the project and allowing it on a restricted key are separate
steps. `API_KEY_SERVICE_BLOCKED` means the key's service restrictions rejected the
request. Old Directions, Distance Matrix and Places API permissions are not needed
by this adapter. Other applications still using their legacy endpoints need their
existing permissions until migrated.

## Behavior and limits

- Places searches/details and Routes use explicit field masks for the data already
  exposed by the tools. No wildcard mask or additional tool is introduced.
- Reviews/opening hours, route distances/durations and navigation steps are mapped
  into the original MCP fields. New APIs can return different search/routing data
  or omit fields; compatibility does not imply identical benchmark answers.
- Route matrix elements are reordered by origin/destination index; omitted proto
  zero indices and zero distances/durations are supported. Per-element errors stay
  errors. No-route elements use `ZERO_RESULTS`.
- The new route matrix does not return normalized addresses. `origin_addresses`
  and `destination_addresses` retain the submitted input labels, without extra
  billable geocoding calls. Routes instructions may be plain text rather than the
  legacy API's HTML markup, inside the same `instructions` field.
- The original API-key variable is reused. Geocoding and elevation pass through.
  Upstream HTTP errors retain their status in the tool's error text; there are no
  added automatic retries or fabricated route/place results.
- Limits and billing belong to the new APIs/SKUs; existing legacy quotas are not
  assumed to transfer. Google applies its current matrix size/mode limits.

Build a new Atlas runtime image to update direct mode. Hub mode requires importing
an updated standalone `google-maps.mcpb` and reloading its connection; updating the
Atlas image alone does not change an MCP hosted in Hub. The bundle must contain the
same adapter and dispatch from its launcher, with a blank credential default.

## Verification

```sh
node --test services/agent-environment/tests/test_google_maps_compat.cjs
```

Tests cover request/response conversion, matrix ordering and errors, all four
travel modes, and an actual stdio `tools/list` comparison against the pinned package.
No real API calls or credentials are required by these tests. A real API smoke test
should additionally exercise search/details, directions/matrix for driving,
walking, bicycling and transit, plus geocoding/reverse geocoding/elevation.

References:
- https://developers.google.com/maps/legacy
- https://developers.google.com/maps/documentation/places/web-service/legacy/migrate-overview
- https://developers.google.com/maps/documentation/routes/migrate-routes
- https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRouteMatrix
