'use strict';

const { performance } = require('node:perf_hooks');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function installSandboxCompatibility(Sandbox, options = {}) {
  if (Sandbox.create._sandboxLifecycleCompat) return;
  const configured = Number(process.env.E2B_SANDBOX_MIN_INTERVAL_MS || 1000);
  const minIntervalMs = options.minIntervalMs ??
    (Number.isFinite(configured) && configured > 0 ? configured : 1000);
  const wait = options.sleep || sleep;
  const now = options.now || (() => performance.now());
  const diagnostic = options.diagnostic || ((message) => process.stderr.write(`[e2b] ${message}\n`));
  let nextStart = 0;
  let gate = Promise.resolve();
  const reserveCreation = () => {
    const permit = gate.then(async () => {
      const delay = nextStart - now();
      if (delay > 0) await wait(delay);
      nextStart = now() + minIntervalMs;
    });
    gate = permit.catch(() => undefined);
    return permit;
  };
  const originalCreate = Sandbox.create;
  async function create(...args) {
    let sandbox;
    // Retry only rejected sandbox creation; user code is executed exactly once.
    for (let attempt = 0; ; attempt++) {
      await reserveCreation();
      try {
        sandbox = await originalCreate.apply(this, args);
        break;
      } catch (error) {
        if (error?.name !== 'RateLimitError' || attempt >= 3) throw error;
        diagnostic(`sandbox creation rate-limited; retry ${attempt + 1}/3`);
        await wait(1000 * (2 ** attempt));
      }
    }
    const originalRunCode = sandbox.runCode;
    sandbox.runCode = async function (...codeArgs) {
      try {
        return await originalRunCode.apply(this, codeArgs);
      } finally {
        try {
          await this.kill();
        } catch {
          // Preserve execution output/errors; expose cleanup failure without secrets.
          diagnostic('sandbox cleanup failed; cloud timeout will release it');
        }
      }
    };
    return sandbox;
  }
  create._sandboxLifecycleCompat = true;
  Sandbox.create = create;
}
async function installE2bCompatibility(packageRoot) {
  const { createRequire } = require('node:module');
  const { join } = require('node:path');
  const resolve = createRequire(join(packageRoot, 'package.json'));
  const { Sandbox } = await import(require('node:url').pathToFileURL(resolve.resolve('@e2b/code-interpreter')).href);
  installSandboxCompatibility(Sandbox);
}
module.exports = { installE2bCompatibility, installSandboxCompatibility };
