from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from agent_environment.usage_audit import start_usage, usage_event

SOURCE = Path(__file__).resolve().parents[1] / "src/agent_environment"


def read_records(directory):
    return [
        json.loads(line)
        for path in directory.rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


def test_python_audit_is_durable_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_USAGE_LOG_DIR", str(tmp_path))
    request_id = start_usage("weatherapi", "fake-upstream-secret")
    usage_event(
        "weatherapi", "fake-upstream-secret", "finished", request_id, status=429
    )
    records = read_records(tmp_path)
    assert [r["event"] for r in records] == ["started", "finished"]
    assert records[0]["request_id"] == records[1]["request_id"]
    assert "fake-upstream-secret" not in json.dumps(records)
    assert all(path.stat().st_mode & 0o077 == 0 for path in tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize(
    "service,origin,header",
    [
        (
            "brave",
            "https://api.search.brave.com/res/v1/web/search",
            "x-subscription-token",
        ),
        ("exa", "https://api.exa.ai/search", "x-api-key"),
    ],
)
def test_node_http_is_audited_before_network_and_fails_closed(
    tmp_path, service, origin, header
):
    program = r"""
const fs = require('node:fs');
const path = require('node:path');
const [preload, dir, service, origin, header] = process.argv.slice(1);
process.env.MCP_USAGE_LOG_DIR = dir;
let calls = 0;
globalThis.fetch = async () => {
  calls++;
  const files = fs.readdirSync(path.join(dir, new Date().toISOString().slice(0,7)));
  const records = fs.readFileSync(path.join(dir,new Date().toISOString().slice(0,7),files[0]), 'utf8').trim().split('\n').map(JSON.parse);
  if (records.at(-1).event !== 'started') throw new Error('missing preflight audit');
  return new Response('{}', {status:429});
};
require(preload).install(service);
(async () => {
  await fetch(origin, {headers:{[header]:'fake-upstream-secret'}});
  const blocked = path.join(dir,'blocked'); fs.writeFileSync(blocked,'file');
  process.env.MCP_USAGE_LOG_DIR = blocked;
  let failed = false;
  try { await fetch(origin, {headers:{[header]:'fake-upstream-secret'}}); } catch { failed = true; }
  if (!failed || calls !== 1) throw new Error('audit failure did not block upstream');
})().catch(e => {console.error(e);process.exitCode=1;});
"""
    subprocess.run(
        [
            "node",
            "-e",
            program,
            str(SOURCE / "yibu_fetch_preload.cjs"),
            str(tmp_path),
            service,
            origin,
            header,
        ],
        check=True,
        capture_output=True,
    )
    records = read_records(tmp_path)
    assert [r["event"] for r in records] == ["started", "finished"]
    assert records[-1]["status"] == 429
    assert "fake-upstream-secret" not in json.dumps(records)


def test_weather_http_is_audited_before_network_and_fails_closed(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("MCP_USAGE_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("WEATHER_YIBU_API_KEY", "fake-upstream-secret")
    calls = []

    async def fake_get(self, url, *args, **kwargs):
        assert read_records(tmp_path)[-1]["event"] == "started"
        assert kwargs["headers"]["Authorization"] == "Bearer fake-upstream-secret"
        assert "key" not in kwargs["params"]
        calls.append(str(url))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    spec = importlib.util.spec_from_file_location(
        "weather_audit_test", SOURCE / "weatherapi_preload/sitecustomize.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def run():
        async with httpx.AsyncClient() as client:
            await client.get(
                "https://api.weatherapi.com/v1/current.json",
                params={"key": "fake", "q": "London"},
            )
            blocked = tmp_path / "blocked"
            blocked.write_text("file")
            monkeypatch.setenv("MCP_USAGE_LOG_DIR", str(blocked))
            with pytest.raises(OSError):
                await client.get(
                    "https://api.weatherapi.com/v1/current.json", params={"q": "London"}
                )

    asyncio.run(run())
    assert len(calls) == 1
    assert [r["event"] for r in read_records(tmp_path)] == ["started", "finished"]


def test_lara_send_and_stream_are_audited_before_network(tmp_path):
    program = r"""
const fs = require('node:fs'); const path = require('node:path');
const Module = require('node:module');
const [preload, dir] = process.argv.slice(1);
process.env.MCP_USAGE_LOG_DIR = dir; process.env.LARA_YIBU_API_KEY = 'fake-upstream-secret';
let calls = 0;
function checkStart() {
 calls++;
 const month = path.join(dir,new Date().toISOString().slice(0,7));
 const records=fs.readFileSync(path.join(month,fs.readdirSync(month)[0]),'utf8').trim().split('\n').map(JSON.parse);
 if(records.at(-1).event!=='started') throw new Error('missing start');
}
class LaraClient {}
class NodeLaraClient {
 async send() {checkStart();return {statusCode:200};}
 async *sendAndGetStream() {checkStart();yield {statusCode:200};}
}
const load=Module._load;
Module._load=function(name,...args) {
 if(name==='@translated/lara/package.json')return {version:'1.13.0'};
 if(name.endsWith('/net/lara/client.js'))return {LaraClient};
 if(name.endsWith('/net/lara/node-client.js'))return {NodeLaraClient};
 return load.call(this,name,...args);
};
require(preload).install('lara');
(async()=>{
 const client=new NodeLaraClient();client.baseUrl={hostname:'original'};
 await client.send('POST','/translate',{},{});
 for await(const chunk of client.sendAndGetStream('POST','/translate',{},{})) {}
 const blocked=path.join(dir,'blocked');fs.writeFileSync(blocked,'file');
 process.env.MCP_USAGE_LOG_DIR=blocked;
 let failed=false;try{await client.send('POST','/translate',{},{});}catch{failed=true;}
 if(!failed||calls!==2)throw new Error('audit failure did not block Lara');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run(
        ["node", "-e", program, str(SOURCE / "yibu_fetch_preload.cjs"), str(tmp_path)],
        check=True,
        capture_output=True,
    )
    assert [r["event"] for r in read_records(tmp_path)] == [
        "started",
        "finished",
        "started",
        "finished",
    ]


def test_oxylabs_request_is_audited_and_fails_closed(tmp_path, monkeypatch):
    utils = pytest.importorskip("oxylabs_mcp.utils")
    import httpx

    monkeypatch.setenv("MCP_USAGE_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OXYLABS_PASSWORD", "fake-upstream-secret")
    calls = []

    class Context:
        async def info(self, message):
            pass

    class FakeHTTP:
        async def post(self, url, **kwargs):
            assert read_records(tmp_path)[-1]["event"] == "started"
            calls.append(url)
            return httpx.Response(
                200, request=httpx.Request("POST", url), json={"results": []}
            )

    async def run():
        client = utils._OxylabsClientWrapper(FakeHTTP(), Context())
        await client.scrape({"source": "universal", "url": "https://example.com"})
        blocked = tmp_path / "blocked"
        blocked.write_text("file")
        monkeypatch.setenv("MCP_USAGE_LOG_DIR", str(blocked))
        with pytest.raises(OSError):
            await client.scrape({"source": "universal", "url": "https://example.com"})

    asyncio.run(run())
    assert len(calls) == 1
    assert [r["event"] for r in read_records(tmp_path)] == ["started", "finished"]
