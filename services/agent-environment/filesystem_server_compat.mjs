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
const buggy = "structuredContent: { content: [contentBlock] }";
const correctedValue = "structuredContent: { content: text }";
const toolsToPatch = ["list_directory_with_sizes", "directory_tree"];
let correctedSource = source;
for (const toolName of toolsToPatch) {
  const sectionStart = correctedSource.indexOf(
    `server.registerTool("${toolName}"`,
  );
  const sectionEnd = correctedSource.indexOf(
    "server.registerTool(", sectionStart + 1,
  );
  if (sectionStart < 0 || sectionEnd < 0) {
    throw new Error(
      `filesystem ${toolName} patch anchors were not found in ` +
        `@modelcontextprotocol/server-filesystem@${actualVersion}`,
    );
  }
  const section = correctedSource.slice(sectionStart, sectionEnd);
  const occurrences = section.split(buggy).length - 1;
  if (occurrences !== 1) {
    throw new Error(
      `expected one ${toolName} structured-content mismatch in ` +
        `@modelcontextprotocol/server-filesystem@${actualVersion}, ` +
        `found ${occurrences}`,
    );
  }
  correctedSource =
    correctedSource.slice(0, sectionStart) +
    section.replace(buggy, correctedValue) +
    correctedSource.slice(sectionEnd);
}
await writeFile(compatPath, correctedSource, "utf8");
await import(pathToFileURL(compatPath).href);
