# Slack MCP 1.1.23 compatibility patch

MCP-Atlas and the released official runtime pin `slack-mcp-server` to 1.1.23.
Slack's import flow represents deactivated imported users with an empty
top-level `real_name`, while preserving the imported identity in
`profile.real_name`. Version 1.1.23 reads only the top-level field, so public
fixture imports lose names such as `Steve Shins` even though Slack still
returns the profile value.

`realname-fallback.patch` keeps the official version and tool schemas intact.
It only resolves an empty real name in this order:

1. top-level real name (unchanged official behavior);
2. profile real name;
3. profile display name;
4. username.

The runtime Docker build checks out the immutable upstream 1.1.23 commit,
applies this patch, runs the focused upstream-style unit test added by the
patch, and replaces only the npm package's platform binary.
