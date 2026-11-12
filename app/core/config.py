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
    # question-bank PDFs are saved here (same local-disk shortcut as resume_dir)
    qbank_dir: str = "storage/qbank"
    qbank_max_mb: int = 20  # upload size limit for a question-bank PDF
    embedding_dim: int = 768  # must match question_embeddings.embedding vector(768) (0003)
    frontend_url: str = "http://localhost:3000"  # invite emails link back here
    # Round 1 paper builder LLMs. Keys are server only; missing => AI_PROVIDER_NOT_CONFIGURED
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"  # generator (ai.google.dev/gemini-api/docs/models)
    groq_model: str = "openai/gpt-oss-120b"  # blind verifier (console.groq.com/docs/models)
    # question-bank RAG embeddings (ai.google.dev/gemini-api/docs/embeddings); requested at
    # embedding_dim dimensions. Changing the model needs a re-embed of every bank.
    gemini_embedding_model: str = "gemini-embedding-2"

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

    @field_validator("gemini_api_key", "groq_api_key")
    @classmethod
    def _blank_is_missing(cls, v: str | None) -> str | None:
        # `GEMINI_API_KEY=` in .env means "not configured", not an empty key
        return v.strip() or None if v is not None else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
