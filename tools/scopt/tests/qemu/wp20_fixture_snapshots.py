#!/usr/bin/env python3
"""WP20 integration: Tier-0 snapshots of the WP24 B3 fixtures (``fixtures/b3``).

    wp20_fixture_snapshots.py --out OUT [--qemu QEMU] [--modes icount,noicount]
                              [--configs 1,1p,...] [--profile] [-j N]

For every (mode, config): render ``b3-<cfg>.toml.in`` with ``fixtures/lib/fdrun.py``'s
``render`` (arm ``sc`` = icount shift 0, ``sc-noicount``), then ``scopt prepare``
(capture-on cold run of the config's first input, further inputs warm with capture
on), ``scopt bundle identity``, ``scopt bundle runtime-identity`` and ``scopt import``
into ``OUT/<mode>/<cfg>/store``. With ``--profile`` a steady-phase profile
(``steady@b3_bench_start``) of the base snapshot is taken too (untimed). Writes
``OUT/<mode>/<cfg>/snap.json`` (snapshot dir, prep dir, profile, return codes) and
``OUT/snapshots.json``.

Never timed: no timing lock, no BENCH comparison. Every QEMU run goes through the
WP8 launcher (``setarch x86_64 -R``, timeout, fresh run dir, ``supercache_flat =
false``).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FX = ROOT / "fixtures" / "b3"
QINT = ROOT / "wt/qemu-int/build/qemu-system-arm"
SCOPT_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(FX))
sys.path.insert(0, str(ROOT / "fixtures" / "lib"))
import b3conf  # noqa: E402
from fdrun import render  # noqa: E402

ARM = {"icount": "sc", "noicount": "sc-noicount"}
DEFAULT_CONFIGS = ["1", "1p", "2", "3", "3b", "4", "5", "6", "8", "20"]


def _scopt(args: list[str], log: Path, timeout: float = 900) -> int:
    t0 = time.time()
    with open(log, "w") as fh:
        fh.write("$ python -m scopt " + " ".join(args) + "\n")
        fh.flush()
        r = subprocess.run([sys.executable, "-m", "scopt", *args], cwd=SCOPT_ROOT,
                           stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        fh.write(f"\nexit={r.returncode} seconds={time.time() - t0:.1f}\n")
    return r.returncode


def one(out: Path, mode: str, name: str, qemu: Path, profile: bool) -> dict:
    cfg = b3conf.BY_NAME[name]
    d = out / mode / name
    d.mkdir(parents=True, exist_ok=False)
    logs = d / "logs"
    logs.mkdir()
    toml, _sc = render(str(FX / f"b3-{name}.toml.in"), ARM[mode], f"b3-{name}.sc", str(qemu), "none")
    base = d / "config.toml"
    base.write_text(toml)
    inputs = [str(FX / "inputs" / f"{i}.bin") for i in cfg.input_names()]
    res: dict = {"mode": mode, "config": name, "fixture": cfg.fixture, "phase": cfg.phase,
                 "elf": cfg.elf, "inputs": inputs, "qemu": str(qemu), "base_toml": str(base)}
    prep = d / "prep"
    rc = _scopt(["prepare", "-c", str(base), "--out", str(prep), "--qemu", str(qemu),
                 "--timeout", "300", "--json", *sum((["--inputs", i] for i in inputs), [])],
                logs / "01-prepare.log")
    res["prepare_rc"] = rc
    if rc != 0:
        return res
    pj = json.loads((prep / "prep.json").read_text())
    res["prep"] = str(prep)
    res["sc"] = pj["sc"]
    res["records"] = [r.get("records_after") for r in pj["runs"]]
    res["prep_verdict"] = pj["verdict"]
    manifest = pj["manifest"]
    rc = _scopt(["bundle", "identity", "-c", str(prep / "run.toml"), "--manifest", manifest,
                 "--out", str(d / "bundle.json")], logs / "02-bundle.log")
    res["bundle_rc"] = rc
    rc2 = _scopt(["bundle", "runtime-identity", "--run-dir", str(prep / "oracle"), "--sc", pj["sc"],
                  "--scm", str(prep / "capture.scm"), "--out", str(d / "runtime_identity.json")],
                 logs / "03-runtime-identity.log")
    res["runtime_identity_rc"] = rc2
    if rc or rc2:
        return res
    store = d / "store"
    rc = _scopt(["import", str(prep), "--bundle", str(d / "bundle.json"), "--runtime-identity",
                 str(d / "runtime_identity.json"), "--store", str(store)], logs / "04-import.log")
    res["import_rc"] = rc
    snaps = sorted((store / "snapshots").iterdir()) if (store / "snapshots").is_dir() else []
    if rc != 0 or len(snaps) != 1:
        res["import_error"] = f"rc={rc} snapshots={len(snaps)}"
        return res
    res["snapshot"] = str(snaps[0])
    if profile:
        start = b3conf.resolve(cfg.elf, "b3_bench_start")
        prof = d / "prof"
        rc = _scopt(["profile", "run", "-c", str(base), "--snapshot", str(snaps[0]), "--reps", "1",
                     "--out", str(prof), "--qemu", str(qemu), "--timeout", "300",
                     "--phase", f"steady@{start:#x}", *sum((["--inputs", i] for i in inputs), [])],
                    logs / "05-profile.log")
        res["profile_rc"] = rc
        pf = prof / "merged.profile.json"
        if rc == 0 and pf.is_file():
            res["profile"] = str(pf)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--qemu", default=str(QINT), type=Path)
    ap.add_argument("--modes", default="icount,noicount")
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                    help="comma list of fixtures/b3 config names, or 'all'")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    names = [c.name for c in b3conf.CONFIGS] if a.configs == "all" else a.configs.split(",")
    a.out.mkdir(parents=True, exist_ok=True)
    jobs = [(m, n) for m in a.modes.split(",") for n in names]
    results = []
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(one, a.out, m, n, a.qemu, a.profile): (m, n) for m, n in jobs}
        for f in cf.as_completed(futs):
            m, n = futs[f]
            try:
                r = f.result()
            except Exception as e:  # recorded, not fatal for the other configs
                r = {"mode": m, "config": n, "error": f"{type(e).__name__}: {e}"}
            (a.out / m / n).mkdir(parents=True, exist_ok=True)
            (a.out / m / n / "snap.json").write_text(json.dumps(r, indent=1, sort_keys=True))
            print(f"{m:8s} {n:10s} snapshot={'yes' if r.get('snapshot') else 'NO'} "
                  f"profile={'yes' if r.get('profile') else '-'}", flush=True)
            results.append(r)
    results.sort(key=lambda r: (r["mode"], r["config"]))
    (a.out / "snapshots.json").write_text(json.dumps(results, indent=1, sort_keys=True))
    bad = [r for r in results if not r.get("snapshot")]
    print(f"{len(results) - len(bad)}/{len(results)} snapshots")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
