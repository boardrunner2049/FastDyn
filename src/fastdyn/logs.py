"""Logging setup for the fastdyn command line and other programs built on FastDyn.

Library code only creates loggers (``logging.getLogger(__name__)``); it never
installs handlers. A program's entry point calls :func:`configure` once.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Iterable, Mapping, Optional, TextIO

LEVEL_ENV = "FASTDYN_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
FORMAT = "%(name)s|%(levelname)s|  %(message)s"

_COLORS = {
    "DEBUG": "\033[37m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
}
_RESET = "\033[0m"
_INSTALLED = "_fastdyn_installed"


def level_number(level) -> int:
    """Return the numeric value of a level name or number; raises ValueError if unknown."""
    if isinstance(level, int):
        return level
    value = logging.getLevelName(str(level).strip().upper())
    if not isinstance(value, int):
        raise ValueError(f"unknown log level: {level!r}")
    return value


class _ColorFormatter(logging.Formatter):
    """Colour whole lines by level without modifying the record."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = _COLORS.get(record.levelname)
        return f"{color}{text}{_RESET}" if color else text


class _ThresholdFilter(logging.Filter):
    """Apply the most specific per-logger threshold, else the default one."""

    def __init__(self, default: int, overrides: Mapping[str, int]):
        super().__init__()
        self.default = default
        self.overrides = sorted(overrides.items(), key=lambda item: -len(item[0]))

    def filter(self, record: logging.LogRecord) -> bool:
        for name, threshold in self.overrides:
            if record.name == name or record.name.startswith(name + "."):
                return record.levelno >= threshold
        return record.levelno >= self.default


def configure(
    level=None,
    levels: Optional[Mapping[str, object]] = None,
    log_file: Optional[str] = None,
    namespaces: Iterable[str] = ("fastdyn",),
    stream: Optional[TextIO] = None,
) -> None:
    """Send log messages from ``namespaces`` to ``stream`` (stderr by default).

    ``level`` is the default threshold; when it is None, ``$FASTDYN_LOG_LEVEL``
    or INFO is used. ``levels`` maps logger names (e.g. ``fastdyn.targets``) to
    their own thresholds. ``log_file`` additionally records every message at
    DEBUG, without colour. Calling it again replaces the previous setup.
    """
    default = level_number(level if level is not None else os.environ.get(LEVEL_ENV) or DEFAULT_LEVEL)
    overrides = {name: level_number(value) for name, value in (levels or {}).items()}
    stream = stream if stream is not None else sys.stderr

    console = logging.StreamHandler(stream)
    use_color = hasattr(stream, "isatty") and stream.isatty()
    console.setFormatter(_ColorFormatter(FORMAT) if use_color else logging.Formatter(FORMAT))
    console.addFilter(_ThresholdFilter(default, overrides))
    handlers = [console]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s " + FORMAT))
        handlers.append(file_handler)

    lowest = min([default, *overrides.values()])
    for name in namespaces:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if getattr(handler, _INSTALLED, False):
                logger.removeHandler(handler)
                handler.close()
        for handler in handlers:
            setattr(handler, _INSTALLED, True)
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if log_file else lowest)
