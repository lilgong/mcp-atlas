'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtemp, mkdir, copyFile, readFile, rm, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join, dirname } = require('node:path');
const { patchGoogleWorkspace } = require('../src/agent_environment/patch_google_workspace.cjs');

test('pinned Workspace source receives only the Gmail quota hook and is idempotent', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'atlas-gmail-patch-'));
  try {
    await mkdir(join(directory, 'build'));
    const original = join(dirname(require.resolve('@geobio/google-workspace-server/package.json')), 'build/index.js');
    const source = await readFile(original, 'utf8');
    await copyFile(original, join(directory, 'build/index.js'));
    patchGoogleWorkspace(directory);
    const patched = await readFile(join(directory, 'build/index.js'), 'utf8');
    assert(patched.includes('gmailQuota.installGmailQuota(this.gmail, this.server);'));
    assert.equal(patched.replace('\nimport gmailQuota from "./gmail-quota.cjs";', '')
      .replace('        gmailQuota.installGmailQuota(this.gmail, this.server);\n', ''), source);
    patchGoogleWorkspace(directory);
    assert.equal(await readFile(join(directory, 'build/index.js'), 'utf8'), patched);
    assert.equal(await readFile(join(directory, 'build/gmail-quota.cjs'), 'utf8'),
      await readFile(join(__dirname, '../src/agent_environment/gmail_quota_compat.cjs'), 'utf8'));
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('an unsupported Workspace entrypoint fails before modifying its source', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'atlas-gmail-patch-'));
  try {
    await mkdir(join(directory, 'build'));
    await writeFile(join(directory, 'build/index.js'), 'unsupported');
    assert.throws(() => patchGoogleWorkspace(directory), /Unsupported pinned/);
    assert.equal(await readFile(join(directory, 'build/index.js'), 'utf8'), 'unsupported');
  } finally { await rm(directory, { recursive: true, force: true }); }
});
