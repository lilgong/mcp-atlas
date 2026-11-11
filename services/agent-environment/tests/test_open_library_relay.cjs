const { test } = require('node:test');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const { installRelay, installLimiter } = require('../src/agent_environment/open_library_compat.cjs');
const { dirname, join } = require('node:path');
const axiosPath = join(dirname(require.resolve('axios/package.json')), 'index.js');
const env = { MCP_RELAY_URL: 'http://relay:3985', MCP_RELAY_TOKEN: 'do-not-log-this-token' };
async function setup(reply = { status: 200, data: { status_code: 200, headers: {}, body_base64: Buffer.from('{"docs":[{"title":"The Hobbit"}]}').toString('base64') } }) {
 const axios = (await import(pathToFileURL(axiosPath))).default;
 const calls = [];
 const transport = { post: async (...args) => { calls.push(args); return reply; } };
 const instance = axios.create({ baseURL: 'https://openlibrary.org', headers: { Authorization: 'private-not-forwarded' } });
 installLimiter(instance, 0); installRelay(instance, transport, env);
 return { instance, calls };
}
test('GET query is preserved and the response follows original Axios JSON decoding', async () => {
 const { instance, calls } = await setup();
 const response = await instance.get('/search.json', { params: { title: 'A & B' } });
 assert.equal(response.data.docs[0].title, 'The Hobbit');
 assert.equal(calls.length, 1);
 assert.equal(new URL(calls[0][1].url).searchParams.get('title'), 'A & B');
 assert.equal(calls[0][1].headers.authorization, undefined);
 assert.equal(calls[0][2].headers.Authorization, 'Bearer '+env.MCP_RELAY_TOKEN);
});
test('upstream failures remain errors rather than successful book data', async () => {
 const {instance}=await setup({status:200,data:{status_code:429,headers:{},body_base64:Buffer.from('too many').toString('base64')}});
 await assert.rejects(instance.get('/search.json'), /upstream HTTP 429/);
});
test('Relay errors do not disclose credentials', async () => {
 const {instance}=await setup({status:502,data:{error:'private-token'}});
 await assert.rejects(instance.get('/search.json'),e=>e.message==='Open Library Relay HTTP 502'&&!JSON.stringify(e).includes(env.MCP_RELAY_TOKEN));
});
test('non Open Library requests and disabled Relay retain the native adapter', async () => {
 const {instance,calls}=await setup();
 const native=async config=>({data:'native',status:200,headers:{},config});
 assert.equal((await instance.get('https://example.org/a',{adapter:native})).data,'native');
 assert.equal(calls.length,0);
 const axios=(await import(pathToFileURL(axiosPath))).default;const direct=axios.create({baseURL:'https://openlibrary.org',adapter:native});installRelay(direct,{},{});
 assert.equal((await direct.get('/a')).data,'native');
});
test('binary response, cancellation and original configured timeouts are preserved',async()=>{
 const {instance,calls}=await setup();const response=await instance.get('/a',{responseType:'arraybuffer',timeout:5000});
 assert(Buffer.isBuffer(response.data));assert.equal(calls[0][2].timeout,5000);assert.equal(calls[0][1].timeout_seconds,4.5);
 const controller=new AbortController();controller.abort();await assert.rejects(instance.get('/a',{signal:controller.signal}),/canceled/i);assert.equal(calls.length,1);
});
test('missing tokens fail without a direct network fallback',async()=>{
 const axios=(await import(pathToFileURL(axiosPath))).default;const instance=axios.create({baseURL:'https://openlibrary.org'});installRelay(instance,{}, {MCP_RELAY_URL:'http://relay:3985'});
 await assert.rejects(instance.get('/a'),/token is not configured/);
});
