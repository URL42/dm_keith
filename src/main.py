"""Entrypoint: python -m src.main"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from src.bot.app import build_application
from src.config import get_settings
from src.llm.models import ModelConfigError
from src.log import get_logger, setup_logging
from src.storage.db import DatabaseNotPersistent, DatabaseUnwritable


def main() -> int:
    """Start the bot. Returns a process exit code."""
    try:
        settings = get_settings()
    except (RuntimeError, ValidationError) as exc:
        # A misconfigured .env is the most likely reason this never starts, and a
        # raw pydantic traceback in `docker compose logs` is a miserable way to
        # find that out.
        print("DM Keith can't start — check your .env:\n", file=sys.stderr)
        print(_readable(exc), file=sys.stderr)
        print("\nSee .env.example for the full set of variables.", file=sys.stderr)
        return 1

    setup_logging(settings.log_level)
    log = get_logger(__name__)
    log.info("starting DM Keith")

    try:
        app = build_application(settings)
        app.run_polling()
    except (ModelConfigError, DatabaseNotPersistent, DatabaseUnwritable) as exc:
        # Misconfiguration, not a crash -- report it as such, without a traceback.
        log.error("%s", exc)
        return 1
    return 0


def _readable(exc: Exception) -> str:
    """Turn a pydantic ValidationError into something a human wants to read."""
    if isinstance(exc, ValidationError):
        return "\n".join(
            f"  {'.'.join(str(p) for p in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
    return f"  {exc}"


if __name__ == "__main__":
    sys.exit(main())
