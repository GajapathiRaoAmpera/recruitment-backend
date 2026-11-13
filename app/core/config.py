import hmac
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # dev | test | prod
    database_url: str  # postgresql+asyncpg://user:pass@host:5432/db
    supabase_url: str  # https://<project-ref>.supabase.co (not secret)
    supabase_secret_key: str  # sb_secret_... — server only, never sent to the browser
    student_token_secret: str = ""  # optional override; default is derived from supabase_secret_key
    resume_dir: str = (
        "storage/resumes"  # resume PDFs are saved here (relative to where you start the app)
    )
    frontend_url: str = "http://localhost:3000"  # invite emails link back here
    judge0_url: str = "http://localhost:2358"  # self-hosted Judge0 (infra/judge0)
    judge0_auth_token: str = ""  # sent as X-Auth-Token; same value the Judge0 stack is started with
    # Round 1 paper builder LLMs. Keys are server only; missing => AI_PROVIDER_NOT_CONFIGURED
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"  # generator (ai.google.dev/gemini-api/docs/models)
    groq_model: str = "openai/gpt-oss-120b"  # blind verifier (console.groq.com/docs/models)
    # Round 1 paper builder LLMs. The key is server only; missing => AI_PROVIDER_NOT_CONFIGURED
    # One NVIDIA key (build.nvidia.com); model ids are listed at
    # integrate.api.nvidia.com/v1/models
    nvidia_api_key: str | None = None
    # writes the Technical questions and composes the sample. Answers on a free key (many listed
    # models return 404 or time out, e.g. meta/llama-3.2-90b-vision-instruct). Faster, smaller
    # alternative: nvidia/nemotron-3.5-super-vl-120b-a12b
    nvidia_generator_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    nvidia_rpm: int = 30  # requests per minute sent to NVIDIA (the free tier allows about 40)

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, v: str) -> str:
        # Supabase hands out postgresql:// URIs; the app's driver is asyncpg
        return v.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
            "postgres://", "postgresql+asyncpg://", 1
        )

    @property
    def entry_signing_key(self) -> str:
        # derived (HMAC, domain-separated) so students need no extra setting
        if self.student_token_secret:
            return self.student_token_secret
        return hmac.new(
            self.supabase_secret_key.encode(), b"student-entry-token", "sha256"
        ).hexdigest()

    @field_validator("nvidia_api_key")
    @classmethod
    def _blank_is_missing(cls, v: str | None) -> str | None:
        # `NVIDIA_API_KEY=` in .env means "not configured", not an empty key
        return v.strip() or None if v is not None else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
