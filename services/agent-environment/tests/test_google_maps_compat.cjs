'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createLegacyFetch } = require('../src/agent_environment/google_maps_compat.cjs');
const legacy = (path, args) => `https://maps.googleapis.com/maps/api/${path}/json?${new URLSearchParams({ key: 'test-key', ...args })}`;
function fixture(data, status = 200) {
  const calls = [];
  const fetch = createLegacyFetch(async (url, options) => {
    calls.push({ url, ...options, body: options?.body && JSON.parse(options.body) });
    return { ok: status < 400, status, json: async () => data };
  });
  return { calls, fetch };
}
test('Places search uses field mask and location bias, preserves numeric zero and place ID', async () => {
  const {fetch,calls} = fixture({places:[{id:'id',displayName:{text:'Museum'},location:{latitude:0,longitude:2},rating:0}]});
  const data = await (await fetch(legacy('place/textsearch',{query:'museum',location:'0,2',radius:'1500'}))).json();
  assert.equal(calls[0].url,'https://places.googleapis.com/v1/places:searchText');
  assert.equal(calls[0].headers['X-Goog-Api-Key'],'test-key');
  assert(calls[0].headers['X-Goog-FieldMask'].includes('places.rating'));
  assert.deepEqual(calls[0].body.locationBias,{circle:{center:{latitude:0,longitude:2},radius:1500}});
  assert.equal(data.results[0].name,'Museum');assert.equal(data.results[0].place_id,'id');
  assert.deepEqual(data.results[0].geometry.location,{lat:0,lng:2});assert.equal(data.results[0].rating,0);
});
test('empty search succeeds with empty places instead of inventing results', async () => {
  const {fetch}=fixture({});
  assert.deepEqual(await (await fetch(legacy('place/textsearch',{query:'nothing'}))).json(),{status:'OK',results:[]});
});
test('details translate review attribution, timestamps, phone, website and opening periods', async () => {
  const {fetch,calls}=fixture({id:'x',nationalPhoneNumber:'123',websiteUri:'https://example.com',
    reviews:[{authorAttribution:{displayName:'User',uri:'https://example.com/user'},rating:5,text:{text:'Good',languageCode:'en'},publishTime:'2020-01-01T00:00:00Z'}],
    regularOpeningHours:{periods:[{open:{day:1,hour:9},close:{day:1,hour:17,minute:30}}],weekdayDescriptions:['Monday: 9–17:30']},currentOpeningHours:{openNow:true}});
  const d=await (await fetch(legacy('place/details',{place_id:'places/x'}))).json();
  assert.equal(calls[0].url,'https://places.googleapis.com/v1/places/x');
  assert.equal(d.result.reviews[0].author_name,'User');assert.equal(d.result.reviews[0].time,1577836800);
  assert.equal(d.result.formatted_phone_number,'123');assert.equal(d.result.website,'https://example.com');
  assert.equal(d.result.opening_hours.open_now,true);assert.deepEqual(d.result.opening_hours.periods,[{open:{day:1,time:'0900'},close:{day:1,time:'1730'}}]);
});
for(const mode of ['driving','walking','bicycling','transit']) {
 test(`directions preserve ${mode} input and legacy distance/duration/steps`,async()=>{
  const {fetch,calls}=fixture({routes:[{description:'Road',legs:[{distanceMeters:1200,duration:'90.5s',steps:[{distanceMeters:20,staticDuration:'10s',travelMode:'WALK',navigationInstruction:{instructions:'Turn left'}}]}]}]});
  const d=await (await fetch(legacy('directions',{origin:'0,2',destination:'place_id:abc',mode}))).json();
  assert.equal(calls[0].body.travelMode,({driving:'DRIVE',walking:'WALK',bicycling:'BICYCLE',transit:'TRANSIT'})[mode]);
  assert.deepEqual(calls[0].body.origin,{location:{latLng:{latitude:0,longitude:2}}});
  assert.deepEqual(calls[0].body.destination,{placeId:'abc'});
  assert.equal(d.routes[0].legs[0].distance.value,1200);assert.equal(d.routes[0].legs[0].duration.value,91);
  assert.equal(d.routes[0].legs[0].steps[0].travel_mode,'WALKING');assert.equal(d.routes[0].legs[0].steps[0].html_instructions,'Turn left');
 });
}
test('matrix sorts indexed responses, handles omitted zero indices, zero values and per-element errors',async()=>{
 const {fetch,calls}=fixture([
  {originIndex:1,destinationIndex:1,condition:'ROUTE_EXISTS',distanceMeters:20,duration:'5s'},
  {condition:'ROUTE_EXISTS'},
  {destinationIndex:1,status:{code:8}},
  {originIndex:1,condition:'ROUTE_NOT_FOUND'}]);
 const d=await (await fetch(legacy('distancematrix',{origins:'A|B',destinations:'C|D',mode:'walking'}))).json();
 assert(calls[0].headers['X-Goog-FieldMask'].includes('status'));
 assert.deepEqual(d.origin_addresses,['A','B']);assert.deepEqual(d.destination_addresses,['C','D']);
 assert.equal(d.rows[0].elements[0].status,'OK');assert.equal(d.rows[0].elements[0].distance.value,0);assert.equal(d.rows[0].elements[0].duration.value,0);
 assert.equal(d.rows[0].elements[1].status,'OVER_QUERY_LIMIT');assert.equal(d.rows[1].elements[0].status,'ZERO_RESULTS');assert.equal(d.rows[1].elements[1].distance.value,20);
});
for(const status of [400,403,429,500]) {
 test(`HTTP ${status} remains a tool error and is not retried`,async()=>{
  const {fetch,calls}=fixture({error:{code:status,message:'Upstream rejected request'}},status);
  const d=await (await fetch(legacy('place/details',{place_id:'x'}))).json();
  assert.notEqual(d.status,'OK');assert(d.error_message.includes(`HTTP ${status}`));assert.equal(calls.length,1);
  assert(!JSON.stringify(d).includes('test-key'));
 });
}
test('geocoding and elevation pass through with original request and options',async()=>{
 for(const path of ['geocode','elevation']) {
  const sentinel={};let recorded;
  const fetch=createLegacyFetch(async(...args)=>{recorded=args;return sentinel});
  const url=legacy(path,{address:'test'});const options={headers:{custom:'yes'}};
  assert.equal(await fetch(url,options),sentinel);assert.deepEqual(recorded,[url,options]);
 }
});
test('network errors are propagated without retries',async()=>{
 let calls=0;const fetch=createLegacyFetch(async()=>{calls++;throw new Error('network failure')});
 await assert.rejects(fetch(legacy('directions',{origin:'a',destination:'b'})),/network failure/);assert.equal(calls,1);
});

test('actual stdio tools/list stays identical to pinned upstream (no network)', async () => {
 const {spawn}=require('node:child_process');const {resolve}=require('node:path');
 const root=resolve(__dirname,'..');
 async function tools(args) {
  const child=spawn(process.execPath,args,{env:{...process.env,GOOGLE_MAPS_API_KEY:'schema-test-key'},stdio:['pipe','pipe','pipe']});
  try {
   return await new Promise((resolvePromise,reject)=>{
    let buffer='',stderr='';const timer=setTimeout(()=>{reject(new Error(`stdio schema timeout: ${stderr}`))},10000);
    child.on('error',(e)=>{clearTimeout(timer);reject(e)});
    child.on('exit',(code)=>{clearTimeout(timer);reject(new Error(`stdio exited ${code}: ${stderr}`))});
    child.stderr.on('data',(s)=>{stderr+=s});
    child.stdout.on('data',(s)=>{
     buffer+=s;let end;
     while((end=buffer.indexOf('\n'))>=0) {
      const line=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!line)continue;
      const msg=JSON.parse(line);
      if(msg.id===1) {
       child.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'})+'\n');
       child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:2,method:'tools/list',params:{}})+'\n');
      }
      if(msg.id===2){clearTimeout(timer);msg.error?reject(new Error(JSON.stringify(msg.error))):resolvePromise(msg.result.tools)}
     }
    });
    child.stdin.write(JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2024-11-05',capabilities:{},clientInfo:{name:'schema-test',version:'1'}}})+'\n');
   });
  } finally {child.kill('SIGTERM')}
 }
 const old=await tools([resolve(root,'node_modules/@modelcontextprotocol/server-google-maps/dist/index.js')]);
 const current=await tools([resolve(root,'src/agent_environment/run_node_mcp.cjs'),'@modelcontextprotocol/server-google-maps@0.6.2','dist/index.js']);
 assert.equal(current.length,7);assert.deepEqual(current,old);
});
