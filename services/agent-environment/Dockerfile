FROM --platform=$BUILDPLATFORM golang:1.24-bookworm AS slack-mcp-builder

ARG TARGETOS
ARG TARGETARCH
ARG SLACK_MCP_COMMIT=21eaf72a3be33e7f3fd44c61a07645bedc376ff4

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/korotovsky/slack-mcp-server.git /src \
    && git -C /src checkout --detach "$SLACK_MCP_COMMIT"
COPY vendor/slack-mcp-server/realname-fallback.patch /tmp/slack-realname.patch
COPY vendor/slack-mcp-server/imported-date-filter.patch /tmp/slack-date-filter.patch
RUN git -C /src apply --check /tmp/slack-realname.patch \
    && git -C /src apply /tmp/slack-realname.patch \
    && git -C /src apply --check /tmp/slack-date-filter.patch \
    && git -C /src apply /tmp/slack-date-filter.patch \
    && cd /src \
    && go test -count=1 -run '^(TestUnitEffectiveRealName|TestUnitBuildDateFiltersUnit)$' ./pkg/handler \
    && CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" \
       go build -ldflags="-s -w \
         -X github.com/korotovsky/slack-mcp-server/pkg/version.CommitHash=$SLACK_MCP_COMMIT \
         -X github.com/korotovsky/slack-mcp-server/pkg/version.Version=v1.1.23 \
         -X github.com/korotovsky/slack-mcp-server/pkg/version.BuildTime=source-patched \
         -X github.com/korotovsky/slack-mcp-server/pkg/version.BinaryName=slack-mcp-server" \
         -o /out/slack-mcp-server ./cmd/slack-mcp-server

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG TARGETARCH

RUN apt-get update && apt-get install -y npm bash git procps curl gettext-base && rm -rf /var/lib/apt/lists/*

# Need to install nodejs from elsewhere, because bookworm-slim is based on debian, and the available version of nodejs is 18, which is too old
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get update
RUN apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*

# Install published Node MCPs from the repository lock, then install UV/Git MCPs.
WORKDIR /agent-environment
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=slack-mcp-builder /out/slack-mcp-server /tmp/slack-mcp-server
RUN install -m 0755 /tmp/slack-mcp-server \
      "/agent-environment/node_modules/slack-mcp-server-linux-${TARGETARCH}/bin/slack-mcp-server-linux-${TARGETARCH}" \
    && rm /tmp/slack-mcp-server
COPY dev_scripts/install_mcp_packages.sh /
RUN /install_mcp_packages.sh && rm /install_mcp_packages.sh

# Copy project files and set up workspace
COPY . .

# Don't trust the checkout to have survived intact. COPY carries the build host's
# file modes and bytes, and a tar-extracted or Windows tree hands us entrypoint.sh
# stripped of its +x bit and/or with CRLF endings. Neither is visible in git:
# core.fileMode=false hides the mode, and .gitattributes only normalises endings
# on checkout, which a tar bypasses. Both faults build cleanly and then kill the
# container at startup — CRLF especially, since the shebang becomes "/bin/bash\r"
# and exec reports "no such file or directory" for a file that plainly exists.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

RUN uv venv --python 3.12

# Move /agent-environment/data to /data
RUN mkdir -p /data
RUN mv /agent-environment/data/* /data/ 2>/dev/null || true
RUN rm -rf /agent-environment/data

# Install git submodules from CSV to specified directories.
# The CSV arrives CRLF from a Windows-authored tree, and `read` hands the trailing
# \r to git clone, so every repo lands in "<name>\r". ls prints it as the name you
# expect — the \r is invisible — while anything that opens the real path gets
# NoSuchPath, which is how git_git_status fails on a directory you can plainly see.
RUN sed -i 's/\r$//' /data/repos/git_submodule_info.csv && \
    while IFS=',' read -r url sha path; do \
    echo "Installing $url at $sha to $path"; \
    rm -rf "$path" 2>/dev/null || true; \
    git clone "$url" "$path"; \
    cd "$path" && git checkout "$sha"; \
done < /data/repos/git_submodule_info.csv

# Set up MCP code executor workspace (depends on copied repo structure)
WORKDIR /data/repos/mcp_code_executor_workspace
RUN uv sync
WORKDIR /agent-environment

RUN rm -rf /agent-environment/dev_scripts

# Sync project dependencies (requires pyproject.toml and uv.lock from COPY)
RUN uv sync

# oxylabs goes in this venv, not the isolated one `uv tool install` builds: the
# template runs it from /agent-environment/.venv/bin/, and out of the tool venv it
# registers no tools at all — the server comes up "healthy" serving nothing, and
# calls come back "Unknown tool". Pin ai-studio for the same reason as in
# install_mcp_packages.sh, then point config.py at the gateway.
# The five html deps are pinned for the same reason as ai-studio: left to float they
# resolve a notch newer than the working image had, and oxylabs then registers no
# tools at all — /enabled-servers still reports it OK, it just serves nothing, and
# calls come back "Unknown tool" with no error anywhere to explain it.
RUN uv pip install oxylabs-mcp==0.4.1 oxylabs-ai-studio==0.2.20 \
      beautifulsoup4==4.14.3 lxml==6.1.0 lxml-html-clean==0.4.4 \
      markdownify==1.2.2 soupsieve==2.8.3
COPY vendor/yibu-patched/oxylabs_mcp/ /tmp/oxylabs_patch/
RUN cp /tmp/oxylabs_patch/*.py \
       "$(dirname "$(find /agent-environment/.venv -path '*/oxylabs_mcp/config.py')")/" \
    && rm -rf /tmp/oxylabs_patch

EXPOSE 1984

# Set the entrypoint (this always runs to generate mcp_server_config.json from template)
ENTRYPOINT ["/agent-environment/entrypoint.sh"]

# Set the default command (users can override this)
CMD ["uv", "run", "python", "-m", "uvicorn", "agent_environment.main:app", "--host", "0.0.0.0", "--port", "1984"]
