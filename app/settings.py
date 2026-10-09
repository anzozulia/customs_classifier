"""Single source of truth for configuration.

The model id lives here and ONLY here, so a banner or log line can never claim one model
while the code constructs another. Everything that reports the model reads it from this
object.
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

    # Send agent traces to the OpenAI Traces dashboard AND let the Responses API retain each
    # request (the Logs page). One switch for both: they are the two halves of "can I see
    # this run on platform.openai.com". Off means nothing about a classification exists on
    # OpenAI's side after the call returns — traces carry customer product descriptions.
    # The app's own /history and the back-office dashboard record every turn regardless of
    # this setting.
    openai_telemetry: bool = True

    # ChatKit
    chatkit_domain_key: str = "domain_pk_localhost_dev"

    # App
    database_url: str = "postgresql://postgres:postgres@localhost:5432/uktzed"
    session_secret: str = ""
    public_base_url: str = "http://localhost:8000"
    log_level: str = "info"

    # Bounded context replay. Replaying the whole transcript lets a long conversation dominate
    # the input token bill and add latency on every turn.
    history_window_items: int = Field(default=40, ge=4, le=400)

    # Hard bounds on a turn: agent loop iterations, wall clock, and repair attempts.
    max_turns: int = 20
    turn_wall_clock_s: float = 180.0
    max_repairs: int = 2

    @property
    def is_production(self) -> bool:
        return self.public_base_url.startswith("https://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
