"""``scopt qualify-capture``: orchestrate T-B1-1 (§2.6, §6) for one (QEMU, plugin) build.

All runs use **one** run directory (same ``run.toml``, so the same FastDyn
fingerprint and the same paths), recreated before each run: cache and work
directories emptied, RAM files reset.

* T-B1-1a: ``off`` capture-off cold runs (default 3);
* T-B1-1b: ``on`` capture-on cold runs (default 2) with
  ``QEMU_SC_CAPTURE=<out>/scm/onN.scm`` -- outside the run directory, so the
  capture file can never be confused with run outputs.

Each run's ``.sc`` is archived sparsely under ``<out>/archive/`` and hashed
over its full logical size (holes as zeros, like ``sha256sum``).

Gate:

* if every 1a run has the same sha256 (deterministic), every 1b run must have
  that sha256 too: ``PASS`` (``mode="whole_file"``) or ``FAIL``;
* otherwise (documented guest-timing nondeterminism) the weaker per-key
  comparison of :mod:`.identity_cmp` runs for every (on, off) pair, using the
  on run's ``.scm`` site list: ``PASS`` with ``mode="per_key"`` (reported as
  the weaker mode) or ``FAIL``.

Also checked per run: exit code 0, guest-visible output equal across runs,
the capture banner (``supercache-capture: level=reloc``) present exactly in
the on runs, the ``.scm`` exists for on runs, and no ``*.scm``/``*.qprof``
inside the run directory. In both modes (§2.6): every run's ``.sc`` header has
the same ``code_address``; each on run's RUN_INFO ``code_address``,
``code_size``, ``prologue_end`` and ``runtime_signature`` equal its ``.sc``
header; the MAPS(first_commit) layouts of the on runs are equal and do not map
the ``.scm``. Capture-off runs write no MAPS, so the on/off half of the MAPS
diff is covered by the ``.sc`` comparison itself (every embedded host address
is in the compared bytes). ``qualification.json`` records the QEMU and plugin
sha256 the result applies to.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
from pathlib import Path
from typing import Any, Mapping

from ..formats.sc_records import read_sc, sha256_sparse, sparse_copy
from .identity_cmp import per_key_compare, scm_checks, unit_images
from .launcher import FASTDYN_SRC, create_run_dir, file_sha256, launch, write_json

__all__ = ["qualify_capture"]

QUAL_SCHEMA = "scopt.qualification/1"
BANNER = "supercache-capture: level=reloc"


def _plugin_path(rd) -> str | None:
    cpu = (rd.config().get("CPU") or {})
    for v in cpu.values():
        for t in (v if isinstance(v, list) else [v]):
            if isinstance(t, dict) and t.get("plugin_library"):
                p = Path(t["plugin_library"])
                return str(p if p.is_absolute() else (FASTDYN_SRC / p).resolve())
    return None


def qualify_capture(base_toml: str | os.PathLike, out: str | os.PathLike, *, off: int = 3,
                    on: int = 2, qemu: str | os.PathLike | None = None, quiet: bool = False,
                    timeout_s: float = 120.0, inputs: Mapping[str, str | os.PathLike] | None = None
                    ) -> dict[str, Any]:
    """T-B1-1a/1b. ``inputs`` (RAM bank id -> file) is written into those banks
    before every run, so a fixture with an input bank runs its real workload."""
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; use a fresh output directory")
    rd = create_run_dir(base_toml, out / "rd", quiet=quiet, qemu=qemu, label="qualify-capture")
    if rd.cache is None:
        raise ValueError("the base TOML has no [Machine].supercache")
    (out / "archive").mkdir(parents=True)
    (out / "scm").mkdir()
    plan = [f"off{i + 1}" for i in range(off)] + [f"on{i + 1}" for i in range(on)]
    runs = []
    for name in plan:
        for d in (rd.cache_dir, rd.work):
            if d.exists():
                shutil.rmtree(d)
        rd.cache_dir.mkdir()
        extra = {}
        scm = None
        if name.startswith("on"):
            scm = out / "scm" / f"{name}.scm"
            extra["QEMU_SC_CAPTURE"] = str(scm)
        r = launch(rd, timeout_s=timeout_s, extra_env=extra, prove=False,
                   run_json=f"{name}.run.json", inputs=inputs)
        text = Path(r["stdout_log"]).read_text(errors="replace")
        row: dict[str, Any] = {"name": name, "exit_code": r["exit_code"],
                               "guest_norm": r["guest_norm"],
                               "capture_banner": BANNER in text,
                               "stray_capture_files": r["stray_capture_files"],
                               "qemu": r["qemu"], "qemu_sha256": r["qemu_sha256"]}
        if rd.cache.is_file():
            arch = out / "archive" / f"{name}.sc"
            sparse_copy(rd.cache, arch)
            row["sc_sha256"] = sha256_sparse(arch)
            with open(arch, "rb") as f:
                row["record_count"] = struct.unpack_from("<Q", f.read(80), 40)[0]
        if scm is not None:
            row["scm"] = str(scm)
            row["scm_exists"] = scm.is_file()
            row["scm_size"] = scm.stat().st_size if scm.is_file() else None
        runs.append(row)

    fails = []
    for r in runs:
        if r["exit_code"] != 0:
            fails.append(f"{r['name']}: exit {r['exit_code']}")
        if "sc_sha256" not in r:
            fails.append(f"{r['name']}: no .sc")
        if r["stray_capture_files"]:
            fails.append(f"{r['name']}: stray capture files in the run dir")
        if r["name"].startswith("on") and not (r.get("scm_exists") and r["capture_banner"]):
            fails.append(f"{r['name']}: capture did not run")
        if r["name"].startswith("off") and r["capture_banner"]:
            fails.append(f"{r['name']}: capture banner in a capture-off run")
        if r["guest_norm"] != runs[0]["guest_norm"]:
            fails.append(f"{r['name']}: guest-visible output differs from {runs[0]['name']}")
    # §2.6 checks that hold in both modes: code_address, RUN_INFO, MAPS
    from ..formats.scm_v1 import ScmError, read_scm

    scs = {r["name"]: read_sc(out / "archive" / f"{r['name']}.sc")
           for r in runs if "sc_sha256" in r}
    cas = {n: sc.header.code_address for n, sc in scs.items()}
    if len(set(cas.values())) > 1:
        fails.append(f"code_address differs between runs: "
                     f"{ {n: hex(v) for n, v in cas.items()} }")
    scms: dict[str, Any] = {}
    layouts: dict[str, Any] = {}
    for r in runs:
        n = r["name"]
        if not (r.get("scm_exists") and n in scs):
            continue
        try:
            scms[n] = read_scm(r["scm"])
        except (ScmError, OSError) as e:
            fails.append(f"{n}: .scm unreadable: {e}")
            continue
        chk = scm_checks(scms[n], scs[n])
        layouts[n] = chk.pop("maps_layout")
        r["scm_checks"] = chk
        fails += [f"{n}: {f}" for f in chk["failures"]]
    if len({v for v in layouts.values() if v is not None}) > 1:
        fails.append("MAPS(first_commit) layout differs between capture-on runs")

    off_sha = {r.get("sc_sha256") for r in runs if r["name"].startswith("off")}
    on_sha = {r.get("sc_sha256") for r in runs if r["name"].startswith("on")}
    if len(off_sha) == 1 and None not in off_sha:
        mode = "whole_file"
        if on_sha != off_sha:
            fails.append(f"capture-on .sc sha256 {sorted(map(str, on_sha))} != capture-off "
                         f"{sorted(off_sha)}")
    else:
        mode = "per_key"
    # The per-key comparison is the gate in per_key mode. It is computed in
    # whole_file mode too (milliseconds; equal files must compare equal), so
    # the code path is exercised on every qualification.
    per_key: dict[str, Any] = {}
    imgs = {n: unit_images(sc) for n, sc in scs.items()}
    for n in (r["name"] for r in runs if r["name"].startswith("on")):
        if n not in imgs or n not in scms:
            fails.append(f"{n}: no .sc/.scm pair for the per-key comparison")
            continue
        for o in (r["name"] for r in runs if r["name"].startswith("off")):
            if o not in imgs:
                continue
            c = per_key_compare(imgs[n], imgs[o], scms[n].tbs)
            per_key[f"{n}~{o}"] = c
            if not c["ok"]:
                fails.append(f"{n} vs {o}: {c['mismatches']} mismatches over "
                             f"{c['common_units']} common units")
    verdict = "FAIL" if fails else "PASS"
    qemu_used = {r["qemu"] for r in runs}
    plugin = _plugin_path(rd)
    doc = {
        "schema": QUAL_SCHEMA,
        "base_toml": str(Path(base_toml).resolve()),
        "run_dir": str(rd.root),
        "qemu": sorted(map(str, qemu_used)),
        "qemu_sha256": sorted({str(r["qemu_sha256"]) for r in runs}),
        "plugin": plugin,
        "plugin_sha256": file_sha256(plugin) if plugin and os.path.isfile(plugin) else None,
        "runs": runs,
        "inputs": {k: str(Path(v).resolve()) for k, v in (inputs or {}).items()},
        "deterministic_off": mode == "whole_file",
        "mode": mode,
        "weaker_mode": mode == "per_key",
        "code_address": sorted({hex(v) for v in cas.values()}),
        "maps_first_commit_equal_across_on_runs": len(set(layouts.values())) <= 1,
        "per_key": per_key,
        "verdict": verdict,
        "failures": fails,
    }
    write_json(out / "qualification.json", doc)
    return doc


def load(path: str | os.PathLike) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
