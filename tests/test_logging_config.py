from __future__ import annotations

import io
import logging

from app.config import Settings
from app.logging_config import configure_logging


def test_logging_writes_to_console_and_rotating_persistent_files(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        log_max_bytes=1024,
        log_backup_count=2,
    )
    stream = io.StringIO()
    logger = logging.getLogger("calorie-bot-test-logging")
    logger.propagate = False

    configure_logging(settings, target_logger=logger, stream=stream)
    for number in range(100):
        logger.info("persistent log entry %s with enough text to rotate", number)

    for handler in logger.handlers:
        handler.flush()

    files = sorted(settings.logs_dir.glob("calorie-bot.log*"))
    assert settings.log_file.exists()
    assert len(files) == 3
    assert "persistent log entry 99" in stream.getvalue()
    assert settings.log_file.stat().st_mode & 0o777 == 0o600

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
