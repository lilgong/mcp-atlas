# MCP Eval

A standalone Python package for running multi-turn LLM conversations that leverage MCP servers for tool use and function calling. Requires the versioned fixture-free MCP runtime described in the repository root README.

## Overview

- HTTP server on port 3000
- `/v2/mcp_eval/run_agent` POST endpoint, with params: model (llm model), messages (single message), enabledTools (which mcp server tools to enable)
- No authentication for calling this endpoint.
- Set LLM endpoint and API key in `.env`. For fastest setup, use with openai api key. Also designed to work with LiteLLM. You don't have to use LiteLLM tho.

## Setup

1. Install dependencies:
```bash
uv sync
```

2. Configure environment:
```bash
cp env.template .env
```
Edit .env file with your API keys `LLM_API_KEY`. Could be an openai api key or LiteLLM key. The default `LLM_BASE_URL` is for openai, but modify it if you're using LiteLLM.

3. Start the MCP server:
Follow the root README to build `mcp-atlas-runtime:<version>`, prepare
`MCP_TASK_DATA_DIR`, and start the shared service. It should expose HTTP POST
`/list-tools` and `/call-tool` on `MCP_SERVER_URL`; local stateful tools are
routed into disposable per-task containers using a copied external fixture.

4. Run the server:
```bash
make run
```
It will run a server on http://localhost:3000 unless you specify otherwise in `.env`

## Usage

Make requests to the evaluation endpoint:
Note that openai has max 128 tools, so if enabledTools is empty, then all ~400 tools would be listed, which would error.

Using the calculator tool (assuming that the MCP server has this tool):
```bash
curl -X POST http://localhost:3000/v2/mcp_eval/run_agent \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o",
    "messages": [{"role": "user", "content": "What is the square root if 95?"}],
    "enabledTools": ["calculator_calculate"],
    "maxTurns": 20
  }'
```
