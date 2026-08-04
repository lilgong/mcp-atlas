import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const sourcePath =
  "/mnt/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js";
const compatPath =
  "/mnt/node_modules/@modelcontextprotocol/server-filesystem/dist/index.compat.js";
const packagePath =
  "/mnt/node_modules/@modelcontextprotocol/server-filesystem/package.json";
const expectedVersion = "2025.11.25";

const packageMetadata = JSON.parse(await readFile(packagePath, "utf8"));
const actualVersion = String(packageMetadata.version ?? "");
if (actualVersion !== expectedVersion) {
  throw new Error(
    `filesystem compatibility shim requires ` +
      `@modelcontextprotocol/server-filesystem@${expectedVersion}, ` +
      `found ${actualVersion || "<missing version>"}`,
  );
}

const source = await readFile(sourcePath, "utf8");
const sectionStart = source.indexOf('server.registerTool("directory_tree"');
const sectionEnd = source.indexOf('server.registerTool("move_file"', sectionStart);
if (sectionStart < 0 || sectionEnd < 0) {
  throw new Error(
    `filesystem directory_tree patch anchors were not found in ` +
      `@modelcontextprotocol/server-filesystem@${actualVersion}`,
  );
}

const section = source.slice(sectionStart, sectionEnd);
const buggy = "structuredContent: { content: [contentBlock] }";
const occurrences = section.split(buggy).length - 1;
if (occurrences !== 1) {
  throw new Error(
    `expected one directory_tree structured-content mismatch in ` +
      `@modelcontextprotocol/server-filesystem@${actualVersion}, ` +
      `found ${occurrences}`,
  );
}

const corrected = section.replace(
  buggy,
  "structuredContent: { content: text }",
);
await writeFile(
  compatPath,
  source.slice(0, sectionStart) + corrected + source.slice(sectionEnd),
  "utf8",
);
await import(pathToFileURL(compatPath).href);
