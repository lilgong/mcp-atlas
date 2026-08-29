# Slack MCP 1.1.23 compatibility patch

MCP-Atlas and the released official runtime pin `slack-mcp-server` to 1.1.23.
Slack's import flow represents deactivated imported users with an empty
top-level `real_name`, while preserving the imported identity in
`profile.real_name`. Version 1.1.23 reads only the top-level field, so public
fixture imports lose names such as `Steve Shins` even though Slack still
returns the profile value.

The runtime keeps the official version and public tool schemas intact, with two
targeted compatibility patches.

`realname-fallback.patch` resolves an empty real name in this order:

1. top-level real name (unchanged official behavior);
2. profile real name;
3. profile display name;
4. username.

`imported-date-filter.patch` preserves the public `filter_date_on` and
`filter_date_during` semantics while emitting equivalent exclusive
`after:`/`before:` ranges internally. Slack's `on:` search modifier can return
no rows for imported messages when the token owner uses fixed UTC+0, even though the
message epoch and range filters are correct. Full dates become a one-day range;
month/year values become a full-month range.

The runtime Docker build checks out the immutable upstream 1.1.23 commit,
applies both patches, runs their focused upstream-style unit tests, and replaces
only the npm package's platform binary.
