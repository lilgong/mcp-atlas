const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeLaraErrorResponse: normalize } = require('../src/agent_environment/yibu_fetch_preload.cjs');
for (const body of [{error:{message:'余额不足',type:'quota'}},'{"error":{"message":"余额不足"}}','余额不足',{message:'余额不足'}]) {
  test('preserves upstream quota error: '+JSON.stringify(body),()=>{
    const r=normalize({statusCode:403,headers:{a:1},body});
    assert.equal(r.statusCode,403);assert.match(r.body.message,/HTTP 403: 余额不足/);assert.deepEqual(r.headers,{a:1});
  });
}
test('success remains identical',()=>{const r={statusCode:200,body:{translation:'hola'}};assert.equal(normalize(r),r)});
test('redacts credentials and bounds text',()=>{
 const r=normalize({statusCode:403,body:{error:{message:'secret-value Bearer other-token sk-abcdef https://host/?api_key=abc '+ 'x'.repeat(5000)}}},'secret-value');
 assert(!r.body.message.includes('secret-value'));assert(!r.body.message.includes('other-token'));assert(!r.body.message.includes('sk-abcdef'));assert(!r.body.message.includes('api_key=abc'));assert(r.body.message.length<4200);
});
test('HTML and unknown structures stay explicit',()=>{
 assert.match(normalize({statusCode:502,body:'<html>bad gateway</html>'}).body.message,/HTTP 502.*HTML/);
 assert.match(normalize({statusCode:403,body:{unexpected:1}}).body.message,/HTTP 403.*without a readable/);
});
