"""The integrated QEMU runtime for every B1/B2 run (WP-QINT, ``RUNTIME.md``).

``wt/qemu-int/build/qemu-system-arm`` is the compat + base + capture + profile
patch stack on QEMU 12f5e68807, built with the same configure options as
``src/qemu/build``. Capture and profile are inert unless ``QEMU_SC_CAPTURE`` /
``QEMU_SC_PROFILE`` is set (the run harness scrubs ``QEMU_SC_*``).

:func:`scopt.run.launcher.create_run_dir` (and so :func:`scopt.run.deploy.deploy_new`
and every CLI command that makes a run directory) uses it when no ``qemu`` is
given (exported as ``FASTDYN_QEMU``); ``qemu=launcher.FASTDYN_DEFAULT_QEMU``
opts out and lets FastDyn choose (B0 comparisons only). A base TOML
that pins ``[Machine].qemu_path`` (the FreeRTOS fixture does) overrides
``FASTDYN_QEMU`` and must be rewritten to this path instead.

The B0 binary (``launcher.B0_QEMU``) stays available for B0 comparisons only.
Its caches are not valid for this binary: the ``.sc`` ``runtime_signature``
hashes the executable, so QEMU would rebuild such a cache.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .launcher import FASTDYN_ROOT, file_sha256

__all__ = ["RUNTIME_QEMU", "RUNTIME_QEMU_SHA256", "RuntimeMismatch", "runtime_qemu",
           "runtime_qemu_info", "OVERRIDE_ENV", "OVERRIDE_SHA_ENV"]

#: Environment override of the runtime path (tests, a relocated build). The
#: binary is still checked: against ``SCOPT_RUNTIME_QEMU_SHA256`` when that is
#: set, else against the recorded ``RUNTIME_QEMU_SHA256`` (so a copy of the
#: integrated binary passes and a different build is refused).
OVERRIDE_ENV = "SCOPT_RUNTIME_QEMU"
OVERRIDE_SHA_ENV = "SCOPT_RUNTIME_QEMU_SHA256"
#: the maintainer's WP-QINT build in the workspace layout; absent on other machines
_DEFAULT = FASTDYN_ROOT / "wt/qemu-int/build/qemu-system-arm"

RUNTIME_QEMU = Path(os.environ.get(OVERRIDE_ENV, str(_DEFAULT)))
RUNTIME_QEMU_SHA256 = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"


class RuntimeMismatch(RuntimeError):
    pass


def _norm(h: str) -> str:
    h = h.strip().lower()
    return h if h.startswith("sha256:") else "sha256:" + h


def runtime_qemu_info(*, check: bool = True) -> dict[str, Any]:
    """``{path, sha256, expected_sha256, override, checked}`` of the runtime binary.

    With ``check`` the binary's sha256 must equal the expected one (raises
    :class:`RuntimeMismatch`); a rebuild changes ``runtime_signature`` and so
    invalidates every snapshot. ``SCOPT_RUNTIME_QEMU`` does not switch the
    check off: see :data:`OVERRIDE_ENV`."""
    override = os.environ.get(OVERRIDE_ENV)
    p = Path(override) if override else _DEFAULT
    if not p.is_file():
        raise RuntimeMismatch(f"{p} does not exist (no default runtime QEMU: pass the QEMU "
                              f"explicitly (--qemu), or set {OVERRIDE_ENV} and "
                              f"{OVERRIDE_SHA_ENV})")
    want_env = os.environ.get(OVERRIDE_SHA_ENV)
    want = _norm(want_env) if (override and want_env) else RUNTIME_QEMU_SHA256
    h = file_sha256(p)
    if check and h != want:
        hint = (f"; with {OVERRIDE_ENV} set to another build, also set {OVERRIDE_SHA_ENV}="
                "<its sha256>" if override and not want_env else
                "; update RUNTIME.md and scopt.run.runtime after a rebuild")
        raise RuntimeMismatch(f"{p} sha256 {h} != expected {want}{hint}")
    return {"path": str(p), "sha256": h, "expected_sha256": want,
            "override": override is not None, "checked": check}


def runtime_qemu(*, check: bool = True) -> Path:
    """The integrated binary, checked (see :func:`runtime_qemu_info`)."""
    return Path(runtime_qemu_info(check=check)["path"])
