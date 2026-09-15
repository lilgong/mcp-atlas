import os

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings


load_dotenv()


class Settings(BaseSettings):
    """Project settings."""

    OXYLABS_SCRAPER_URL: str = (
        f"{(os.getenv('MCP_TOOL_BASE_URL') or 'https://yibuapi.com').rstrip('/')}"
        "/oxylabs/v1/queries"
    )
    OXYLABS_REQUEST_TIMEOUT_S: int = 100
    LOG_LEVEL: str = "INFO"

    @field_validator("OXYLABS_SCRAPER_URL", mode="before")
    @classmethod
    def default_scraper_url(cls, value: str | None) -> str:
        if value and value.strip():
            return value
        base_url = os.getenv("MCP_TOOL_BASE_URL") or "https://yibuapi.com"
        return f"{base_url.rstrip('/')}/oxylabs/v1/queries"


settings = Settings()
