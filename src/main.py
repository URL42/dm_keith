"""Entrypoint: python -m src.main"""

from __future__ import annotations

from src.bot.app import build_application
from src.config import get_settings
from src.log import get_logger, setup_logging


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger(__name__)
    log.info("starting DM Keith")

    app = build_application(settings)
    app.run_polling()


if __name__ == "__main__":
    main()
