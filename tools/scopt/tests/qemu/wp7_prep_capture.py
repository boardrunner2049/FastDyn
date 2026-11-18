#!/usr/bin/env python3
"""One capture-on cold prep run of a FastDyn config, laid out for ``scopt import`` (WP7).

    wp7_prep_capture.py QEMU OUT [--template configs/b0-sc.toml] [--old-root runs/b0]
                        [--timeout 180]

``OUT`` must not exist or be empty. The run directory is ``OUT`` itself::

    OUT/run.toml            the template with every ``--old-root`` path moved into OUT
    OUT/cache/<name>.sc     the cache QEMU wrote (and its .sc.fastdyn.json manifest)
    OUT/capture.scm         QEMU_SC_CAPTURE output
    OUT/work/ ram files     FastDyn work dir and RAM backing files
    OUT/run.log             FastDyn/QEMU output
    OUT/prep.json           exit code, qemu path and sha256, capture banner seen

The run uses ``setarch x86_64 -R`` with a timeout, cwd ``src/fastdyn``,
``FASTDYN_QEMU=QEMU``, every other ``QEMU_SC_*`` variable removed, and
``supercache_flat = false`` (asserted in the rendered TOML).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FASTDYN_SRC = ROOT / "src/fastdyn"
FASTDYN_BIN = FASTDYN_SRC / "fastdyn-env/bin/fastdyn"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("qemu")
    ap.add_argument("out")
    ap.add_argument("--template", default=str(FASTDYN_SRC / "configs/b0-sc.toml"))
    ap.add_argument("--old-root", default=str(ROOT / "runs/b0"))
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args(argv)
    qemu = Path(a.qemu).resolve()
    out = Path(a.out).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to reuse non-empty {out}", file=sys.stderr)
        return 2
    (out / "cache").mkdir(parents=True, exist_ok=True)
    text = Path(a.template).read_text()
    old = a.old_root.rstrip("/")
    if old not in text:
        print(f"{a.template} does not mention {old}", file=sys.stderr)
        return 2
    text = text.replace(old + "/", f"{out}/")
    if "supercache_flat = false" not in text:
        print("template does not pin supercache_flat = false", file=sys.stderr)
        return 2
    toml = out / "run.toml"
    toml.write_text(text)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QEMU_SC_")}
    env["FASTDYN_QEMU"] = str(qemu)
    scm = out / "capture.scm"
    env["QEMU_SC_CAPTURE"] = str(scm)
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10",
           str(a.timeout), str(FASTDYN_BIN), "run", "-c", str(toml), "-o", str(out / "work")]
    p = subprocess.run(cmd, cwd=FASTDYN_SRC, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, errors="replace")
    (out / "run.log").write_text(p.stdout)
    scs = sorted(str(x) for x in (out / "cache").glob("*.sc"))
    rec = {"cmd": cmd, "exit": p.returncode, "qemu": str(qemu), "qemu_sha256": sha256(qemu),
           "capture_banner": "supercache-capture: level=reloc" in p.stdout,
           "scm": str(scm) if scm.exists() else None, "sc": scs,
           "manifest": [s + ".fastdyn.json" for s in scs if Path(s + ".fastdyn.json").exists()],
           "used_qemu_line": [ln for ln in p.stdout.splitlines() if "QEMU executable" in ln]}
    (out / "prep.json").write_text(json.dumps(rec, indent=2) + "\n")
    print(json.dumps(rec, indent=2))
    ok = p.returncode == 0 and rec["capture_banner"] and rec["scm"] and len(scs) == 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
