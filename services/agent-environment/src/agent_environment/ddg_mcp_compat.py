"""Keep DuckDuckGo tools intact while routing searches and exposing transport failures."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time
import urllib.parse

import httpx
from bs4 import BeautifulSoup

try:
    from .egress_relay_client import enabled, fetch_async
except ImportError:
    from egress_relay_client import enabled, fetch_async


_failure = ContextVar('ddg_search_failure', default=None)


class DuckDuckGoSearchError(RuntimeError):
    def __init__(self, message, *, retryable=False):
        self.retryable = retryable
        super().__init__(message)


def validate_search_html(html, status=200):
    sample = html.lower()
    if status in {202, 403, 429} or any(marker in sample for marker in (
        'anomaly-modal', 'anomaly.js', 'bots use duckduckgo too',
        'select all squares containing', 'cf-mitigated', 'checking your browser',
    )):
        raise DuckDuckGoSearchError(
            f'DDG_SEARCH_BLOCKED: DuckDuckGo returned a challenge or rate limit (HTTP {status}); '
            'no search results were obtained. Try again later.', retryable=True
        )
    if status != 200:
        raise DuckDuckGoSearchError(f'DDG_UPSTREAM_HTTP_ERROR: DuckDuckGo returned HTTP {status}')
    soup = BeautifulSoup(html, 'html.parser')
    if not soup.select_one('.result__a, .no-results, .result--no-result'):
        raise DuckDuckGoSearchError('DDG_INVALID_RESPONSE: upstream did not return a recognizable search results page')
    return html


def install():
    from duckduckgo_mcp_server import server as upstream
    DuckDuckGoSearcher = upstream.DuckDuckGoSearcher
    if getattr(DuckDuckGoSearcher.search, '_relay_error_compat', False):
        return
    original_request = DuckDuckGoSearcher._request
    original_search = DuckDuckGoSearcher.search

    async def request(self, data, ctx):
        try:
            if enabled():
                # Preserve the original search read budget across all attempts.
                deadline = time.monotonic() + 30
                try:
                    async with asyncio.timeout(30):
                        for attempt in range(3):
                            response = await fetch_async(
                                'POST', self.BASE_URL,
                                body=urllib.parse.urlencode(data).encode(),
                                headers={**self.HEADERS, 'Content-Type': 'application/x-www-form-urlencoded'},
                                allow_redirects=True, timeout_seconds=max(.001, deadline - time.monotonic()),
                            )
                            decoded = httpx.Response(response.status_code, headers=response.headers, content=response.body)
                            try:
                                return validate_search_html(decoded.text, response.status_code)
                            except DuckDuckGoSearchError as error:
                                if not error.retryable or attempt == 2:
                                    raise
                                await ctx.info(f'DuckDuckGo challenge/rate limit; retrying through Relay ({attempt + 2}/3)')
                except TimeoutError:
                    raise DuckDuckGoSearchError('DDG_SEARCH_TIMEOUT: search exceeded its total 30-second request budget') from None
            return validate_search_html(await original_request(self, data, ctx))
        except Exception as error:
            state = _failure.get()
            if state is not None:
                state['error'] = error
            raise

    async def search(self, query, ctx, max_results=10, region=''):
        state = {}
        token = _failure.set(state)
        try:
            result = await original_search(self, query, ctx, max_results=max_results, region=region)
            # Upstream catches HTTP/parser exceptions and returns []; restore the
            # actual failure at the tool boundary without changing real empty results.
            if 'error' in state:
                raise state['error']
            return result
        finally:
            _failure.reset(token)

    search._relay_error_compat = True
    DuckDuckGoSearcher._request = request
    DuckDuckGoSearcher.search = search
    # Replace only the callable: keep the registered name, schema, description,
    # argument validation and formatting. FastMCP can then mark failures isError.
    async def tool_search(query: str, ctx, max_results: int = 10, region: str = ''):
        results = await upstream.searcher.search(query, ctx, max_results, region)
        return upstream.searcher.format_results_for_llm(results)
    upstream.mcp._tool_manager.get_tool('search').fn = tool_search


def main():
    install()
    from duckduckgo_mcp_server.server import main as upstream_main
    upstream_main()


if __name__ == '__main__':
    main()
