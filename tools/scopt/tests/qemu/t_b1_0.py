#!/usr/bin/env python3
"""T-B1-0 (design §6, WP-QINT): B0 re-qualification of the integrated QEMU
binary with capture and profile off, plus the disabled-hook cost measurement.

    t_b1_0.py --out OUT [--new QEMU] [--old QEMU] [--pairs 30] [--pairs-minimal N]
              [--workload minimal|freertos|compute|both|all] [--layout random|fixed]
              [--seed 1] [--timeout 120] [--skip-timing]

Every QEMU run goes through the WP8 harness (``scopt.run``): a fresh run
directory with its own ``cache/``, ``work/`` and RAM files, ``setarch x86_64
-R``, a timeout, an environment scrubbed of ``QEMU_SC_*`` (so capture and
profile stay off), ``supercache_flat=false`` (from the base TOMLs).

Part A, re-qualification with the NEW binary (per workload):

* ``nocache``, ``cold``, ``warm1``, ``warm2``, each in its own run directory;
  ``warm*`` deploy the ``cold`` run's ``.sc`` (archived read-only under
  ``OUT/<wl>/snapshot-new/``) through the manifest oracle, so the §4.8 proof
  runs and ``appended`` is measured.
* minimal: ``norm_b0(log) == norm_b0(runs/b0/<arm>.norm)`` for every arm,
  ``Reg 0:0x00000003`` and a BENCH line present, warm proof PASS and
  appended 0; the cold ``.sc`` has the same 163 record keys (tb_uid) as the
  B0 golden ``runs/b0/cache/minimal.sc``.
* freertos (icount arm of ``fixtures/freertos-mps2``): the strict guest
  normalization of ``fixtures/freertos-mps2/run.sh`` equals the qualified
  ``runs/b0-freertos/qual-icount/nocache-1/guest.norm`` and the final guest
  RAM sha256 equals the qualified one; warm proof PASS and appended 0. The
  fixture's TOMLs pin ``[Machine].qemu_path``; this script writes copies with
  that one line changed per binary under ``OUT/configs/``.
* compute (WP12 ``fixtures/freertos-mps2-compute``, icount, added by WP14):
  the ``input`` RAM bank holds ``fixtures/inputs/train/freertos-mps2-compute/
  train-1.bin`` in every run; the fixture normalization (``fixtures/lib/
  fdrun.py``: guest lines, BENCH host time masked, ``exit_code=``) equals
  WP12's ``expected/train-1.icount.norm``. Source TOML: the rendered
  ``runs/wp13-r2/compute/configs/compute-sc.toml`` (``--compute-toml``) with
  only ``qemu_path`` changed per binary; nocache drops only its
  ``supercache =`` line.
* no run prints a ``supercache-capture:`` / ``supercache-profile:`` banner
  and no ``*.scm``/``*.qprof`` appears in any run directory.

Part B, disabled-hook cost (per workload): ``--pairs`` pairs of warm runs,
OLD binary with its own cache (minimal: the B0 golden ``runs/b0/cache/
minimal.sc``, deployed as a sparse copy, never written; freertos: a fresh
OLD-binary cold run) and NEW binary with the Part A snapshot. Within each
pair the order is drawn from a seeded RNG (interleaved, balanced in
expectation). With ``--layout random`` (the default) every sample also gets
a random run-directory name length (0-63 extra characters) and a random-length
``SCOPT_LAYOUT_PAD`` environment value (0-4095 bytes). That is setup
randomization: one fixed setup was measured to carry a deterministic,
path-length-dependent bias of about +-5% between the two binaries, far above
the 1% flag threshold. ``--layout fixed`` reproduces the plain setup. Each
sample is a fresh deploy + launch; only runs with exit 0,
proof PASS, appended 0 and the reference output count. The metric is the
guest window's BENCH ``elapsed_ns``. Reported: per-arm median, the overhead
``median(new)/median(old) - 1`` with a 95% bootstrap CI (arms resampled
independently), and the median of per-pair ratios with its bootstrap CI.
The overhead is ``flagged`` only when the whole 95% CI is above 1%; it is
``unresolved`` when the CI contains 1% and either the median is above 1% or
the CI is wider than 5 points; else ``not_flagged``. It is not a gate
(design §6). Part A fails when the minimal cold ``.sc`` differs from the B0
golden in its tb_uid set or in any unit's host code size.

Writes ``OUT/t-b1-0.json``; exit 0 iff every Part A check passed and Part B
collected the requested number of valid pairs.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scopt.formats.sc_records import read_sc, sha256_sparse, sparse_copy  # noqa: E402
from scopt.run.deploy import deploy_new  # noqa: E402
from scopt.run.launcher import (B0_QEMU, FASTDYN_SRC, create_run_dir, file_sha256,  # noqa: E402
                                launch, norm_b0, write_json)

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
NEW_QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
B0_RUNS = ROOT / "runs/b0"
B0_GOLDEN = B0_RUNS / "cache/minimal.sc"
B0_NOCACHE_TOML = FASTDYN_SRC / "configs/b0-nocache.toml"
B0_SC_TOML = FASTDYN_SRC / "configs/b0-sc.toml"
FR_DIR = ROOT / "fixtures/freertos-mps2"
FR_QUAL = ROOT / "runs/b0-freertos/qual-icount"
FR_REF_GUEST = FR_QUAL / "nocache-1/guest.norm"
FR_REF_RAM = FR_QUAL / "nocache-1/ram0.sha256"
CP_DIR = ROOT / "fixtures/freertos-mps2-compute"
CP_TOML = ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml"
CP_INPUT = ROOT / "fixtures/inputs/train/freertos-mps2-compute/train-1.bin"
CP_EXPECTED = CP_DIR / "expected/train-1.icount.norm"
BANNERS = ("supercache-capture:", "supercache-profile:")
SCHEMA = "scopt.t-b1-0/1"


# --------------------------------------------------------------------------
# statistics (stdlib only)
# --------------------------------------------------------------------------

def bootstrap_ci(stat: Callable[..., float], samples: Sequence[Sequence[float]], *,
                 reps: int = 10000, seed: int = 1, level: float = 0.95) -> tuple[float, float]:
    """Percentile bootstrap CI of ``stat(*samples)``, each sample resampled independently."""
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        rs = [[s[rng.randrange(len(s))] for _ in range(len(s))] for s in samples]
        vals.append(stat(*rs))
    vals.sort()
    lo = vals[int((1 - level) / 2 * reps)]
    hi = vals[min(reps - 1, int((1 + level) / 2 * reps))]
    return lo, hi


#: CI width (upper - lower, as a fraction) above which a straddling CI cannot
#: resolve the 1% threshold at all
UNRESOLVED_CI_WIDTH = 0.05


def flag_status(median: float, lo: float, hi: float, *, threshold: float = 0.01,
                max_width: float = UNRESOLVED_CI_WIDTH) -> str:
    """``flagged``: the whole 95% CI is above ``threshold`` (the overhead is
    resolved and larger than 1%). ``unresolved``: the CI contains ``threshold``
    and either the median is above it or the CI is wider than ``max_width``
    (the data cannot tell). ``not_flagged`` otherwise (CI below the threshold,
    or straddling it narrowly with the median at or below it)."""
    if lo > threshold:
        return "flagged"
    if lo <= threshold <= hi and (median > threshold or hi - lo > max_width):
        return "unresolved"
    return "not_flagged"


def overhead_stats(old: Sequence[float], new: Sequence[float],
                   pairs: Sequence[tuple[float, float]] | None = None, *,
                   flag_threshold: float = 0.01, reps: int = 10000,
                   seed: int = 1) -> dict[str, Any]:
    """Median overhead of ``new`` vs ``old`` (ratio of medians - 1) with CIs.

    ``flag`` is :func:`flag_status` of the overhead and its CI;
    ``flagged`` is true only for ``flag == "flagged"``."""
    if not old or not new:
        return {"n_old": len(old), "n_new": len(new), "overhead_median": None,
                "flagged": None, "flag": "no_data"}
    m_old = statistics.median(old)
    m_new = statistics.median(new)
    ratio = lambda o, n: statistics.median(n) / statistics.median(o) - 1.0  # noqa: E731
    lo, hi = bootstrap_ci(ratio, [old, new], reps=reps, seed=seed)
    doc: dict[str, Any] = {
        "n_old": len(old), "n_new": len(new),
        "median_old": m_old, "median_new": m_new,
        "mean_old": statistics.fmean(old), "mean_new": statistics.fmean(new),
        "stdev_old": statistics.stdev(old) if len(old) > 1 else None,
        "stdev_new": statistics.stdev(new) if len(new) > 1 else None,
        "min_old": min(old), "min_new": min(new),
        "overhead_median": m_new / m_old - 1.0,
        "overhead_ci95": [lo, hi],
        "flag_threshold": flag_threshold,
    }
    doc["flag"] = flag_status(doc["overhead_median"], lo, hi, threshold=flag_threshold)
    doc["flagged"] = doc["flag"] == "flagged"
    doc["unresolved_ci_width"] = UNRESOLVED_CI_WIDTH
    if pairs:
        pr = [n / o - 1.0 for o, n in pairs]
        plo, phi = bootstrap_ci(lambda s: statistics.median(s), [pr], reps=reps, seed=seed + 1)
        doc["paired"] = {"n": len(pr), "median_ratio_minus_1": statistics.median(pr),
                         "ci95": [plo, phi],
                         "new_slower_count": sum(1 for x in pr if x > 0)}
    return doc


# --------------------------------------------------------------------------
# workloads
# --------------------------------------------------------------------------

_FR_GUEST_RE = re.compile(r"^(Current CPU type|\[[0-9]+\]\[[0-9.]+\] : |Reg [0-9]+:|"
                          r"\[BENCH\]|qemu-system-arm:)")


def freertos_guest_norm(text: str, rc: int) -> list[str]:
    """The strict guest normalization of ``fixtures/freertos-mps2/run.sh``."""
    out = []
    for line in text.splitlines():
        if _FR_GUEST_RE.match(line):
            out.append(re.sub(r"^(\[BENCH\]\[[^]]*\]) elapsed_ns=.*", r"\1 elapsed=<host-time>",
                              line))
    out.append(f"exit_code={rc}")
    return out


def with_qemu_path(src: Path, dst: Path, qemu: Path) -> Path:
    """Copy a fixture TOML, changing only its ``[Machine].qemu_path`` line."""
    text = src.read_text()
    new, n = re.subn(r'(?m)^qemu_path\s*=\s*"[^"]*"', f'qemu_path = "{qemu}"', text)
    if n != 1:
        raise SystemExit(f"{src}: expected one qemu_path line, found {n}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new)
    return dst


class Workload:
    name: str

    def inputs(self) -> dict[str, Path] | None:
        """RAM bank id -> input file written before every run (None: no input)."""
        return None

    def nocache_toml(self, qemu: Path) -> Path: ...
    def sc_toml(self, qemu: Path) -> Path: ...
    def check_output(self, arm: str, r: dict[str, Any], rd) -> list[str]: ...


class Minimal(Workload):
    name = "minimal"

    def nocache_toml(self, qemu: Path) -> Path:
        return B0_NOCACHE_TOML

    def sc_toml(self, qemu: Path) -> Path:
        return B0_SC_TOML

    def reference(self, arm: str) -> Path:
        """B0 reference: the same arm for Part A, warm1 for timing samples."""
        if arm in ("nocache", "cold", "warm1", "warm2"):
            return B0_RUNS / f"{arm}.norm"
        return B0_RUNS / ("cold.norm" if arm.endswith("cold") else "warm1.norm")

    def check_output(self, arm: str, r: dict[str, Any], rd) -> list[str]:
        fails = []
        text = Path(r["stdout_log"]).read_text(errors="replace")
        ref = norm_b0(self.reference(arm).read_text())
        if norm_b0(text) != ref:
            fails.append(f"{arm}: norm_b0 differs from {self.reference(arm).name}")
        if "Reg 0:0x00000003" not in r["guest_norm"]:
            fails.append(f"{arm}: no 'Reg 0:0x00000003'")
        if not r["bench"]:
            fails.append(f"{arm}: no BENCH line")
        return fails


class FreeRTOS(Workload):
    name = "freertos"

    def __init__(self, cfg_dir: Path):
        self.cfg_dir = cfg_dir
        self.ref_guest = FR_REF_GUEST.read_text().splitlines()
        self.ref_ram = FR_REF_RAM.read_text().strip()

    def _tag(self, qemu: Path) -> str:
        return "new" if Path(qemu).resolve() != B0_QEMU.resolve() else "old"

    def nocache_toml(self, qemu: Path) -> Path:
        return with_qemu_path(FR_DIR / "configs/nocache.toml",
                              self.cfg_dir / f"freertos-nocache-{self._tag(qemu)}.toml", qemu)

    def sc_toml(self, qemu: Path) -> Path:
        return with_qemu_path(FR_DIR / "configs/sc.toml",
                              self.cfg_dir / f"freertos-sc-{self._tag(qemu)}.toml", qemu)

    def check_output(self, arm: str, r: dict[str, Any], rd) -> list[str]:
        fails = []
        text = Path(r["stdout_log"]).read_text(errors="replace")
        g = freertos_guest_norm(text, r["exit_code"])
        r["freertos_guest_norm_sha256"] = hashlib.sha256(
            ("\n".join(g) + "\n").encode()).hexdigest()
        if g != self.ref_guest:
            fails.append(f"{arm}: guest.norm differs from {FR_REF_GUEST}")
        ram = rd.ram / "ram0.ram"
        if ram.is_file():
            h = hashlib.sha256(ram.read_bytes()).hexdigest()
            r["ram0_sha256"] = h
            if h != self.ref_ram:
                fails.append(f"{arm}: final RAM sha256 {h[:16]} != qualified {self.ref_ram[:16]}")
        else:
            fails.append(f"{arm}: no ram0.ram after the run")
        if not r["bench"]:
            fails.append(f"{arm}: no BENCH line")
        return fails


class Compute(Workload):
    """WP12 compute variant (icount) with the train-1 input in the ``input`` bank."""
    name = "compute"

    def __init__(self, cfg_dir: Path, src: Path = CP_TOML, inp: Path = CP_INPUT,
                 expected: Path = CP_EXPECTED):
        self.cfg_dir = cfg_dir
        self.src = src
        self.inp = inp
        self.ref = expected.read_text().splitlines()

    def inputs(self) -> dict[str, Path] | None:
        return {"input": self.inp}

    def _tag(self, qemu: Path) -> str:
        return "new" if Path(qemu).resolve() != B0_QEMU.resolve() else "old"

    def sc_toml(self, qemu: Path) -> Path:
        return with_qemu_path(self.src, self.cfg_dir / f"compute-sc-{self._tag(qemu)}.toml", qemu)

    def nocache_toml(self, qemu: Path) -> Path:
        sc = self.sc_toml(qemu)
        text, n = re.subn(r'(?m)^supercache\s*=\s*"[^"]*"\n', "", sc.read_text())
        if n != 1:
            raise SystemExit(f"{sc}: expected one supercache line, found {n}")
        dst = self.cfg_dir / f"compute-nocache-{self._tag(qemu)}.toml"
        dst.write_text(text)
        return dst

    def check_output(self, arm: str, r: dict[str, Any], rd) -> list[str]:
        fails = []
        text = Path(r["stdout_log"]).read_text(errors="replace")
        g = freertos_guest_norm(text, r["exit_code"])  # same rules as fixtures/lib/fdrun.py
        r["freertos_guest_norm_sha256"] = hashlib.sha256(
            ("\n".join(g) + "\n").encode()).hexdigest()
        if g != self.ref:
            fails.append(f"{arm}: guest.norm differs from {CP_EXPECTED.name}")
        if not r["bench"]:
            fails.append(f"{arm}: no BENCH line")
        inp = (r.get("inputs") or {}).get("input") or {}
        if inp.get("sha256") != "sha256:" + hashlib.sha256(self.inp.read_bytes()).hexdigest():
            fails.append(f"{arm}: input bank does not hold {self.inp.name}")
        return fails


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def _common_checks(arm: str, r: dict[str, Any], *, qemu: Path, warm: bool) -> list[str]:
    fails = []
    if r["exit_code"] != 0 or r["timed_out"]:
        fails.append(f"{arm}: exit {r['exit_code']} timed_out={r['timed_out']}")
    if r["qemu"] is None or Path(r["qemu"]).resolve() != Path(qemu).resolve():
        fails.append(f"{arm}: ran QEMU {r['qemu']}, expected {qemu}")
    text = Path(r["stdout_log"]).read_text(errors="replace")
    for b in BANNERS:
        if b in text:
            fails.append(f"{arm}: '{b}' banner in a capture/profile-off run")
    if r["stray_capture_files"]:
        fails.append(f"{arm}: stray capture files {r['stray_capture_files']}")
    if any(k.startswith("QEMU_SC_") for k in r["env_diff"]["set"]):
        fails.append(f"{arm}: QEMU_SC_* set in the run environment")
    if warm:
        if r["verdict"] != "PASS":
            fails.append(f"{arm}: verdict {r['verdict']}")
        if r["appended"] != 0:
            fails.append(f"{arm}: appended {r['appended']}")
        if r["cache_invalidated_by_frontend"]:
            fails.append(f"{arm}: frontend invalidated the cache")
    return fails


def _brief(r: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    keep = ("run_dir", "exit_code", "timed_out", "verdict", "appended", "qemu", "qemu_sha256",
            "output_norm_sha256", "cache_invalidated_by_frontend", "stray_capture_files",
            "freertos_guest_norm_sha256", "ram0_sha256")
    d = {k: r.get(k) for k in keep if k in r}
    d["bench_ns"] = r["bench"][0]["elapsed_ns"] if r["bench"] else None
    d["wall_s"] = r["timings"]["wall_s"]
    if extra:
        d.update(extra)
    return d


def _archive_snapshot(cache: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / cache.name
    sparse_copy(cache, dst)
    man = Path(str(cache) + ".fastdyn.json")
    if man.is_file():
        shutil.copyfile(man, str(dst) + ".fastdyn.json")
    os.chmod(dst, 0o444)
    return dst


def golden_checks(gold, sc) -> tuple[dict[str, Any], list[str]]:
    """Part A, minimal: the cold ``.sc`` against the B0 golden. Both the tb_uid set
    and every unit's host code size (``tc_size`` per tb_uid) must be equal; either
    difference is a Part A failure."""
    a = sorted(u.tb_uid for u in gold.units())
    b = sorted(u.tb_uid for u in sc.units())
    tc_a = sorted((u.tb_uid, u.record.tc_size) for u in gold.units())
    tc_b = sorted((u.tb_uid, u.record.tc_size) for u in sc.units())
    extra = {"tb_uids_equal_b0_golden": a == b,
             "b0_golden_record_count": gold.header.record_count,
             "tc_sizes_equal_b0_golden": tc_a == tc_b}
    fails = []
    if a != b:
        fails.append("cold: tb_uid set differs from the B0 golden minimal.sc")
    if tc_a != tc_b:
        fails.append("cold: host code sizes (tc_size per tb_uid) differ from the B0 golden "
                     "minimal.sc")
    return extra, fails


def requalify(wl: Workload, out: Path, qemu: Path, timeout: float,
              log: Callable[[str], None]) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    runs: dict[str, Any] = {}
    fails: list[str] = []

    rd = create_run_dir(wl.nocache_toml(qemu), out / "nocache", qemu=qemu, label="t-b1-0")
    r = launch(rd, timeout_s=timeout, prove=False, inputs=wl.inputs())
    f = _common_checks("nocache", r, qemu=qemu, warm=False) + wl.check_output("nocache", r, rd)
    runs["nocache"] = _brief(r, {"failures": f})
    fails += f
    log(f"  {wl.name} nocache: exit={r['exit_code']} fails={len(f)}")

    rd = create_run_dir(wl.sc_toml(qemu), out / "cold", qemu=qemu, label="t-b1-0")
    r = launch(rd, timeout_s=timeout, prove=False, inputs=wl.inputs())
    f = _common_checks("cold", r, qemu=qemu, warm=False) + wl.check_output("cold", r, rd)
    snap = None
    extra: dict[str, Any] = {}
    if rd.cache is not None and rd.cache.is_file():
        snap = _archive_snapshot(rd.cache, out / "snapshot-new")
        sc = read_sc(snap)
        extra = {"snapshot": str(snap), "snapshot_sha256": sha256_sparse(snap),
                 "record_count": sc.header.record_count,
                 "runtime_signature": f"0x{sc.header.runtime_signature:016x}",
                 "code_address": f"0x{sc.header.code_address:x}"}
        if sc.header.record_count == 0:
            f.append("cold: 0 records")
        if isinstance(wl, Minimal):
            gx, gf = golden_checks(read_sc(B0_GOLDEN), sc)
            extra.update(gx)
            f += gf
    else:
        f.append("cold: no .sc written")
    runs["cold"] = _brief(r, {"failures": f, **extra})
    fails += f
    log(f"  {wl.name} cold: exit={r['exit_code']} records={extra.get('record_count')} "
        f"fails={len(f)}")

    for arm in ("warm1", "warm2"):
        if snap is None:
            fails.append(f"{arm}: skipped (no snapshot)")
            continue
        rd, dep = deploy_new(snap, wl.sc_toml(qemu), out / arm, qemu=qemu, label="t-b1-0")
        r = launch(rd, timeout_s=timeout, inputs=wl.inputs())
        f = _common_checks(arm, r, qemu=qemu, warm=True) + wl.check_output(arm, r, rd)
        runs[arm] = _brief(r, {"failures": f, "deployed_n": dep["state"]["n"]})
        fails += f
        log(f"  {wl.name} {arm}: verdict={r['verdict']} appended={r['appended']} "
            f"fails={len(f)}")
    return {"runs": runs, "failures": fails, "snapshot": str(snap) if snap else None,
            "verdict": "PASS" if not fails else "FAIL"}


def old_snapshot(wl: Workload, out: Path, old: Path, timeout: float) -> tuple[Path, dict]:
    if isinstance(wl, Minimal):
        return B0_GOLDEN, {"source": "runs/b0/cache/minimal.sc (B0 golden, sparse-copied per run)",
                           "sha256": sha256_sparse(B0_GOLDEN)}
    rd = create_run_dir(wl.sc_toml(old), out / "old-cold", qemu=old, label="t-b1-0")
    r = launch(rd, timeout_s=timeout, prove=False, inputs=wl.inputs())
    f = _common_checks("old-cold", r, qemu=old, warm=False) + wl.check_output("old-cold", r, rd)
    if f or rd.cache is None or not rd.cache.is_file():
        raise SystemExit(f"old-binary cold run failed: {f}")
    snap = _archive_snapshot(rd.cache, out / "snapshot-old")
    return snap, {"source": "fresh OLD-binary cold run", "run": _brief(r),
                  "sha256": sha256_sparse(snap)}


LAYOUT_DIR_PAD_MAX = 63    # extra characters in the run-directory name
LAYOUT_ENV_PAD_MAX = 4095  # length of the SCOPT_LAYOUT_PAD environment value


def timing(wl: Workload, out: Path, arms: dict[str, tuple[Path, Path]], pairs: int,
           seed: int, timeout: float, log: Callable[[str], None], *,
           layout: str = "random") -> dict[str, Any]:
    """Interleaved warm pairs. ``layout="random"`` (setup randomization): every
    sample draws its own run-directory name length and environment pad, which
    shift QEMU's argv/env strings and heap allocations; one fixed setup carries
    a deterministic layout bias of several percent (see RUNTIME.md)."""
    rng = random.Random(seed)
    lrng = random.Random(seed + 1000)
    samples: list[dict[str, Any]] = []
    good: dict[str, list[float]] = {"old": [], "new": []}
    paired: list[tuple[float, float]] = []
    tdir = out / "timing"
    tdir.mkdir(parents=True, exist_ok=True)
    max_attempts = pairs * 2
    i = 0
    while len(paired) < pairs and i < max_attempts:
        order = ["old", "new"]
        rng.shuffle(order)
        vals: dict[str, float] = {}
        for arm in order:
            qemu, snap = arms[arm]
            name = f"{i:03d}-{arm}"
            env_pad = 0
            if layout == "random":
                name += "-" + "p" * lrng.randint(0, LAYOUT_DIR_PAD_MAX)
                env_pad = lrng.randint(0, LAYOUT_ENV_PAD_MAX)
            la0 = os.getloadavg()[0]
            rd, _ = deploy_new(snap, wl.sc_toml(qemu), tdir / name, qemu=qemu, label="t-b1-0")
            r = launch(rd, timeout_s=timeout, inputs=wl.inputs(),
                       extra_env={"SCOPT_LAYOUT_PAD": "x" * env_pad} if env_pad else None)
            f = _common_checks(name, r, qemu=qemu, warm=True) + wl.check_output(name, r, rd)
            row = _brief(r, {"pair": i, "arm": arm, "pos": order.index(arm), "failures": f,
                             "run_dir_name_len": len(name), "env_pad": env_pad,
                             "loadavg1_before": la0, "loadavg1_after": os.getloadavg()[0]})
            samples.append(row)
            for p in rd.ram.glob("*.ram"):  # keep disk use bounded; hashes are recorded
                p.unlink()
            if not f and row["bench_ns"] is not None:
                vals[arm] = float(row["bench_ns"])
                good[arm].append(float(row["bench_ns"]))
        if len(vals) == 2:
            paired.append((vals["old"], vals["new"]))
        log(f"  {wl.name} pair {i:03d} {'/'.join(order)}: "
            + " ".join(f"{a}={vals.get(a, float('nan')) / 1e3:.1f}us" for a in ("old", "new"))
            + ("" if len(vals) == 2 else "  (pair invalid)"))
        i += 1
    st = overhead_stats(good["old"], good["new"], paired, seed=seed)
    return {"pairs_requested": pairs, "pairs_valid": len(paired), "pairs_attempted": i,
            "invalid_samples": [s for s in samples if s["failures"]],
            "stats": st, "samples": samples, "metric": "BENCH elapsed_ns (guest window)",
            "seed": seed, "layout": layout}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--new", type=Path, default=NEW_QEMU, help="integrated binary")
    ap.add_argument("--old", type=Path, default=B0_QEMU, help="B0 binary")
    ap.add_argument("--pairs", type=int, default=30)
    ap.add_argument("--workload", choices=("minimal", "freertos", "compute", "both", "all"),
                    default="both", help="both = minimal+freertos; all adds compute")
    ap.add_argument("--compute-toml", type=Path, default=CP_TOML,
                    help="rendered compute fixture TOML (qemu_path is rewritten per binary)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--skip-timing", action="store_true")
    ap.add_argument("--layout", choices=("random", "fixed"), default="random",
                    help="random: per-sample run-dir name length and env pad (default)")
    ap.add_argument("--pairs-minimal", type=int, help="override --pairs for minimal")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error(f"{out} is not empty; use a fresh output directory")
    out.mkdir(parents=True, exist_ok=True)
    new, old = a.new.resolve(), a.old.resolve()
    logf = open(out / "t-b1-0.log", "a")

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "new_qemu": str(new), "new_qemu_sha256": file_sha256(new),
        "old_qemu": str(old), "old_qemu_sha256": file_sha256(old),
        "b0_golden_sha256_before": sha256_sparse(B0_GOLDEN),
        "pairs": a.pairs, "pairs_minimal": a.pairs_minimal or a.pairs, "seed": a.seed,
        "layout": a.layout, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "loadavg_start": os.getloadavg(), "nproc": os.cpu_count(),
        "workloads": {},
    }
    wls: list[Workload] = []
    if a.workload in ("minimal", "both", "all"):
        wls.append(Minimal())
    if a.workload in ("freertos", "both", "all"):
        wls.append(FreeRTOS(out / "configs"))
    if a.workload in ("compute", "all"):
        wls.append(Compute(out / "configs", src=a.compute_toml))
    ok = True
    for wl in wls:
        log(f"== {wl.name}: part A (re-qualification, new binary)")
        wdir = out / wl.name
        req = requalify(wl, wdir / "requal", new, a.timeout, log)
        w: dict[str, Any] = {"requalification": req}
        ok &= req["verdict"] == "PASS"
        if not a.skip_timing and req["snapshot"]:
            log(f"== {wl.name}: part B (disabled-hook cost, interleaved pairs, "
                f"layout={a.layout})")
            osnap, oinfo = old_snapshot(wl, wdir, old, a.timeout)
            w["old_snapshot"] = {"path": str(osnap), **oinfo}
            w["new_snapshot"] = {"path": req["snapshot"]}
            npairs = a.pairs_minimal if (wl.name == "minimal" and a.pairs_minimal) else a.pairs
            tm = timing(wl, wdir, {"old": (old, osnap), "new": (new, Path(req["snapshot"]))},
                        npairs, a.seed, a.timeout, log, layout=a.layout)
            w["timing"] = tm
            ok &= tm["pairs_valid"] >= npairs
            s = tm["stats"]
            if s.get("overhead_median") is not None:
                log(f"   {wl.name}: median old={s['median_old'] / 1e3:.1f}us "
                    f"new={s['median_new'] / 1e3:.1f}us overhead={s['overhead_median'] * 100:+.2f}% "
                    f"CI95=[{s['overhead_ci95'][0] * 100:+.2f}%, {s['overhead_ci95'][1] * 100:+.2f}%]"
                    f" paired={s['paired']['median_ratio_minus_1'] * 100:+.2f}% "
                    f"flag={s['flag']}")
        doc["workloads"][wl.name] = w
        write_json(out / "t-b1-0.json", doc)
    doc["b0_golden_sha256_after"] = sha256_sparse(B0_GOLDEN)
    if doc["b0_golden_sha256_after"] != doc["b0_golden_sha256_before"]:
        ok = False
        doc["b0_golden_changed"] = True
    doc["loadavg_end"] = os.getloadavg()
    doc["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    doc["verdict"] = "PASS" if ok else "FAIL"
    write_json(out / "t-b1-0.json", doc)
    log(f"verdict={doc['verdict']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
