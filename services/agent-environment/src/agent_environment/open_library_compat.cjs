'use strict';
const { pathToFileURL } = require('node:url');
const { dirname, join } = require('node:path');
const { performance } = require('node:perf_hooks');
const { Readable } = require('node:stream');
const { STATUS_CODES } = require('node:http');

function installLimiter(axios, interval) {
  let queue = Promise.resolve();
  let lastStarted = -Infinity;
  axios.interceptors.request.use(async (config) => {
    const url = new URL(config.url, config.baseURL);
    if (url.hostname !== 'openlibrary.org') return config;
    const wait = queue.then(async () => {
      const delay = lastStarted + interval - performance.now();
      if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
      lastStarted = performance.now();
    });
    queue = wait.catch(() => undefined);
    await wait;
    return config;
  });
}

function installRelay(instance, transport, env = process.env) {
  instance.interceptors.request.use((config) => {
    const url = new URL(instance.getUri(config));
    if (url.hostname !== 'openlibrary.org' && !url.hostname.endsWith('.openlibrary.org')) return config;
    const relay = (env.MCP_RELAY_URL || '').trim().replace(/\/$/, '');
    if (!relay) return config;
    const token = (env.MCP_RELAY_TOKEN || '').trim();
    if (!token) throw new Error('Open Library Relay token is not configured');
    const seconds = Number(env.MCP_RELAY_TIMEOUT_SECONDS || 90);
    if (!Number.isFinite(seconds) || seconds <= 0) throw new Error('Invalid Open Library Relay timeout');
    config.adapter = async (request) => {
      if ((request.method || 'get').toLowerCase() !== 'get')
        throw new Error('Open Library Relay supports GET requests only');
      const timeout = request.timeout > 0 ? Math.min(request.timeout, seconds * 1000) : seconds * 1000;
      const headers = {};
      const originalHeaders = request.headers?.toJSON?.() || request.headers || {};
      for (const name of ['accept', 'accept-language', 'user-agent']) {
        const pair = Object.entries(originalHeaders).find(([key]) => key.toLowerCase() === name);
        if (pair && typeof pair[1] === 'string') headers[name] = pair[1];
      }
      let response;
      try {
        response = await transport.post(`${relay}/v1/fetch`, {
          url: url.href, headers, allow_redirects: request.maxRedirects !== 0,
          timeout_seconds: Math.max(0.001, timeout / 1000 * 0.9),
        }, {
          headers: { Authorization: `Bearer ${token}` },
          timeout, signal: request.signal, responseType: 'json',
          validateStatus: () => true,
        });
      } catch (error) {
        if (transport.isCancel?.(error)) throw new transport.CanceledError('Open Library request cancelled');
        throw new Error('Open Library Relay request failed or timed out');
      }
      if (response.status !== 200) throw new Error(`Open Library Relay HTTP ${response.status}`);
      const data = response.data;
      if (!data || !Number.isInteger(data.status_code) || typeof data.body_base64 !== 'string')
        throw new Error('Open Library Relay returned an invalid response');
      const body = Buffer.from(data.body_base64, 'base64');
      const result = {
        status: data.status_code, statusText: STATUS_CODES[data.status_code] || '',
        headers: data.headers || {}, config: request,
        data: request.responseType === 'arraybuffer' ? body :
          request.responseType === 'stream' ? Readable.from([body]) : body.toString(request.responseEncoding || 'utf8'),
      };
      if (request.validateStatus && !request.validateStatus(result.status)) {
        const error = new Error(`Open Library upstream HTTP ${result.status}`);
        error.isAxiosError = true;
        error.response = result;
        throw error;
      }
      return result;
    };
    return config;
  });
}

async function main() {
  const source = '@geobio/mcp-open-library@0.1.6';
  if (process.argv[2] && process.argv[2] !== source) throw new Error('Unsupported Open Library MCP version');
  const { resolveEntrypoint } = require('./run_node_mcp.cjs');
  const entry = resolveEntrypoint(source, 'build/index.js');
  const packageFile = require.resolve('axios/package.json', { paths: [dirname(entry)] });
  const { default: axios } = await import(pathToFileURL(join(dirname(packageFile), 'index.js')).href);
  const interval = Number(process.env.OPEN_LIBRARY_MIN_INTERVAL_MS ?? 1000);
  if (!Number.isSafeInteger(interval) || interval < 0 || interval > 600000) throw new Error('Invalid OPEN_LIBRARY_MIN_INTERVAL_MS');
  const create = axios.create.bind(axios);
  axios.create = (...args) => {
    const instance = create(...args);
    installLimiter(instance, interval);
    installRelay(instance, axios);
    return instance;
  };
  installLimiter(axios, interval);
  installRelay(axios, axios);
  process.argv = [process.argv[0], entry];
  await import(pathToFileURL(entry).href);
}
module.exports = { installLimiter, installRelay, main };
if (require.main === module) main().catch(() => { process.stderr.write('Open Library startup failed\n'); process.exitCode = 1; });
