import asyncio

from mcp_completion.main import classify_tools


def test_classify_tools_exposes_fail_closed_cloud_policy():
    result = asyncio.run(classify_tools({"tools": [
        "notion_API-post-search",
        "notion_API-patch-page",
        "filesystem_read_file",
        "filesystem_write_file",
        "mongodb_drop-database",
    ]}))
    by_name = {item["name"]: item for item in result["tools"]}
    assert by_name["notion_API-post-search"]["read_only"] is True
    assert by_name["notion_API-post-search"]["server"] == "notion"
    assert by_name["notion_API-patch-page"]["blocked"] is True
    assert by_name["filesystem_read_file"]["route"] == "task_local"
    assert by_name["filesystem_read_file"]["generation_allowed"] is True
    assert by_name["filesystem_write_file"]["blocked"] is False
    assert by_name["filesystem_write_file"]["generation_allowed"] is False
    assert by_name["filesystem_write_file"]["effect"] == "local_mutation"
    assert by_name["mongodb_drop-database"]["coverage_role"] == "excluded"
