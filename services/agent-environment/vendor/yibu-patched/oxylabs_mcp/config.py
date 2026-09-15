import os

from dotenv import load_dotenv
from pydantic_settings import BaseSettings


load_dotenv()


class Settings(BaseSettings):
    """Project settings."""

    OXYLABS_REQUEST_TIMEOUT_S: int = 100
    LOG_LEVEL: str = "INFO"

    @property
    def OXYLABS_SCRAPER_URL(self) -> str:
        base_url = os.getenv("MCP_TOOL_BASE_URL") or "https://yibuapi.com"
        return f"{base_url.rstrip('/')}/oxylabs/v1/queries"


settings = Settings()
