"""Single source of truth for configuration.

The model id lives here and ONLY here. v1 hardcoded a banner that claimed `o3` + `gpt-5`
while the code constructed `o4-mini` + `gpt-4.1`, and the logs lied for two months.
Everything that reports the model reads it from this object.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenAI
    openai_api_key: str = ""
    openai_org_id: str | None = None
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "low"

    # ChatKit
    chatkit_domain_key: str = "domain_pk_localhost_dev"

    # App
    database_url: str = "postgresql://postgres:postgres@localhost:5432/uktzed"
    session_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    log_level: str = "info"

    # Bounded context replay. v1 replayed the whole transcript: max 796 items,
    # 68% of the input token bill, and a 2.6x latency tax at 750 items.
    history_window_items: int = Field(default=40, ge=4, le=400)

    # Bounds v1 never had. max_turns was its ONLY bound and never bound (max observed 11).
    max_turns: int = 20
    turn_wall_clock_s: float = 180.0
    max_repairs: int = 2

    @property
    def is_production(self) -> bool:
        return self.public_base_url.startswith("https://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
