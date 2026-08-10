"use strict";

const { readFileSync } = require("node:fs");
const { dirname, join, resolve, sep } = require("node:path");
const { pathToFileURL } = require("node:url");

const NODE_MODULES = resolve(__dirname, "..", "..", "node_modules");

function parseSource(source) {
  const revisionIndex = source.lastIndexOf("#");
  if (revisionIndex > 0) {
    return {
      name: source.slice(0, revisionIndex),
      expectedRevision: source.slice(revisionIndex + 1),
    };
  }
  const versionIndex = source.lastIndexOf("@");
  if (versionIndex <= 0) {
    throw new Error(`Node MCP source must be exactly pinned: ${source}`);
  }
  return {
    name: source.slice(0, versionIndex),
    expectedVersion: source.slice(versionIndex + 1),
  };
}

function resolveEntrypoint(source, relativeEntrypoint) {
  const { name, expectedRevision, expectedVersion } = parseSource(source);
  const packageRoot = resolve(NODE_MODULES, name);
  const entrypoint = resolve(packageRoot, relativeEntrypoint);
  if (!entrypoint.startsWith(packageRoot + sep)) {
    throw new Error(`Node MCP entrypoint escapes package root: ${relativeEntrypoint}`);
  }
  const metadata = JSON.parse(
    readFileSync(join(packageRoot, "package.json"), "utf8"),
  );
  if (expectedVersion && metadata.version !== expectedVersion) {
    throw new Error(
      `${name} version mismatch: expected ${expectedVersion}, installed ${metadata.version}`,
    );
  }
  if (expectedRevision) {
    const installed = readFileSync(join(packageRoot, ".atlas-revision"), "utf8").trim();
    if (installed !== expectedRevision) {
      throw new Error(
        `${name} revision mismatch: expected ${expectedRevision}, installed ${installed}`,
      );
    }
  }
  return entrypoint;
}

async function main() {
  const [, , source, relativeEntrypoint, ...serverArgs] = process.argv;
  if (!source || !relativeEntrypoint) {
    throw new Error("usage: run_node_mcp.cjs <package@version> <entrypoint> [args...]");
  }
  const entrypoint = resolveEntrypoint(source, relativeEntrypoint);
  if (process.env.MCP_YIBU_SERVICE) {
    require("./yibu_fetch_preload.cjs");
  }
  process.argv = [process.argv[0], entrypoint, ...serverArgs];
  await import(pathToFileURL(entrypoint).href);
}

module.exports = { parseSource, resolveEntrypoint };

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error?.stack || error}\n`);
    process.exitCode = 1;
  });
}
