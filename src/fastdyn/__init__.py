# See LICENSE.fastdyn for details

"""Top-level package for Fastdyn Framework."""

__author__ = """Fastdyn Team"""
__email__ = 'fastdyn@pseclab.com'
__version__ = '1.0.0'

import logging

# Library code never configures logging; programs call fastdyn.logs.configure().
logging.getLogger("fastdyn").addHandler(logging.NullHandler())

# Public entry points, imported on first use so `import fastdyn` stays light
# (runner pulls in the FMU and helper-process machinery).
_RUNNER_EXPORTS = ("run_config", "load", "Session", "RunResult", "RunError")

__all__ = list(_RUNNER_EXPORTS)


def __getattr__(name):
    if name in _RUNNER_EXPORTS:
        from . import runner

        return getattr(runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_RUNNER_EXPORTS))
