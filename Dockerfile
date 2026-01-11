FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DMK_DB_PATH=/app/local/dev.sqlite3 \
    DMK_DEFAULT_MODE=narrator \
    DMK_PROFANITY_LEVEL=3 \
    DMK_RATING=PG-13 \
    DMK_MODEL=gpt-4o \
    DMK_TANGENTS_LEVEL=1 \
    DMK_ACHIEVEMENT_DENSITY=normal

WORKDIR /app

# Install runtime dependencies (mirrors pyproject runtime deps).
RUN pip install --upgrade --no-cache-dir pip && \
    pip install --no-cache-dir \
      "openai>=1.40.0" \
      "python-telegram-bot>=21.0" \
      "python-dotenv>=1.0" \
      "pydantic>=2.7" \
      "tenacity>=8.2" \
      "typing-extensions>=4.9"

# Copy source last to maximize cache hits on dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src
COPY assets ./assets
COPY prompts ./prompts
COPY characters ./characters
COPY docs ./docs
COPY tools ./tools

# Ensure local storage directory exists for SQLite WAL.
RUN mkdir -p /app/local

ENTRYPOINT ["python", "-m", "src.bots.telegram_bot"]
