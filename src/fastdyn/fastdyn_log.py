"""Deprecated logging helpers, kept so existing callers keep working.

New code uses ``logging.getLogger(__name__)``; entry points call
``fastdyn.logs.configure()``.
"""

import logging

from . import logs


def getFastdynLogger():
    """Deprecated: returns the ``fastdyn`` logger. Use ``logging.getLogger(__name__)``."""
    return logging.getLogger("fastdyn")


def setLogConfig():
    """Deprecated: same as ``fastdyn.logs.configure()`` with its defaults."""
    logs.configure()
