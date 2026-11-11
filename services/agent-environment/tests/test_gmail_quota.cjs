const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {GmailQuota,minuteQuota,installGmailQuota}=require('../src/agent_environment/gmail_quota_compat.cjs');
async function fixture(fn){const directory=await fs.mkdtemp(path.join(os.tmpdir(),'gmail-quota-test-'));try{await fn(directory);}finally{await fs.rm(directory,{recursive:true,force:true});}}
test('shared weighted window bounds concurrent requests across limiter instances',()=>fixture(async directory=>{
 const instances=[new GmailQuota({directory,scope:'account',units:100,windowMs:100}),new GmailQuota({directory,scope:'account',units:100,windowMs:100})];
 const times=[];await Promise.all(Array.from({length:12},(_,i)=>instances[i%2].call('get',async()=>{times.push(Date.now());return 'ok';},Date.now()+3000)));
 assert.equal(times.length,12);assert(times.at(-1)-times[0]>=200);
 const records=JSON.parse(await fs.readFile(instances[0].file));assert(records.events.reduce((n,e)=>n+e[1],0)<=100);
 const other=new GmailQuota({directory,scope:'other',units:100,windowMs:100});assert.notEqual(other.file,instances[0].file);
}));
test('temporary minute quotas retry reads and honor shared cooldown',()=>fixture(async directory=>{
 const q=new GmailQuota({directory,scope:'account'});let calls=0;
 const error=Object.assign(new Error("Quota exceeded: Units per minute per user"),{code:403});
 const result=await q.call('get',async()=>{if(++calls===1)throw error;return 'ok';},Date.now()+6000);
 assert.equal(result,'ok');assert.equal(calls,2);
}));
test('writes and invalid credentials are never replayed',()=>fixture(async directory=>{
 const q=new GmailQuota({directory,scope:'account'});let calls=0;
 const limited=Object.assign(new Error('rate limit'),{code:429});
 await assert.rejects(q.call('send',async()=>{calls++;throw limited;},Date.now()+5000));assert.equal(calls,1);
 const invalid=Object.assign(new Error('Invalid Credentials'),{code:401});
 assert(!minuteQuota(invalid));
 await assert.rejects(q.call('get',async()=>{calls++;throw invalid;},Date.now()+5000));assert.equal(calls,2);
}));
test('deadline removes queued work before HTTP is started',()=>fixture(async directory=>{
 const q=new GmailQuota({directory,scope:'account',units:100,windowMs:1000});
 await q.acquire(100,Date.now()+500);let calls=0;
 await assert.rejects(q.call('get',async()=>{calls++;},Date.now()+40),/GMAIL_RATE_LIMIT/);assert.equal(calls,0);
}));
test('quota wrapper shares a tool deadline and disables hidden HTTP retries',()=>fixture(async directory=>{
 const old=process.env.GOOGLE_GMAIL_QUOTA_DIR;process.env.GOOGLE_GMAIL_QUOTA_DIR=directory;
 try{
  let handler,options;const server={setRequestHandler(_schema,h){handler=h;}};
  const gmail={users:{messages:Object.fromEntries(['list','get','modify','send'].map(n=>[n,async(_params,o)=>{options=o;return 'ok';}]))}};
  installGmailQuota(gmail,server);server.setRequestHandler({},async()=>gmail.users.messages.get({id:'x'}));
  assert.equal(await handler(),'ok');assert.equal(options.retry,false);assert(options.timeout<=55000 && options.timeout>0);
 }finally{if(old===undefined)delete process.env.GOOGLE_GMAIL_QUOTA_DIR;else process.env.GOOGLE_GMAIL_QUOTA_DIR=old;}
}));
test('separate OS processes coordinate the same quota ledger',()=>fixture(async directory=>{
 const {spawn}=require('node:child_process');
 const modulePath=path.resolve(__dirname,'../src/agent_environment/gmail_quota_compat.cjs');
 const script=`const {GmailQuota}=require(${JSON.stringify(modulePath)});const q=new GmailQuota({directory:process.argv[1],scope:'cross-process',units:100,windowMs:100});const grants=[];const state=q.state.bind(q);q.state=(update,deadline)=>state(s=>{const last=s.events.at(-1);const result=update(s);if(s.events.length&&s.events.at(-1)!==last)grants.push(s.events.at(-1)[0]);return result;},deadline);Promise.all(Array.from({length:6},()=>q.call('get',async()=>{},Date.now()+5000))).then(()=>console.log(JSON.stringify(grants))).catch(e=>{console.error(e);process.exit(1)});`;
 async function run(){return new Promise((resolve,reject)=>{let out='';const p=spawn(process.execPath,['-e',script,directory]);p.stdout.on('data',x=>out+=x);p.on('error',reject);p.on('exit',code=>code?reject(new Error('child failed')):resolve(JSON.parse(out)));});}
 // Quota applies to admission times; callback execution may be delayed by scheduling/I/O.
 const all=(await Promise.all([run(),run()])).flat().sort((a,b)=>a-b);
 assert.equal(all.length,12);assert(all.at(-1)-all[0]>=200);
 for(const t of all)assert(all.filter(x=>x>t-100&&x<=t).length<=5);
}));
