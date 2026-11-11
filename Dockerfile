FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
# deps layer first so code changes don't reinstall packages
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
# config (DATABASE_URL, API keys) comes from runtime env vars, never baked into the image
CMD ["fastapi", "run", "app/main.py", "--port", "8000"]
