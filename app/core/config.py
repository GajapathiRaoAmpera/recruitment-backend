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
    judge0_url: str = "http://localhost:2358"  # self-hosted Judge0 (infra/judge0)
    judge0_auth_token: str = ""  # sent as X-Auth-Token; same value the Judge0 stack is started with

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, v: str) -> str:
        # Supabase hands out postgresql:// URIs; the app's driver is asyncpg
        return v.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
            "postgres://", "postgresql+asyncpg://", 1
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
