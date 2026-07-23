#!/bin/bash
set -euo pipefail

mongorestore \
  --drop \
  --nsInclude='video_game_store.*' \
  /opt/mcp-atlas-fixture/mongo_dump_video_game_store
