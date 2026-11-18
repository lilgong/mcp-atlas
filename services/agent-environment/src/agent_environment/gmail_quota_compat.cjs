'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { AsyncLocalStorage } = require('node:async_hooks');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const context = new AsyncLocalStorage();
const costs = { list: 5, get: 20, send: 100, modify: 5 };
function minuteQuota(error) {
  const status = Number(error.response?.status || error.code);
  const text = `${error.message || ''} ${JSON.stringify(error.response?.data || {})}`.toLowerCase();
  return status === 429 || ((status === 403 || text.includes('quota exceeded')) &&
    /per minute|per second|ratelimitexceeded|userratelimitexceeded/.test(text));
}
class GmailQuota {
  constructor({directory, scope, units = 6000, windowMs = 60000, now = Date.now, sleep = delay} = {}) {
    if (!Number.isFinite(units) || units < 100 || !Number.isFinite(windowMs) || windowMs <= 0) throw new Error('Invalid Gmail quota configuration');
    this.units = units; this.windowMs = windowMs; this.now = now; this.sleep = sleep;
    this.file = path.join(directory, crypto.createHash('sha256').update(scope).digest('hex') + '.json');
  }
  remaining(deadline) {
    const remaining = deadline - this.now();
    if (remaining <= 0) throw new Error('GMAIL_RATE_LIMIT: Gmail quota queue/retry deadline exceeded');
    return remaining;
  }
  async state(update, deadline) {
    await fs.mkdir(path.dirname(this.file), {recursive:true});
    const lock = this.file + '.lock';
    while (true) {
      this.remaining(deadline);
      try { await fs.mkdir(lock); break; } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        // A crashed process must not leave a permanent quota lock.
        const stat = await fs.stat(lock).catch(() => null);
        if (stat && this.now() - stat.mtimeMs > 30000) await fs.rmdir(lock).catch(() => {});
        await this.sleep(Math.min(20, this.remaining(deadline)));
      }
    }
    try {
      let state;
      try { state = JSON.parse(await fs.readFile(this.file,'utf8')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; state = {events:[],next:0,cooldown:0}; }
      const result = update(state);
      const temp = this.file + '.' + process.pid + '.' + crypto.randomUUID();
      try { await fs.writeFile(temp,JSON.stringify(state),{mode:0o600}); await fs.rename(temp,this.file); }
      finally { await fs.rm(temp,{force:true}); }
      return result;
    } finally { await fs.rmdir(lock); }
  }
  async acquire(cost, deadline) {
    while (true) {
      const wait = await this.state(state => {
        const now = this.now();
        state.events = state.events.filter(e => e[0] > now-this.windowMs);
        const used = state.events.reduce((n,e)=>n+e[1],0);
        let ready = Math.max(state.next, state.cooldown);
        if (used+cost>this.units) {
          let released=0;
          for (const e of state.events) { released+=e[1]; if (used-released+cost<=this.units) { ready=Math.max(ready,e[0]+this.windowMs);break; } }
        }
        if (ready>now) return ready-now;
        state.events.push([now,cost]);
        state.next=now+cost*this.windowMs/this.units;
        return 0;
      },deadline);
      if (!wait) return;
      await this.sleep(Math.min(wait,this.remaining(deadline)));
    }
  }
  async call(method, run, deadline) {
    const cost=costs[method];
    for (let attempt=0;attempt<4;attempt++) {
      await this.acquire(cost,deadline);
      try { return await run(this.remaining(deadline)); }
      catch(error) {
        if (!minuteQuota(error)) throw error;
        const raw=error.response?.headers?.get?.('retry-after') || error.response?.headers?.['retry-after'];
        const seconds=Number(raw);
        const retryAfter=raw ? (Number.isFinite(seconds) ? seconds*1000 : Math.max(0,Date.parse(raw)-this.now())) : 0;
        const backoff=Math.max(Number.isFinite(retryAfter)?retryAfter:0,1000*2**attempt+Math.random()*1000);
        await this.state(state=>{state.cooldown=Math.max(state.cooldown,this.now()+backoff);},deadline);
        process.stderr.write(`[gmail-quota] temporary_rate_limit method=${method} attempt=${attempt+1} cooldown_ms=${Math.ceil(backoff)}\n`);
        // Writes are never replayed; a lost response can conceal a successful send.
        if (!['list','get'].includes(method) || attempt===3) throw error;
      }
    }
  }
}
function installGmailQuota(gmail, server) {
  const scope=process.env.GOOGLE_GMAIL_QUOTA_SCOPE || [process.env.GOOGLE_CLIENT_ID,process.env.GOOGLE_REFRESH_TOKEN].join(':');
  const directory=process.env.GOOGLE_GMAIL_QUOTA_DIR || path.join(process.env.MCP_USAGE_LOG_DIR || os.tmpdir(),'gmail-quota');
  const limiter=new GmailQuota({directory,scope,units:Number(process.env.GOOGLE_GMAIL_QUOTA_UNITS_PER_MINUTE || 6000)});
  const register=server.setRequestHandler.bind(server);
  server.setRequestHandler=(schema,handler)=>register(schema,(...args)=>context.run({deadline:Date.now()+55000},()=>handler(...args)));
  for (const method of Object.keys(costs)) {
    const original=gmail.users.messages[method].bind(gmail.users.messages);
    gmail.users.messages[method]=(params,options={})=>limiter.call(method,
      remaining=>original(params,{...options,timeout:remaining,retry:false}),
      context.getStore()?.deadline || Date.now()+55000);
  }
}
module.exports={GmailQuota,minuteQuota,installGmailQuota};
