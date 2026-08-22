# Build stage: resolve dependencies into a self-contained virtualenv.
# Deps come from uv.lock, so the image can never drift from pyproject.toml the way
# the old hand-written `pip install` layer did.
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer -- cached until pyproject/uv.lock actually change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime stage.
FROM python:3.11-slim

WORKDIR /app
# /data is where compose mounts the host directory holding the campaign database.
# It's a fixed path on purpose: the container never needs to know where that
# directory lives on the host.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DMK_DB_PATH=/data/dmk.sqlite3

COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY prompts ./prompts
COPY assets ./assets

# The bot holds the Telegram token and the campaign database; it has no business
# running as root.
RUN useradd --create-home --uid 1000 keith \
    && mkdir -p /data \
    && chown -R keith:keith /app /data
USER keith

CMD ["python", "-m", "src.main"]
