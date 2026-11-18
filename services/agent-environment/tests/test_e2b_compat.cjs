'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { installSandboxCompatibility } = require('../src/agent_environment/e2b_compat.cjs');

for (const outcome of ['success', 'execution-failure', 'cleanup-failure']) {
  test(`E2B cleans up after ${outcome}, preserves result/error and never reruns code`, async () => {
    let runs = 0, kills = 0;
    const failure = new Error('execution failed');
    const messages = [];
    const sandbox = {
      async runCode() { runs++; if (outcome === 'execution-failure') throw failure; return { results: [123], logs: {} }; },
      async kill() { kills++; if (outcome === 'cleanup-failure') throw new Error('private-details'); },
    };
    const Sandbox = { async create() { return sandbox; } };
    installSandboxCompatibility(Sandbox, { minIntervalMs: 1, diagnostic: (msg) => messages.push(msg) });
    const created = await Sandbox.create();
    if (outcome === 'execution-failure') await assert.rejects(created.runCode('code'), (error) => error === failure);
    else assert.deepEqual(await created.runCode('code'), { results: [123], logs: {} });
    assert.equal(runs, 1); assert.equal(kills, 1);
    assert.equal(messages.length, outcome === 'cleanup-failure' ? 1 : 0);
    assert.ok(messages.every((message) => !message.includes('private-details')));
  });
}

test('E2B paces actual creations, retries rejected creations only, bounds retries', async () => {
  let clock = 100, attempts = 0;
  const starts = [];
  const rate = () => Object.assign(new Error('rate limited'), { name: 'RateLimitError' });
  const Sandbox = { async create() { starts.push(clock); if (++attempts <= 2) throw rate(); return {}; } };
  installSandboxCompatibility(Sandbox, { minIntervalMs: 1000, now: () => clock,
    sleep: async (ms) => { clock += ms; }, diagnostic: () => {} });
  await Sandbox.create(); await Sandbox.create();
  assert.equal(attempts, 4);
  assert.ok(starts.slice(1).every((value, index) => value - starts[index] >= 1000));
  let rejected = 0;
  const Broken = { async create() { rejected++; throw rate(); } };
  installSandboxCompatibility(Broken, { minIntervalMs: 1, sleep: async () => {}, diagnostic: () => {} });
  await assert.rejects(Broken.create(), { name: 'RateLimitError' });
  assert.equal(rejected, 4);
  let failures = 0;
  const Unauthorized = { async create() { failures++; throw new Error('unauthorized'); } };
  installSandboxCompatibility(Unauthorized, { diagnostic: () => {} });
  await assert.rejects(Unauthorized.create()); assert.equal(failures, 1);
});
