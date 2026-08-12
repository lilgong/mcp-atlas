# Makefile for Agent Environment

IMAGE_NAME = agent-environment
VERSION = 1.2.5
GHCR_REPO = ghcr.io/scaleapi/mcp-atlas
MCP_HOST_PORT ?= 1984
MCP_STATUS_HOST_PORT ?= 1985
# Host-networked shared MCP port is read from MCP_SHARED_PORT in .env.
TASK_MONGO_IMAGE ?=
ATLAS_RUNTIME_IMAGE ?= mcp-atlas-runtime:latest
MONGO_FIXTURE_DUMP ?=
MONGO_FIXTURE_DB ?=
MONGO_FIXTURE_ID ?=

.PHONY: build build-atlas-runtime build-task-mongo run run-docker run-docker-host shell test test-completion run-mcp-completion check-task-isolation cleanup-task-sandboxes push

run-docker: # run docker container for mcp servers (agent-environment service)
	docker run --rm -p 127.0.0.1:$(MCP_HOST_PORT):1984 -p 127.0.0.1:$(MCP_STATUS_HOST_PORT):1985 --add-host=host.docker.internal:host-gateway --env-file .env $(IMAGE_NAME):latest

run-docker-host: # run shared MCP using MCP_SHARED_HOST/MCP_SHARED_PORT from .env
	uv run --project services/mcp_eval python scripts/run_shared_mcp.py

build: # builds agent-environment; only tags :latest (VERSION is for the GHCR push target)
	cd services/agent-environment && docker buildx build --platform linux/amd64 -t $(IMAGE_NAME) .

build-atlas-runtime: # builds the versioned fixture-free runtime; never touches agent-environment:latest
	python3 scripts/build_atlas_runtime.py --image "$(ATLAS_RUNTIME_IMAGE)"

build-task-mongo: # build disposable synthetic Mongo fixture; does not modify agent-environment:latest
	@test -n "$(MONGO_FIXTURE_DUMP)" || (echo "MONGO_FIXTURE_DUMP is required" && exit 2)
	@test -n "$(MONGO_FIXTURE_DB)" || (echo "MONGO_FIXTURE_DB is required" && exit 2)
	@test -n "$(MONGO_FIXTURE_ID)" || (echo "MONGO_FIXTURE_ID is required" && exit 2)
	python3 scripts/build_task_mongo_fixture.py \
		--dump-dir "$(MONGO_FIXTURE_DUMP)" \
		--source-database "$(MONGO_FIXTURE_DB)" \
		--fixture-id "$(MONGO_FIXTURE_ID)" \
		$(if $(TASK_MONGO_IMAGE),--image "$(TASK_MONGO_IMAGE)",)

shell: # shell for agent-environment
	docker run -it --rm --env-file .env $(IMAGE_NAME):latest bash


# Makefile for MCP Eval

# Run the MCP completion server (port 3000, http post endpoint at /v2/mcp_eval/run_agent)
# Note: This runs agent completions (not evaluation/scoring). For scoring, see mcp_evals_scores.py
run-mcp-completion: 
	cd services/mcp_eval && uv run python -m mcp_completion.main

check-task-isolation:
	cd services/mcp_eval && uv run python scripts/check_task_isolation.py

cleanup-task-sandboxes:
	cd services/mcp_eval && uv run python scripts/cleanup_task_sandboxes.py

test-completion:
	cd services/mcp_eval && uv run python -m unittest discover -s tests -v

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
