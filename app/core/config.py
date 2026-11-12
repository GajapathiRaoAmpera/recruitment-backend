from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # dev | test | prod
    database_url: str  # postgresql+asyncpg://user:pass@host:5432/db
    supabase_url: str  # https://<project-ref>.supabase.co (not secret)
    supabase_secret_key: str  # sb_secret_... — server only, never sent to the browser
    frontend_url: str = "http://localhost:3000"  # invite emails link back here

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, v: str) -> str:
        # Supabase hands out postgresql:// URIs; the app's driver is asyncpg
        return v.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
            "postgres://", "postgresql+asyncpg://", 1
        )

    @field_validator("gemini_api_key", "groq_api_key")
    @classmethod
    def _blank_is_missing(cls, v: str | None) -> str | None:
        # `GEMINI_API_KEY=` in .env means "not configured", not an empty key
        return v.strip() or None if v is not None else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
