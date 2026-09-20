import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.run_shared_mcp import redact_line, secret_values


def test_secret_values_include_credentials_but_not_normal_configuration():
    values = secret_values({
        "NOTION_TOKEN": "notion-secret",
        "OXYLABS_PASSWORD": "password-secret",
        "OXYLABS_USERNAME": "account-name",
        "MONGODB_CONNECTION_STRING": "mongodb://user:pass@host/db",
        "MCP_SERVER_URL": "http://127.0.0.1:2987",
    })
    assert "notion-secret" in values
    assert "password-secret" in values
    assert "account-name" in values
    assert "mongodb://user:pass@host/db" in values
    assert "http://127.0.0.1:2987" not in values


def test_redact_line_replaces_exact_secret_values():
    text = "Authorization: Bearer notion-secret; password=password-secret\n"
    assert redact_line(text, ["notion-secret", "password-secret"]) == (
        "Authorization: Bearer <redacted>; password=<redacted>\n"
    )
