#!/bin/bash
set -euo pipefail

# Runtime scaffolding is created only after Docker has attached the external
# /data volume. No task or official fixture exists in an image layer.
mkdir -p \
  /data/repos/mcp_code_executor_workspace \
  /data/repos/memory_mcp_server

memory_file=/data/repos/memory_mcp_server/memories-for-mcp.json
if [[ ! -e "$memory_file" ]]; then
  printf '%s\n' '{"entities":[],"relations":[]}' > "$memory_file"
fi

envsubst \
  < src/agent_environment/mcp_server_template.json \
  > src/agent_environment/mcp_server_config.json

exec "$@"
