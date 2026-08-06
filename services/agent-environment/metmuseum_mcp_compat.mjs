#!/usr/bin/env node
/** Run metmuseum-mcp 0.9.2 while accepting nullable tag URL fields. */

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const EXPECTED_VERSION = "0.9.2";
const root = process.env.METMUSEUM_MCP_PACKAGE_ROOT || "/mnt/node_modules/metmuseum-mcp";
const metadata = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
if (metadata.version !== EXPECTED_VERSION) {
  throw new Error(
    `Met compatibility wrapper expected metmuseum-mcp==${EXPECTED_VERSION}, found ${metadata.version}`,
  );
}

const { ObjectResponseSchema } = await import(
  pathToFileURL(path.join(root, "dist/types/types.js")).href
);
const originalSafeParse = ObjectResponseSchema.safeParse.bind(ObjectResponseSchema);
ObjectResponseSchema.safeParse = (input) => {
  if (input && Array.isArray(input.tags)) {
    for (const tag of input.tags) {
      if (!tag || typeof tag !== "object") continue;
      if (tag.AAT_URL === null) delete tag.AAT_URL;
      if (tag.Wikidata_URL === null) delete tag.Wikidata_URL;
    }
  }
  return originalSafeParse(input);
};

await import(pathToFileURL(path.join(root, "dist/index.js")).href);
