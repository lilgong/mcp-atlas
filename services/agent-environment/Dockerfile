FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y npm bash git procps curl gettext-base && rm -rf /var/lib/apt/lists/*

# Need to install nodejs from elsewhere, because bookworm-slim is based on debian, and the available version of nodejs is 18, which is too old
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get update
RUN apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*

# Pre-install all MCP server packages (NPX and UVX), saves approx 1 minute of startup time
COPY dev_scripts/install_mcp_packages.sh /
RUN /install_mcp_packages.sh && rm /install_mcp_packages.sh

# mcp_server_template.json runs the node servers from /mnt/node_modules, which npm's
# global prefix (/usr/lib/node_modules) doesn't provide. Copy rather than symlink:
# the next step overwrites files in here, and a symlink would corrupt the npm tree.
RUN cp -r /usr/lib/node_modules /mnt/node_modules

# Our LLM traffic goes through a third-party gateway, and three of the node servers
# hard-code their vendor's endpoint and auth header. vendor/yibu-patched holds the
# handful of files with those rewritten (api.search.brave.com -> the gateway,
# X-Subscription-Token -> Authorization: Bearer, and the same for exa). Without them
# the servers start and report healthy while sending gateway keys to the vendors'
# real endpoints, which answer 401/422 — reads exactly like an expired key.
COPY vendor/yibu-patched/node_modules/ /mnt/node_modules/

# Copy project files and set up workspace
WORKDIR /agent-environment
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
