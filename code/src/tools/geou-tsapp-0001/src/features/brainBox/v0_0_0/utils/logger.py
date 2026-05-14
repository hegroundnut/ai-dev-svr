"""日志工具."""

import logging
import logging.handlers
from pathlib import Path

from config.settings import get_settings


def setup_logging(name: str = "brainBox") -> logging.Logger:
    """
    配置日志系统。

    参数:
        name: 日志记录器名称

    返回:
        配置好的日志记录器
    """
    settings = get_settings()
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, settings.logging.level))

    log_dir = Path(settings.logging.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    if settings.logging.file_enabled:
        log_file = log_dir / settings.logging.log_file
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=settings.logging.max_bytes,
            backupCount=settings.logging.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(getattr(logging, settings.logging.level))
        formatter = logging.Formatter(settings.logging.format)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    if settings.logging.console_enabled:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, settings.logging.level))
        formatter = logging.Formatter(settings.logging.format)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger
