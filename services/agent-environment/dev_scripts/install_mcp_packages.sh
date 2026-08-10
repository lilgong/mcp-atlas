#!/bin/bash
set -e

# Published Node servers are installed by `npm ci` from the repository-owned
# package.json/package-lock.json. Keep this script for non-npm and Git sources.

# This server is not published at the schema used by MCP-Atlas.  Pin the
# audited git revision and build it once into the runtime image; invoking the
# git URL through npx at every container start races the 25-second MCP client
# initialization timeout on busy hosts.
GITHUB_MCP_REVISION="68368436034fb0003a6d8ed91afc9d0a64142b84"
GITHUB_MCP_BUILD_DIR="$(mktemp -d)"
git init "${GITHUB_MCP_BUILD_DIR}"
git -C "${GITHUB_MCP_BUILD_DIR}" remote add origin \
    https://github.com/geobio/smitheryai-mcp-servers-github.git
git -C "${GITHUB_MCP_BUILD_DIR}" fetch --depth 1 origin "${GITHUB_MCP_REVISION}"
git -C "${GITHUB_MCP_BUILD_DIR}" checkout --detach FETCH_HEAD
(
    cd "${GITHUB_MCP_BUILD_DIR}"
    # The pinned upstream revision changed @types/node in package.json
    # without refreshing package-lock.json, so npm ci rejects the checkout.
    # npm install reconciles that one upstream inconsistency before building.
    npm install --ignore-scripts
    npm run build
    npm prune --omit=dev --ignore-scripts
    rm -rf .git src
    printf '%s\n' "${GITHUB_MCP_REVISION}" > .atlas-revision
)
mkdir -p /agent-environment/node_modules/@smithery
mv "${GITHUB_MCP_BUILD_DIR}" /agent-environment/node_modules/@smithery/mcp-github
test -f /agent-environment/node_modules/@smithery/mcp-github/dist/cli.js
test "$(cat /agent-environment/node_modules/@smithery/mcp-github/.atlas-revision)" = \
    "${GITHUB_MCP_REVISION}"

echo "Installing UVX MCP server packages..."
# Pre-install all UVX MCP server packages to eliminate download time during runtime
uv tool install arxiv-mcp-server==0.2.11 --with mcp==1.28.1
uv tool install mcp-server-calculator==0.2.0 --with mcp==1.28.1
uv tool install cli-mcp-server==0.2.5 --with mcp==1.28.1
uv tool install 'duckduckgo-mcp-server[browser]==0.6.1' --with mcp==1.28.1
uv tool install mcp-server-fetch==2025.4.7 --with mcp==1.28.1
# --with pins a transitive dep that would otherwise float: mcp-server-git is pinned
# but its MCP SDK isn't, and 1.28 asks the client for "roots" on startup, which the
# agent-environment client doesn't implement — the server then dies with -32603 and
# every git task fails. 1.25 is the version the working image shipped.
uv tool install mcp-server-git==2026.7.10 --with mcp==1.25.0
uv tool install osm-mcp-server==0.1.1 --with mcp==1.28.1
uv tool install mcp-server-twelve-data==0.2.5 --with mcp==1.28.1
uv tool install wikipedia-mcp==2.0.1 --with mcp==1.28.1

echo "Git-backed and UVX MCP package installation complete."
