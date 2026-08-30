from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from app.config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class PrivateRotatingFileHandler(RotatingFileHandler):
    def doRollover(self) -> None:
        super().doRollover()
        _restrict_file(Path(self.baseFilename))


def _log_level(value: str) -> int:
    level = getattr(logging, value.strip().upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"지원하지 않는 LOG_LEVEL입니다: {value}")
    return level


def configure_logging(
    settings: Settings,
    *,
    target_logger: logging.Logger | None = None,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Send logs to the console and a size-limited persistent file."""
    settings.ensure_directories()
    logger = target_logger or logging.getLogger()
    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler(stream)
    console_handler.setFormatter(formatter)

    file_handler = PrivateRotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    _restrict_file(settings.log_file)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(_log_level(settings.log_level))
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    # Public-data API keys are query parameters. httpx's INFO request log includes
    # the full URL, so keep dependency request logs below INFO to avoid key leakage.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return logger


def _restrict_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Logging must remain available on filesystems that do not support POSIX modes.
        pass
