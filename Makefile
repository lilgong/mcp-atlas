# Makefile for Agent Environment

IMAGE_NAME = agent-environment
VERSION = 1.2.5
GHCR_REPO = ghcr.io/scaleapi/mcp-atlas
MCP_HOST_PORT ?= 1984
MCP_STATUS_HOST_PORT ?= 1985
# Internal/host port used by run-docker-host (host networking). Override: make run-docker-host MCP_PORT=2984
MCP_PORT ?= 2984

.PHONY: build run run-docker run-docker-host shell test run-mcp-completion push

run-docker: # run docker container for mcp servers (agent-environment service)
	docker run --rm -p $(MCP_HOST_PORT):1984 -p $(MCP_STATUS_HOST_PORT):1985 --add-host=host.docker.internal:host-gateway --env-file .env $(IMAGE_NAME):latest

run-docker-host: # run mcp servers with --network host on $(MCP_PORT) (so it reaches host-local services like mongodb on 127.0.0.1, and avoids colliding with another instance on 1984)
	docker run --rm --network host --add-host=host.docker.internal:host-gateway --env-file .env \
		$(IMAGE_NAME):latest \
		uv run python -m uvicorn agent_environment.main:app --host 0.0.0.0 --port $(MCP_PORT)

build: # builds agent-environment
	cd services/agent-environment && docker buildx build --platform linux/amd64 -t $(IMAGE_NAME) .
	docker tag $(IMAGE_NAME):latest $(IMAGE_NAME):$(VERSION)

shell: # shell for agent-environment
	docker run -it --rm --env-file .env $(IMAGE_NAME):latest bash


# Makefile for MCP Eval

# Run the MCP completion server (port 3000, http post endpoint at /v2/mcp_eval/run_agent)
# Note: This runs agent completions (not evaluation/scoring). For scoring, see mcp_evals_scores.py
run-mcp-completion: 
	cd services/mcp_eval && uv run python -m mcp_completion.main

# Build and push multi-arch image to ghcr.io
# Requires Docker, and may not work with Rancher Desktop
# First do: docker login ghcr.io
push:
	@echo "--- Building and pushing multi-arch $(GHCR_REPO):$(VERSION) and :latest ---"
	cd services/agent-environment && docker buildx build --platform linux/amd64,linux/arm64 \
		-t $(GHCR_REPO):$(VERSION) \
		-t $(GHCR_REPO):latest \
		--push .
	@echo "✓ Successfully pushed to $(GHCR_REPO):$(VERSION)"
