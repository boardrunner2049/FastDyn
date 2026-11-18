"""FastDyn shim: the real ``fastdyn`` CLI with QEMU launch stubbed out (§4.8, G14).

Runs inside FastDyn's own interpreter (``src/fastdyn/fastdyn-env/bin/python``),
started as ``python <scopt>/scopt/run/fdshim [shim options] <fastdyn args>``,
with ``cwd=src/fastdyn``. It does not import scopt (that lives in another
virtualenv); it must stay stdlib-only.

What it does:

* replaces ``fastdyn.targets.qemu_target.start_execution`` (called through the
  module attribute, fastdyn.py:66-75) with a stub that records the QEMU argv,
  the environment FastDyn would give QEMU (``_build_qemu_env``) and returns 0;
* wraps ``qemu_target._configure_supercache`` (a module global looked up at
  call time by ``build_qemu_cmd``) to record the fingerprint FastDyn computes
  with its own ``_supercache_fingerprint``;
* then calls ``fastdyn.main.cli()`` with the remaining arguments and catches
  ``SystemExit``.

So the real CLI path runs -- config validation, ``prepare_work_dir``,
preprocessing, ``setup_qemu``, ``build_qemu_cmd`` and ``_configure_supercache``,
which writes ``<cache>.fastdyn.json`` with exactly the fingerprint the real run
will compute (including the ``-q``-dependent ``log_level``). No ``.sc`` exists
yet, so nothing is deleted.

Shim options (must come first):

``--scopt-out PATH``
    where to write the JSON record (argv, env diff, fingerprint, exit code).
``--scopt-fingerprint-only``
    do not call the real ``_configure_supercache``: compute the fingerprint
    and return the same ``-accel`` value without touching the cache directory
    (used by ``deploy --check`` in scratch work directories).

Exit status: FastDyn's exit status; 3 if FastDyn finished without reaching
``start_execution`` (nothing recorded).
"""

import json
import os
import sys
import tempfile


def _write_json(path, value):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fdshim.", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv):
    out = None
    fingerprint_only = False
    args = list(argv)
    while args and args[0].startswith("--scopt-"):
        opt = args.pop(0)
        if opt == "--scopt-out":
            out = args.pop(0)
        elif opt == "--scopt-fingerprint-only":
            fingerprint_only = True
        else:
            sys.stderr.write(f"fdshim: unknown option {opt}\n")
            return 2
    if out is None:
        sys.stderr.write("fdshim: --scopt-out PATH is required\n")
        return 2

    from fastdyn.targets import qemu_target

    environ_before = dict(os.environ)
    record = {
        "shim_version": 1,
        "fastdyn_args": args,
        "cwd": os.getcwd(),
        "fingerprint_only": fingerprint_only,
        "reached_start_execution": False,
        "supercache": None,
    }
    real_configure = qemu_target._configure_supercache

    def configure_supercache(cache_path, *, flat, settings, content_files, native_files):
        fingerprint, inputs = qemu_target._supercache_fingerprint(
            settings=settings, content_files=content_files, native_files=native_files)
        abs_cache = os.path.abspath(os.fspath(cache_path))
        record["supercache"] = {
            "cache_path": abs_cache,
            "manifest_path": abs_cache + ".fastdyn.json",
            "flat": bool(flat),
            "fingerprint": fingerprint,
            "inputs": inputs,
            "cache_existed": os.path.exists(abs_cache),
        }
        if fingerprint_only:
            accel = f"tcg,supercache={abs_cache}"
            if flat:
                accel += ",supercache-flat=on"
            return accel
        return real_configure(cache_path, flat=flat, settings=settings,
                              content_files=content_files, native_files=native_files)

    def start_execution(qemu_cmd, launch_gdb, gdb_cmd, binary, python_endpoints=None,
                        exit_timeout_ms=5000):
        env = qemu_target._build_qemu_env(qemu_cmd)
        record.update({
            "reached_start_execution": True,
            "argv": [str(a) for a in qemu_cmd],
            "env_set": {k: v for k, v in sorted(env.items()) if environ_before.get(k) != v},
            "env_unset": sorted(k for k in environ_before if k not in env),
            "launch_gdb": bool(launch_gdb),
            "gdb_cmd": gdb_cmd,
            "binary": binary,
            "python_endpoints": [list(map(str, e)) for e in (python_endpoints or [])],
            "exit_timeout_ms": exit_timeout_ms,
        })
        return 0

    qemu_target._configure_supercache = configure_supercache
    qemu_target.start_execution = start_execution

    from fastdyn.main import cli

    sys.argv = ["fastdyn", *args]
    code = 0
    try:
        cli()
    except SystemExit as e:
        if e.code is None:
            code = 0
        elif isinstance(e.code, int):
            code = e.code
        else:
            sys.stderr.write(f"{e.code}\n")
            code = 1
    except BaseException as e:  # record what happened, then fail
        record["fastdyn_exit"] = None
        record["error"] = f"{type(e).__name__}: {e}"
        _write_json(out, record)
        raise
    record["fastdyn_exit"] = code
    _write_json(out, record)
    if code == 0 and not record["reached_start_execution"]:
        return 3
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
