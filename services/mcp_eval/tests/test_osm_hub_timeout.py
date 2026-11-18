from agent_environment.mcphub_transport import hub_server_configs

def test_osm_hub_stream_covers_all_nodes_without_extending_other_servers():
    env = {"MCP_TOOL_TRANSPORT": "hub", "MCPHUB_URL": "http://hub:3003",
           "MCPHUB_TOKEN": "test", "MCPHUB_SERVERS": "osm-mcp-server,calculator",
           "TOOL_CALL_TIMEOUT": "180", "MCPHUB_CATALOG": "atlas"}
    result = hub_server_configs(env, {"osm-mcp-server", "calculator"})
    assert result["osm-mcp-server"]["sse_read_timeout"] == 420
    assert result["calculator"]["sse_read_timeout"] == 180
