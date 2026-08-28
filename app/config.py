from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "multi-agent-customer-support"
    database_url: str = "sqlite:///./support.db"
    ingestion_api_key: str = "dev-ingestion-key"
    model_name: str = "gpt-4.1-mini"
    triage_confidence_floor: float = Field(default=0.7, ge=0.0, le=1.0)
    refund_approval_threshold: float = Field(default=50.0, ge=0.0)
    max_tool_calls: int = Field(default=12, ge=1)
    max_agent_turns: int = Field(default=8, ge=1)
    max_execution_seconds: float = Field(default=10.0, gt=0)
    max_tool_retries: int = Field(default=1, ge=0)

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
