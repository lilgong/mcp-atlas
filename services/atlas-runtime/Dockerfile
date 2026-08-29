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

LABEL mcp-atlas.runtime="true" \
      mcp-atlas.runtime-version="20260810" \
      mcp-atlas.data-contract="external-data-v1" \
      mcp-atlas.contains-fixture="false"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash curl gettext-base git nodejs npm procps \
    && rm -rf /var/lib/apt/lists/*

# ClinicalTrials.gov 1.9.3 requires Node 22. The Debian package is only used
# to bootstrap npm before NodeSource replaces it.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /agent-environment
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=slack-mcp-builder /out/slack-mcp-server /tmp/slack-mcp-server
RUN install -m 0755 /tmp/slack-mcp-server \
      "/agent-environment/node_modules/slack-mcp-server-linux-${TARGETARCH}/bin/slack-mcp-server-linux-${TARGETARCH}" \
    && rm /tmp/slack-mcp-server
COPY install_mcp_packages.sh /install_mcp_packages.sh
RUN /install_mcp_packages.sh && rm /install_mcp_packages.sh
COPY pyproject.toml uv.lock README.md entrypoint.sh ./
COPY src/agent_environment/__init__.py src/agent_environment/logger.py \
     src/agent_environment/main.py src/agent_environment/mcp_client.py \
     src/agent_environment/mcp_router.py \
     src/agent_environment/osm_mcp_compat.py \
     src/agent_environment/oxylabs_mcp_compat.py \
     src/agent_environment/pubmed_mcp_compat.py \
     src/agent_environment/twelvedata_mcp_compat.py \
     src/agent_environment/run_node_mcp.cjs \
     src/agent_environment/yibu_fetch_preload.cjs \
     src/agent_environment/
COPY src/agent_environment/mcp_server_template.json \
     src/agent_environment/mcp_server_template.json
COPY src/agent_environment/wikipedia_preload/sitecustomize.py \
     src/agent_environment/wikipedia_preload/sitecustomize.py
COPY src/agent_environment/weatherapi_preload/sitecustomize.py \
     src/agent_environment/weatherapi_preload/sitecustomize.py
RUN sed -i 's/\r$//' entrypoint.sh \
    && chmod 0755 entrypoint.sh \
    && uv sync --frozen

# Immutable execution dependencies live outside /data. Task code and every
# fixture remain external and are injected into /data at container creation.
COPY code-executor-requirements.txt /tmp/code-executor-requirements.txt
RUN uv venv --python 3.12 /opt/mcp-code-venv \
    && uv pip install \
        --python /opt/mcp-code-venv/bin/python \
        --requirement /tmp/code-executor-requirements.txt \
    && rm /tmp/code-executor-requirements.txt

RUN uv pip install \
      oxylabs-mcp==0.4.1 oxylabs-ai-studio==0.2.20 \
      beautifulsoup4==4.14.3 lxml==6.1.0 lxml-html-clean==0.4.4 \
      markdownify==1.2.2 soupsieve==2.8.3
COPY vendor/yibu-patched/oxylabs_mcp/ /tmp/oxylabs_patch/
RUN cp /tmp/oxylabs_patch/*.py \
       "$(dirname "$(find /agent-environment/.venv -path '*/oxylabs_mcp/config.py')")/" \
    && rm -rf /tmp/oxylabs_patch

# The image deliberately carries no fixture. Docker supplies an empty volume
# for shared use or an explicit per-task bind mount for synthesis/evaluation.
RUN mkdir -p /data \
    && test -z "$(find /data -mindepth 1 -print -quit)"
VOLUME ["/data"]

EXPOSE 1984
ENTRYPOINT ["/agent-environment/entrypoint.sh"]
CMD ["/agent-environment/.venv/bin/python", "-m", "uvicorn", "agent_environment.main:app", "--host", "0.0.0.0", "--port", "1984"]
