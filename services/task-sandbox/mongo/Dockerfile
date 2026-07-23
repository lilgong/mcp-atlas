FROM mongo:7.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/*

COPY data_exports/mongo_dump_video_game_store-UNZIP-FIRST.zip /tmp/mcp-atlas-mongo.zip
RUN mkdir -p /opt/mcp-atlas-fixture \
    && unzip -q /tmp/mcp-atlas-mongo.zip -d /opt/mcp-atlas-fixture \
    && rm /tmp/mcp-atlas-mongo.zip

COPY services/task-sandbox/mongo/10-restore-fixture.sh /docker-entrypoint-initdb.d/10-restore-fixture.sh
RUN chmod 0755 /docker-entrypoint-initdb.d/10-restore-fixture.sh
