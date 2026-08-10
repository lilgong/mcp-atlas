#!/bin/bash
set -e

echo "Installing NPX MCP server packages globally..."

# Pre-install all NPX MCP server packages globally to eliminate download time during runtime
npm install -g \
    @felores/airtable-mcp-server@0.3.0 \
    @alchemy/mcp-server@0.1.8 \
    @modelcontextprotocol/server-brave-search@0.6.2 \
    clinicaltrialsgov-mcp-server@1.9.3 \
    @upstash/context7-mcp@1.0.33 \
    @wonderwhy-er/desktop-commander@0.2.7 \
    @e2b/mcp-server@0.2.0 \
    exa-mcp-server@0.3.10 \
    @modelcontextprotocol/server-filesystem@2026.7.10 \
    @modelcontextprotocol/server-google-maps@0.6.2 \
    @geobio/google-workspace-server@0.1.0 \
    @translated/lara-mcp@0.0.11 \
    @geobio/code_execution_server@0.2.1 \
    mcp-server-code-runner@0.1.7 \
    @modelcontextprotocol/server-memory@2025.8.4 \
    metmuseum-mcp@1.0.0 \
    mongodb-mcp-server@0.2.0 \
    mcp-server-nationalparks@1.0.1 \
    @notionhq/notion-mcp-server@1.8.1 \
    @geobio/mcp-open-library@0.1.6 \
    slack-mcp-server@1.1.23 \
    @bharathvaj/whois-mcp@1.0.1

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
)
mkdir -p /usr/lib/node_modules/@smithery
mv "${GITHUB_MCP_BUILD_DIR}" /usr/lib/node_modules/@smithery/mcp-github
test -f /usr/lib/node_modules/@smithery/mcp-github/dist/cli.js

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
uv tool install mcp-server-git==2025.7.1 --with mcp==1.25.0
uv tool install osm-mcp-server==0.1.1 --with mcp==1.28.1
uv tool install mcp-server-twelve-data==0.2.5 --with mcp==1.28.1
uv tool install wikipedia-mcp==2.0.1 --with mcp==1.28.1

echo "All UVX/NPX MCP packages installation complete."
