"""Filtered error-report logger for SentientSands server support.

This keeps a small, player-sendable log of warnings/errors without changing the
normal server.log/debug.log behavior.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

_ROOT_MARKER = "_ss_error_report_handler"
_DEBUG_MARKER = "_ss_debug_error_report_handler"


def _has_marked_handler(logger: logging.Logger, marker: str) -> bool:
    return any(getattr(handler, marker, False) for handler in logger.handlers)


def _make_handler(log_path: str, marker: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        log_path,
        maxBytes=512 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    setattr(handler, marker, True)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    return handler


def setup_server_error_report_logger(log_dir: str, debug_logger: Optional[logging.Logger] = None) -> str:
    """Write WARNING/ERROR lines to server/logs/error_report.log.

    The root logger catches most server warnings/errors. The kenshi_debug logger
    has propagate=False, so it receives a separate handler pointing at the same
    file. This is intentionally low-footprint: it observes existing log calls
    instead of adding new behavior to gameplay/prompt paths.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "error_report.log")

    root_logger = logging.getLogger()
    if not _has_marked_handler(root_logger, _ROOT_MARKER):
        root_logger.addHandler(_make_handler(log_path, _ROOT_MARKER))

    if debug_logger is not None and not _has_marked_handler(debug_logger, _DEBUG_MARKER):
        debug_logger.addHandler(_make_handler(log_path, _DEBUG_MARKER))

    return log_path
