const test = require('node:test');
const assert = require('node:assert/strict');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const packageRoot = process.env.ATLAS_MET_PACKAGE_ROOT || resolve(__dirname, '../node_modules/metmuseum-mcp');
const { installMetCompatibility } = require('../src/agent_environment/met_museum_compat.cjs');
test('Met preserves schema, maps pagination/filters, clamps last page and preserves errors', async () => {
  const { SearchMuseumObjectsTool } = await import(pathToFileURL(packageRoot + '/dist/tools/SearchMuseumObjectsTool.js').href);
  const before = new SearchMuseumObjectsTool({});
  const schema = before.inputSchema;
  await installMetCompatibility(packageRoot);
  assert.equal(new SearchMuseumObjectsTool({}).inputSchema, schema);
  const urls = [];
  const tool = new SearchMuseumObjectsTool({ async fetchJson(url) {
    urls.push(new URL(url)); return { total: 53, objectIDs: [111, 222] };
  } });
  const result = await tool.execute({ q: 'Monet', title: true, page: 2, pageSize: 24, departmentId: 11 });
  assert.equal(result.isError, false);
  assert.deepEqual(result.structuredContent, { total: 53, page: 2, pageSize: 24, totalPages: 3, objectIDs: [111, 222] });
  assert.equal(urls[0].pathname, '/public/collection/v1.1/search');
  assert.equal(urls[0].searchParams.get('offset'), '24');
  assert.equal(urls[0].searchParams.get('limit'), '24');
  assert.equal(urls[0].searchParams.get('title'), 'true');
  assert.equal(urls[0].searchParams.get('departmentId'), '11');
  const last = await tool.execute({ q: 'Monet', page: 99, pageSize: 24 });
  assert.equal(last.structuredContent.page, 3);
  assert.equal(urls.at(-1).searchParams.get('offset'), '48');
  const count = urls.length;
  assert.equal((await tool.execute({ q: 'Monet', dateBegin: 1800 })).isError, true);
  assert.equal(urls.length, count);
  const empty = new SearchMuseumObjectsTool({ async fetchJson() { return { total: 0, objectIDs: null }; } });
  assert.equal((await empty.execute({ q: 'unlikely' })).structuredContent.totalPages, 0);
  const malformed = new SearchMuseumObjectsTool({ async fetchJson() { return { total: 4, objectIDs: null }; } });
  assert.equal((await malformed.execute({ q: 'Monet' })).isError, true);
});

test('installation is idempotent and preserves upstream failures without retrying', async () => {
  const { SearchMuseumObjectsTool } = await import(pathToFileURL(packageRoot + '/dist/tools/SearchMuseumObjectsTool.js').href);
  const { MetMuseumApiError } = await import(pathToFileURL(packageRoot + '/dist/api/MetMuseumApiClient.js').href);
  await installMetCompatibility(packageRoot);
  const execute = SearchMuseumObjectsTool.prototype.execute;
  await installMetCompatibility(packageRoot);
  assert.equal(SearchMuseumObjectsTool.prototype.execute, execute);
  let calls = 0;
  const tool = new SearchMuseumObjectsTool({ async fetchJson() { calls++; throw new MetMuseumApiError('Rate limited', 429, true); } });
  const result = await tool.execute({q:'Monet'});
  assert.equal(result.isError, true);
  assert.equal(result.content[0].text, 'Rate limited');
  assert.equal(calls, 1);
});


test('out-of-range null IDs clamp to last page before ID validation', async () => {
  const { SearchMuseumObjectsTool } = await import(pathToFileURL(packageRoot + '/dist/tools/SearchMuseumObjectsTool.js').href);
  await installMetCompatibility(packageRoot);
  const urls=[];
  const tool=new SearchMuseumObjectsTool({async fetchJson(raw) {
    const url=new URL(raw);urls.push(url);
    return Number(url.searchParams.get('offset')) >= 124
      ? {total:124,objectIDs:null} : {total:124,objectIDs:[121,122,123,124]};
  }});
  const result=await tool.execute({q:'Cello',page:99,pageSize:5});
  assert.equal(result.isError,false);
  assert.deepEqual(result.structuredContent,{total:124,page:25,pageSize:5,totalPages:25,objectIDs:[121,122,123,124]});
  assert.deepEqual(urls.map(u=>u.searchParams.get('offset')),['490','120']);
  assert(urls.every(u=>u.searchParams.get('q')==='Cello' && u.searchParams.get('limit')==='5'));
});

for (const objectIDs of [null, 'bad', [1,'bad']]) {
  test(`invalid IDs on the clamped page remain errors: ${JSON.stringify(objectIDs)}`, async () => {
    const { SearchMuseumObjectsTool } = await import(pathToFileURL(packageRoot + '/dist/tools/SearchMuseumObjectsTool.js').href);
    await installMetCompatibility(packageRoot);
    let calls=0;
    const tool=new SearchMuseumObjectsTool({async fetchJson() { return {total:124,objectIDs:++calls===1?null:objectIDs}; }});
    const result=await tool.execute({q:'Cello',page:99,pageSize:5});
    assert.equal(result.isError,true);assert.equal(calls,2);
  });
}
