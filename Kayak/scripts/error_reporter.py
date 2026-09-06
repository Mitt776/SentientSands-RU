# License: GNU General Public License version 3; see LICENSE.
# Pineaxe-authored portions of this file are subject to the attribution terms
# in Kayak/ADDITIONAL_TERMS.md. See CREDITS.md for project authorship.

"""Small filtered error-report logger for Kayak support."""
from __future__ import annotations
import logging, os
from logging.handlers import RotatingFileHandler
from typing import Any
_REPORT_LOGGER_NAME = "kayak.error_report"
_HANDLER_MARKER = "_kayak_error_report_handler"
def setup_error_report_logger(kayak_root: str) -> str:
    log_dir = os.path.join(kayak_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "error_report.log")
    root_logger = logging.getLogger("kayak")
    for handler in root_logger.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return log_path
    handler = RotatingFileHandler(log_path, maxBytes=512*1024, backupCount=3, encoding="utf-8")
    setattr(handler, _HANDLER_MARKER, True)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root_logger.addHandler(handler)
    root_logger.setLevel(min(root_logger.level or logging.INFO, logging.INFO))
    return log_path
def _format_context(context: dict[str, Any]) -> str:
    parts=[]
    exc=context.pop("exception", None)
    for key in sorted(context):
        value=context[key]
        if value is None or value == "":
            continue
        normalized_value = str(value).replace(chr(10), "\\n")
        parts.append(f"{key}={normalized_value}")
    if exc is not None:
        parts.append(f"exception={type(exc).__name__}: {exc}")
    return " | ".join(parts)
def report_event(level:int, subsystem:str, message:str, **context:Any)->None:
    try:
        logger=logging.getLogger(_REPORT_LOGGER_NAME)
        ctx=_format_context(dict(context))
        text=f"{subsystem}: {message}" if subsystem else message
        if ctx:
            text=f"{text} | {ctx}"
        logger.log(level, text)
    except Exception:
        pass
def report_warning(subsystem:str, message:str, **context:Any)->None:
    report_event(logging.WARNING, subsystem, message, **context)
def report_error(subsystem:str, message:str, **context:Any)->None:
    report_event(logging.ERROR, subsystem, message, **context)
