"""Exercise the pinned MCP and SDK without network requests or credentials."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/agent_environment'))
import twelvedata_mcp_compat as compat
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult
from mcp_server_twelve_data.response_models import GetMarketState200Response
from pydantic import BaseModel, ValidationError


class MarketStateCompatibility(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = FuncMetadata.convert_result
        self.server = FastMCP('test')
        self.payload = [{'name': 'NYSE', 'code': 'XNYS', 'is_market_open': True}]

        async def market_state() -> GetMarketState200Response:
            return GetMarketState200Response.model_validate(self.payload)

        self.server.add_tool(market_state, name='GetMarketState')

    def tearDown(self):
        FuncMetadata.convert_result = self.original

    async def test_reproduces_unpatched_failure(self):
        raw = await self.server.call_tool('GetMarketState', {})
        with self.assertRaises(ValidationError):
            CallToolResult(content=raw[0], structuredContent=raw[1])

    async def test_real_protocol_handler_preserves_text_and_schemas(self):
        before = copy.deepcopy(await self.server.list_tools())
        original = await self.server.call_tool('GetMarketState', {})
        compat.install_market_state_patch()
        request = CallToolRequest(method='tools/call', params=CallToolRequestParams(name='GetMarketState', arguments={}))
        result = (await self.server._mcp_server.request_handlers[CallToolRequest](request)).root
        self.assertFalse(result.isError)
        self.assertIsNone(result.structuredContent)
        self.assertEqual(result.content, original[0])
        self.assertEqual(json.loads(result.content[0].text)[0]['code'], 'XNYS')
        self.assertEqual(await self.server.list_tools(), before)

    async def test_empty_list_is_a_successful_result(self):
        self.payload = []
        compat.install_market_state_patch()
        result = await self.server.call_tool('GetMarketState', {})
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(json.loads(result.content[0].text), [])
        self.assertFalse(result.isError)

    async def test_invalid_market_data_still_fails_validation(self):
        self.payload = ['invalid item']
        compat.install_market_state_patch()
        with self.assertRaises(Exception):
            await self.server.call_tool('GetMarketState', {})

    async def test_other_tools_keep_structured_results(self):
        class Quote(BaseModel):
            price: str

        async def quote() -> Quote:
            return Quote(price='100')

        self.server.add_tool(quote, name='GetPrice')
        before = await self.server.call_tool('GetPrice', {})
        compat.install_market_state_patch()
        self.assertEqual(await self.server.call_tool('GetPrice', {}), before)

    def test_install_is_idempotent(self):
        compat.install_market_state_patch()
        installed = FuncMetadata.convert_result
        compat.install_market_state_patch()
        self.assertIs(FuncMetadata.convert_result, installed)


if __name__ == '__main__':
    unittest.main()
