'use strict';
// Apply the same Gmail quota boundary as the standalone MCP package at build time.
const { readFileSync, writeFileSync, copyFileSync } = require('node:fs');
const { join } = require('node:path');

function patchGoogleWorkspace(packageRoot) {
  const entry = join(packageRoot, 'build/index.js');
  let source = readFileSync(entry, 'utf8');
  const importAnchor = 'import { google } from "googleapis";';
  const setupAnchor = '        this.setupToolHandlers();';
  if (!source.includes('gmailQuota.installGmailQuota')) {
    if (source.split(importAnchor).length !== 2 || source.split(setupAnchor).length !== 2)
      throw new Error('Unsupported pinned Google Workspace MCP entrypoint');
    source = source.replace(importAnchor, importAnchor + '\nimport gmailQuota from "./gmail-quota.cjs";')
      .replace(setupAnchor, '        gmailQuota.installGmailQuota(this.gmail, this.server);\n' + setupAnchor);
    writeFileSync(entry, source);
  }
  copyFileSync(join(__dirname, 'gmail_quota_compat.cjs'), join(packageRoot, 'build/gmail-quota.cjs'));
}

module.exports = { patchGoogleWorkspace };
if (require.main === module)
  patchGoogleWorkspace(join(__dirname, '../../node_modules/@geobio/google-workspace-server'));
