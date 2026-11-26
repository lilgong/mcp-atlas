import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_environment import ddg_mcp_compat as mod
from duckduckgo_mcp_server import server as upstream

GOOD = '<html><div class="result"><h2 class="result__title"><a class="result__a" href="https://example.com">Example</a></h2><div class="result__snippet">Summary</div></div></html>'
EMPTY = '<html><div class="no-results">No results found for query</div></html>'
CHALLENGE = '<html><form action="//duckduckgo.com/anomaly.js"><div class="anomaly-modal">Select all squares containing a duck</div></form></html>'

ORIGINAL_TOOLS = None


class Context:
    info = AsyncMock()
    error = AsyncMock()

class CompatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global ORIGINAL_TOOLS
        if ORIGINAL_TOOLS is None:
            ORIGINAL_TOOLS = [t.model_dump() for t in await upstream.mcp.list_tools()]
        mod.install()
        self.rate = patch.object(upstream.searcher.rate_limiter,'acquire',new=AsyncMock())
        self.rate.start()
    async def asyncTearDown(self): self.rate.stop()

    async def call(self, html=GOOD, status=200):
        with patch.object(mod,'enabled',return_value=True), patch.object(mod,'fetch_async',new=AsyncMock(return_value=SimpleNamespace(status_code=status,headers={'content-type':'text/html'},body=html.encode()))):
            return await upstream.mcp._tool_manager.get_tool('search').fn('Argentina',Context(),5,'wt-wt')

    async def test_schema_is_identical(self):
        # Installing the adapter changes only fn, not metadata or schema.
        before = ORIGINAL_TOOLS
        mod.install()
        self.assertEqual(before,[t.model_dump() for t in await upstream.mcp.list_tools()])
        self.assertEqual(['search','fetch_content'],[t['name'] for t in before])

    async def test_form_parameters_and_success_format(self):
        fetch=AsyncMock(return_value=SimpleNamespace(status_code=200,headers={},body=GOOD.encode()))
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=fetch):
            result=await upstream.searcher.search('hello world',Context(),max_results=5,region='us-en')
        self.assertEqual(result[0].title,'Example')
        self.assertIn('Found 1 search results',upstream.searcher.format_results_for_llm(result))
        args=fetch.call_args
        self.assertEqual(args.args[0],'POST')
        from urllib.parse import parse_qs
        form=parse_qs(args.kwargs['body'].decode(),keep_blank_values=True)
        self.assertEqual(form['q'],['hello world']);self.assertEqual(form['kl'],['us-en'])
        self.assertEqual(form['kp'],[upstream.searcher.safe_search.value])

    async def test_real_empty_results_stay_empty(self):
        result=await self.call(EMPTY)
        self.assertIn('No results were found',str(result))

    async def test_challenge_202_and_200_raise_at_tool_boundary(self):
        for status in [202,200]:
            with self.assertRaisesRegex(Exception,'DDG_SEARCH_BLOCKED'):
                await self.call(CHALLENGE,status)

    async def test_http_errors_raise(self):
        for status in [429,500,503]:
            with self.assertRaisesRegex(Exception,'DDG_'):
                await self.call('upstream failure',status)

    async def test_malformed_response_raises(self):
        with self.assertRaisesRegex(Exception,'DDG_INVALID_RESPONSE'):
            await self.call('<html>unrecognized</html>')

    async def test_network_error_is_not_swallowed(self):
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=AsyncMock(side_effect=RuntimeError('relay_network_error: disconnected'))):
            with self.assertRaisesRegex(Exception,'relay_network_error'):
                await upstream.mcp._tool_manager.get_tool('search').fn('Argentina',Context())

    async def test_direct_backend_also_validates_final_curl_challenge(self):
        with patch.object(mod,'enabled',return_value=False),patch.object(upstream.DuckDuckGoSearcher,'_request_httpx',new=AsyncMock(return_value=(202,CHALLENGE))),patch.object(upstream.DuckDuckGoSearcher,'_request_curl',new=AsyncMock(return_value=CHALLENGE)):
            with self.assertRaisesRegex(Exception,'DDG_SEARCH_BLOCKED'):
                await upstream.mcp._tool_manager.get_tool('search').fn('Argentina',Context())

    async def test_cancellation_propagates(self):
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await upstream.searcher.search('Argentina',Context())

    async def test_concurrent_failure_does_not_leak_into_success(self):
        async def response(method,url,**kwargs):
            await asyncio.sleep(.001)
            body=CHALLENGE if b'q=blocked' in kwargs['body'] else GOOD
            return SimpleNamespace(status_code=200,headers={},body=body.encode())
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=response):
            results=await asyncio.gather(upstream.searcher.search('blocked',Context()),upstream.searcher.search('ok',Context()),return_exceptions=True)
        self.assertIsInstance(results[0],mod.DuckDuckGoSearchError)
        self.assertEqual(results[1][0].title,'Example')

    async def test_challenge_retry_recovers_once_and_never_replays_success(self):
        responses=[SimpleNamespace(status_code=202,headers={},body=CHALLENGE.encode()),
                   SimpleNamespace(status_code=200,headers={},body=GOOD.encode())]
        fetch=AsyncMock(side_effect=responses)
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=fetch):
            result=await upstream.searcher.search('Argentina',Context())
        self.assertEqual(fetch.await_count,2)
        self.assertEqual(result[0].title,'Example')
        self.assertLessEqual(fetch.call_args_list[1].kwargs['timeout_seconds'],fetch.call_args_list[0].kwargs['timeout_seconds'])

    async def test_retries_are_capped_at_three(self):
        fetch=AsyncMock(return_value=SimpleNamespace(status_code=202,headers={},body=CHALLENGE.encode()))
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=fetch):
            with self.assertRaisesRegex(Exception,'DDG_SEARCH_BLOCKED'):
                await upstream.searcher.search('Argentina',Context())
        self.assertEqual(fetch.await_count,3)

    async def test_429_then_html_challenge_then_success(self):
        fetch=AsyncMock(side_effect=[SimpleNamespace(status_code=429,headers={},body=b'limited'),
            SimpleNamespace(status_code=200,headers={},body=CHALLENGE.encode()),
            SimpleNamespace(status_code=200,headers={},body=GOOD.encode())])
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=fetch):
            self.assertEqual((await upstream.searcher.search('Argentina',Context()))[0].title,'Example')
        self.assertEqual(fetch.await_count,3)

    async def test_nonretryable_errors_and_empty_results_are_not_replayed(self):
        for status,html in [(400,'bad request'),(503,'unavailable'),(200,EMPTY),(200,'broken')]:
            fetch=AsyncMock(return_value=SimpleNamespace(status_code=status,headers={},body=html.encode()))
            with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=fetch):
                try:await upstream.searcher.search('Argentina',Context())
                except mod.DuckDuckGoSearchError:pass
            self.assertEqual(fetch.await_count,1)

    async def test_timeout_stops_instead_of_restarting_budget(self):
        fetch=AsyncMock(side_effect=TimeoutError())
        with patch.object(mod,'enabled',return_value=True),patch.object(mod,'fetch_async',new=fetch):
            with self.assertRaisesRegex(Exception,'DDG_SEARCH_TIMEOUT'):
                await upstream.searcher.search('Argentina',Context())
        self.assertEqual(fetch.await_count,1)

if __name__=='__main__':unittest.main()
