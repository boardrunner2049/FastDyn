#!/usr/bin/env python3
"""WP28: the B3 gate (design §11 "B3 spec gate", "B3 hardening", plan ``## WP28``) on the
frozen gate binary ``qint-v6-gate-1`` (``runs/wp22/gate-binary.json``).

    cd wt/scopt-wp28
    taskset -c 16-23 python tests/qemu/wp28_gate.py --out runs/wp28 [--steps a,b,...]

Run with the tools-env python from the worktree. Every QEMU run goes through the
scopt/FastDyn launcher or WP22's/WP24's diag runners (``setarch x86_64 -R``, timeout, fresh
run directory, ``supercache_flat = false``); gdb is always ``-nx`` with cwd outside any QEMU
tree. Nothing here is timed; timing is ``tests/qemu/wp28_timing.sh`` (holds
``runs/.timing.lock``).

Steps (each writes ``OUT/<step>.json``; ``verdict`` reads them all):

``gate``       exe sha256, tag, HEAD and clean tree of the gate binary.
``bases``      every B3 fixture config (icount shift 0 and noicount) prepared, imported and
               profiled on the gate binary (``wp20_fixture_snapshots.py``) plus
               ``scopt admission identity`` (RuntimeIdentity v2). F-B3-11 is captured
               and profiled on its **train** input only (``fx-11t``), so the held-out call
               site and path are unseen.
``artifacts``  ``scopt optimize --pipeline b3 --b3`` (all six B3 passes; fixture param
               ``specialize-returns.growth_fraction = 1.0``) on every base, the static rows
               of every artifact (V1-V21, V23; V22 ``not_run: WP23 not approved``), the
               slots/variants from the output ``cache.sc`` and the passes' refusal counts,
               then ``scopt publish`` (verified copy incl. the hostsym coverage check).
``dynamic``    ``dynamic_oracles`` O1/O2a/O2b/O3/O5 (fixture per-mode oracle) of every
               published artifact against its warm base, both modes; gdb hit counts on every
               C' and every SPEC slot's fallback F (+ per-fixture extra host targets).
``gdbdiag``    F-B3-12 and F-B3-13 (a)-(d): the WP24 guest-gdb scenarios on the published
               icount artifacts, run by WP22's host-gdb harness (loader events, slot state,
               v6 stats, variant hits); expectations as WP22's integration found them.
``stale``      F-B3-16 (a)-(e): deploy refusals / post-run proofs / the direct guest-hash
               rerun (``b3_16d.elf``) with v6 stats.
``o4``         negative controls (§9.3 dynamic set) built from the manager's own plan of the
               named fixture (WP26 ``package_negative``), O4 on that fixture.
``mutants``    static mutation suite: ``synthetic_suite`` (V18), ``real_mutants`` and the
               link mutants (WP19) on every icount fixture base, and the guard mutants on
               the **manager's** link of every fixture artifact (100 % rejected).
``roundtrip``  T-B3-28: CalleeSummary export for every return unit and the graph round trip
               with ``guarded_slot`` / ``variant_link`` edges of every artifact.
``compute``    compute base on the gate binary, the ``b3-chain`` / ``b3-static`` /
               ``b3-spec`` artifacts, V10 on held-out inputs 1-3.
``verdict``    ``OUT/verdict.json`` (gate items, hardening, reported outcomes).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FIX = ROOT / "fixtures/b3"
for _p in (WT, HERE, FIX, FIX / "diag", ROOT / "fixtures/lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
GATE_JSON = ROOT / "runs/wp22/gate-binary.json"
QINT = ROOT / "wt/qemu-int/build/qemu-system-arm"
ENVLAYOUT_DIR = ROOT / "runs/wp18/envlayout"
PY = sys.executable
JOBS = int(os.environ.get("WP28_JOBS", "4"))
os.environ.setdefault("SCOPT_ENVLAYOUT_PATH", str(ENVLAYOUT_DIR))
os.environ.setdefault("WP21_CPUS", os.environ.get("WP28_CPUS", "16-23"))

import b3conf  # noqa: E402  (fixtures/b3/b3conf.py)

MODES = ("icount", "noicount")
FIXTURE_PARAMS = {"specialize-returns": {"growth_fraction": 1.0}}
STATIC_ROWS = [f"V{i}" for i in range(1, 22) if i != 10] + ["V23"]
WANT_SKIP = {"V9", "V22"}  # V9: not an identity candidate; V22: WP23 not approved


@dataclasses.dataclass(frozen=True)
class Fx:
    """One fixture config of the gate (design §9.5 row)."""
    fixture: str
    group: str  # spec | hardening | other
    vc: tuple[str, ...] = ()  # continuation symbols whose SPEC variant must run (O3 C' > 0)
    spec: bool = True  # a SPEC link to vc is expected
    refusals: tuple[str, ...] = ()  # specialize-returns refusal counts expected > 0
    exit_reasons: tuple[tuple[str, str], ...] = ()  # (exit symbol, return-edges reason code)
    stats: tuple[tuple[str, str, str], ...] = ()  # (mode|*, counter, requirement)
    inputs: tuple[str, ...] | None = None  # fixture inputs for the dynamic runs
    note: str = ""


FIXTURES: dict[str, Fx] = {
    "1": Fx("F-B3-1", "spec", ("vc_b3_1",), note="two-TB example"),
    "1p": Fx("F-B3-1p", "spec", ("vc_b3_1p",), note="pop-return shape"),
    "2": Fx("F-B3-2", "spec", ("vc_b3_2",), note="effectful callee: guest load/store"),
    "3": Fx("F-B3-3", "spec", ("vc_b3_3",), note="effectful callee: device (SYST_CSR)"),
    "3b": Fx("F-B3-3b", "spec", ("vc_b3_3b", "vc_b3_3c"), note="dead-result MMIO / faulting load"),
    "4": Fx("F-B3-4", "spec", ("vc_b3_4",), note="effectful callee: plugin callback"),
    "5": Fx("F-B3-5", "spec", (), spec=False, refusals=("fact_killed_by_helper",),
            note="helper clobber (randstate writes r0)"),
    "6": Fx("F-B3-6", "spec", ("vc_b3_6",), note="alternate callers, arbitrary entry (bx r3)"),
    "6t": Fx("F-B3-6t", "spec", ("vc_b3_6t",), note="tail call"),
    "6r": Fx("F-B3-6r", "spec", ("rec_b3_cont",),
             note="recursion: both returns of rec_b3 (base case pop, z-path epilogue) specialize "
                  "rec_b3_cont, the grown epilogue unit itself"),
    "6h": Fx("F-B3-6h", "spec", (), spec=False,
             exit_reasons=(("f_b3_6h_ret", "handler_mode_return"),),
             note="handler-mode negative"),
    "7": Fx("F-B3-7", "spec", ("vc_b3_7",), stats=(("icount", "variant_entry_exits", ">0"),),
            note="interrupt clobber at Vc"),
    "8": Fx("F-B3-8", "spec", ("vc_b3_8",), note="fault in callee"),
    "8p": Fx("F-B3-8p", "spec", ("vc_b3_8p",), note="fault in R's last insn (pop)"),
    "9": Fx("F-B3-9", "spec", ("vc_b3_9",), stats=(("*", "variant_restores", ">0"),),
            note="fault in the second member"),
    "10": Fx("F-B3-10", "spec", ("vc_b3_10",), note="faulting unused load"),
    "11": Fx("F-B3-11", "spec", ("vc_b3_11",), inputs=("11-train", "11-heldout"),
             note="unseen target / unobserved branch (base: train input only)"),
    "12": Fx("F-B3-12", "spec", ("vc_b3_12",), note="code-page invalidation (+ gdbdiag)"),
    "13": Fx("F-B3-13", "hardening", ("vc_b3_13",), note="debug boundary (+ gdbdiag)"),
    "14": Fx("F-B3-14", "hardening", ("vc_b3_14",),
             stats=(("*", "disarm_tlb_events", ">0"),), note="mapping change (MPU enable)"),
    "17": Fx("F-B3-17", "hardening", (), spec=False, note="SMC in RAM"),
    "20": Fx("F-B3-20", "spec", ("vc_b3_20",), note="interrupt clobber at a budget boundary"),
    "21": Fx("F-B3-21", "hardening", ("vc_b3_21",), note="privilege drop"),
    "22": Fx("F-B3-22", "hardening", ("vc_b3_22",), note="INVSTATE via LR bit 0"),
    "23": Fx("F-B3-23", "spec", (), spec=False, note="coverage attribution (O6)"),
    "24-o1-c1": Fx("F-B3-24", "spec", (), spec=False, note="C twin of F-B3-1, -O1"),
    "24-o1-c2": Fx("F-B3-24", "spec", (), spec=False, note="C twin of F-B3-2, -O1"),
    "24-o0-c1": Fx("F-B3-24", "spec", (), spec=False, note="C twin of F-B3-1, -O0"),
    "24-o0-c2": Fx("F-B3-24", "spec", (), spec=False, note="C twin of F-B3-2, -O0"),
    "24-os-c1": Fx("F-B3-24", "spec", (), spec=False, note="C twin of F-B3-1, -Os"),
    "24-os-c2": Fx("F-B3-24", "spec", (), spec=False, note="C twin of F-B3-2, -Os"),
}
# bases only (references for F-B3-16 deploys)
REF_ONLY = ("16a", "16b", "16e")
CAPTURE = [c for c in FIXTURES if c != "11"] + list(REF_ONLY)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def dump(p: Path, doc: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    tmp.replace(p)


def rd(p: Path) -> Any:
    return json.loads(p.read_text()) if p.is_file() else None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def rmtree(d: Path) -> None:
    if d.exists():
        subprocess.run(["chmod", "-R", "u+w", str(d)], check=False)
        shutil.rmtree(d)


def cli(args: list[Any], logf: Path, *, timeout: float = 3600) -> tuple[int, str]:
    """``python -m scopt ARGS`` from the worktree; stdout returned, both logged."""
    logf.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    p = subprocess.run([PY, "-m", "scopt", *[str(a) for a in args]], cwd=str(WT),
                       capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "SCOPT_ENVLAYOUT_PATH": str(ENVLAYOUT_DIR)})
    logf.write_text("$ python -m scopt " + " ".join(str(a) for a in args) + "\n--- stdout\n" +
                    p.stdout[-200000:] + "\n--- stderr\n" + p.stderr[-20000:] +
                    f"\nexit={p.returncode} seconds={time.time() - t0:.1f}\n")
    return p.returncode, p.stdout


_SYMS: dict[str, dict[str, int]] = {}


def sym(name: str, elf: str = "b3.elf") -> int:
    if elf not in _SYMS:
        _SYMS[elf] = b3conf.symbols(elf)
    return _SYMS[elf][name] & ~1


def gate_info() -> dict[str, Any]:
    g = json.loads(GATE_JSON.read_text())
    return {"exe": Path(g["exe_path"]), "sha": g["exe_sha256"], "record": g}


def pmap(fn, items, jobs: int = JOBS) -> list[Any]:
    """``fn`` over ``items`` in threads; exceptions become ``{"error": ...}`` rows."""
    out: list[Any] = [None] * len(items)
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in cf.as_completed(futs):
            i = futs[f]
            try:
                out[i] = f.result()
            except Exception as e:  # noqa: BLE001 - recorded, never hidden
                out[i] = {"error": f"{type(e).__name__}: {e}",
                          "traceback": traceback.format_exc()[-4000:]}
                log("ERROR", items[i], out[i]["error"])
    return out


# ---------------------------------------------------------------------------
# gate binary
# ---------------------------------------------------------------------------

def step_gate(out: Path) -> dict[str, Any]:
    gi = gate_info()
    g = gi["record"]
    wt = Path(g["worktree"])

    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True,
                              text=True).stdout.strip()
    s = sha256(gi["exe"])
    head = git("rev-parse", "HEAD")
    tag = git("rev-parse", "qint-v6-gate-1^{commit}")
    dirty = git("status", "--porcelain")
    doc = {"exe": str(gi["exe"]), "exe_sha256": s, "recorded_sha256": g["exe_sha256"],
           "tag": "qint-v6-gate-1", "tag_commit": tag, "head": head,
           "recorded_commit": g["commit"], "tree_clean": dirty == "",
           "qint_exe_sha256": sha256(QINT),
           "ok": s == g["exe_sha256"] and head == tag == g["commit"] and dirty == "",
           "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    dump(out / "gate.json", doc)
    log("gate", s[:16], tag[:10], head[:10], "clean", dirty == "", "->", doc["ok"])
    if not doc["ok"]:
        raise SystemExit("gate binary check failed")
    return doc


# ---------------------------------------------------------------------------
# bases
# ---------------------------------------------------------------------------

def base_dir(out: Path, mode: str, cfg: str) -> Path:
    return out / ("fx-11t" if cfg == "11" else "fx") / mode / cfg


def base_info(out: Path, mode: str, cfg: str) -> dict[str, Any] | None:
    d = base_dir(out, mode, cfg)
    sj = rd(d / "snap.json")
    if not sj or not sj.get("snapshot"):
        return None
    return {"dir": d, "cfg": cfg, "mode": mode, "snapshot": Path(sj["snapshot"]),
            "prep": Path(sj["prep"]), "profile": Path(sj["profile"]) if sj.get("profile")
            else None, "toml": Path(sj["base_toml"]), "ri": d / "runtime_identity.v2.json",
            "inputs": [Path(x) for x in sj["inputs"]], "elf": FIX / sj["elf"],
            "qemu": sj["qemu"], "prep_verdict": sj.get("prep_verdict")}


def _capture(out_root: Path, cfgs: list[str], modes: tuple[str, ...], exe: Path,
             logf: Path) -> int:
    todo = [c for c in cfgs if any(not (out_root / m / c / "snap.json").is_file()
                                   for m in modes)]
    if not todo:
        return 0
    p = subprocess.run([PY, str(HERE / "wp20_fixture_snapshots.py"), "--out", str(out_root),
                        "--qemu", str(exe), "--configs", ",".join(todo), "--modes",
                        ",".join(modes), "--profile", "-j", str(JOBS)], cwd=str(WT),
                       capture_output=True, text=True)
    logf.write_text(p.stdout + p.stderr)
    return p.returncode


def _capture_11_train(out: Path, exe: Path) -> None:
    """F-B3-11 base on the train input only (held-out site and path unseen)."""
    import wp20_fixture_snapshots as W20

    cfg = b3conf.BY_NAME["11"]
    W20.b3conf.BY_NAME["11"] = dataclasses.replace(cfg, inputs={"-train": 0})
    try:
        for m in MODES:
            d = out / "fx-11t" / m / "11"
            if (d / "snap.json").is_file():
                continue
            rmtree(d)
            r = W20.one(out / "fx-11t", m, "11", exe, True)
            dump(d / "snap.json", r)
    finally:
        W20.b3conf.BY_NAME["11"] = cfg


def step_bases(out: Path) -> dict[str, Any]:
    gi = gate_info()
    exe = gi["exe"]
    rc = _capture(out / "fx", CAPTURE, MODES, exe, out / "fx-capture-step.log")
    _capture_11_train(out, exe)
    res: dict[str, Any] = {"capture_rc": rc, "bases": {}}
    for m in MODES:
        for c in list(FIXTURES) + list(REF_ONLY):
            b = base_info(out, m, c)
            key = f"{m}/{c}"
            if b is None:
                res["bases"][key] = {"ok": False, "why": "no snapshot"}
                continue
            if not b["ri"].is_file():
                cli(["admission", "identity", "--snapshot", b["snapshot"], "--config", b["toml"],
                     "--out", b["ri"]], b["dir"] / "logs/06-admission.log")
            ri = rd(b["ri"]) or {}
            row = {"snapshot": str(b["snapshot"]), "profile": str(b["profile"]),
                   "prep_verdict": b["prep_verdict"], "qemu_is_gate": Path(b["qemu"]) == exe,
                   "inputs": [p.name for p in b["inputs"]],
                   "ri_qemu_sha256": ri.get("qemu_sha256"), "ri_schema": ri.get("schema"),
                   "envlayout_id": (ri.get("admission") or {}).get("envlayout_id"),
                   "tb_start_cb_possible": (ri.get("admission") or {}).get(
                       "tb_start_cb_possible"),
                   "elf_sha256": sha256(b["elf"]),
                   "base_cache_sc_sha256": sha256(b["snapshot"] / "cache.sc")}
            row["ok"] = (row["qemu_is_gate"] and row["prep_verdict"] == "PASS" and
                         row["ri_qemu_sha256"] == "sha256:" + gi["sha"] and
                         row["ri_schema"] == "scopt.runtime_identity/2" and
                         (b["profile"] is not None or c in REF_ONLY))
            res["bases"][key] = row
    res["ok"] = all(r["ok"] for r in res["bases"].values())
    res["n"] = len(res["bases"])
    res["n_ok"] = sum(1 for r in res["bases"].values() if r["ok"])
    dump(out / "bases.json", res)
    log("bases", res["n_ok"], "/", res["n"])
    return res


# ---------------------------------------------------------------------------
# artifacts (static part of every fixture)
# ---------------------------------------------------------------------------

def selftest_path(out: Path) -> Path:
    return out / "hostsym_selftest.json"


def ensure_selftest(out: Path) -> dict[str, Any]:
    from scopt.validate_sym.selftest import load_or_run

    st = load_or_run(str(selftest_path(out)))
    return st if isinstance(st, dict) else {"loaded": True}


def ext_slots(sc: Path, in_vaddrs: list[int | None], r0_off: int) -> dict[str, Any]:
    import wp21_integ as W

    return W.variant_diff(sc, r0_off, in_vaddrs)


def _passes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"pass": p.get("pass"), "outcome": p.get("outcome"), "reason": p.get("reason"),
             "stats": p.get("stats") or {}} for p in doc.get("passes", [])]


def optimize_one(out: Path, b: dict[str, Any], tag: str, store: Path, *,
                 pipeline: str = "b3", disable: tuple[str, ...] = (),
                 params: dict[str, Any] | None = None) -> dict[str, Any]:
    args: list[Any] = ["optimize", b["snapshot"], "--pipeline", pipeline, "--b3", "--store", store,
                       "--runtime-identity", b["ri"], "--prep", b["prep"], "--elf", b["elf"],
                       "--selftest", selftest_path(out), "--json"]
    if b.get("profile"):
        args += ["--profile", b["profile"]]
    for p in disable:
        args += ["--disable", p]
    if params:
        pf = out / "params" / f"{tag}.json"
        dump(pf, params)
        args += ["--params", pf]
    t0 = time.time()
    rc, so = cli(args, out / "logs" / f"optimize-{tag}.log")
    try:
        doc = json.loads(so)
    except ValueError:
        return {"exit": rc, "error": "no JSON on stdout", "status": None}
    return {"exit": rc, "seconds": round(time.time() - t0, 1), "status": doc.get("status"),
            "outcome": doc.get("outcome"), "reason": doc.get("reason"),
            "artifact_dir": doc.get("artifact_dir"), "artifact_id": doc.get("artifact_id"),
            "rejection_path": doc.get("rejection_path"),
            "pass_byte_delta": doc.get("pass_byte_delta"), "downgraded": doc.get("downgraded"),
            "validation_codes": doc.get("validation"), "passes": _passes(doc)}


def publish(out: Path, adir: Path, pub: Path, tag: str) -> dict[str, Any]:
    rc, so = cli(["publish", adir, "--store", pub, "--json"], out / "logs" / f"publish-{tag}.log")
    try:
        pj = json.loads(so)
    except ValueError:
        pj = {}
    r = {"exit": rc, "artifact_id": pj.get("artifact_id"), "artifact_dir": pj.get("artifact_dir"),
         "status": pj.get("status"), "reason": pj.get("reason") or pj.get("error")}
    r["same_id"] = str(pj.get("artifact_id", "")).removeprefix("sha256:") == adir.name
    r["cache_sc_equal"] = bool(pj.get("artifact_dir")) and sha256(
        Path(pj["artifact_dir"]) / "cache.sc") == sha256(adir / "cache.sc")
    return r


def refusal_counts(passes: list[dict[str, Any]], name: str = "specialize-returns"
                   ) -> dict[str, int]:
    for p in passes:
        if p["pass"] == name:
            return {k.split(":", 1)[1]: v for k, v in p["stats"].items()
                    if k.startswith("refused:")}
    return {}


def static_rows_ok(val: dict[str, Any], semantic_table: bool, n_spec: int) -> tuple[bool, str]:
    """V1-V21 and V23 pass on a semantic-extension artifact; V9 and V22 skip (V22: WP23 not approved);
    V14 may skip only when the artifact has no SPEC link. A pipeline whose passes made no
    B3 edit is packaged with public records only (design §9.6 "Identity"): V1-V9 pass there."""
    codes = val["codes"]
    if val["verdict"] != "accepted":
        return False, "not accepted"
    if not semantic_table:
        ok = all(codes.get(f"V{i}") == "pass" for i in (1, 2, 3, 4, 5, 6, 7, 9)) and \
            codes.get("V8") in ("pass", "skip")
        return ok, "public records (no B3 edit): V1-V7, V9 pass, V8 pass|skip"
    skip_ok = set(WANT_SKIP) | ({"V14"} if n_spec == 0 else set())
    ok = all(codes.get(c) == "pass" for c in STATIC_ROWS if c not in skip_ok) and \
        all(codes.get(c) in ("pass", "skip") for c in skip_ok)
    return ok, "semantic extension: V1-V21, V23 pass; V9, V22 skip" + ("; V14 skip (no SPEC link)"
                                                       if n_spec == 0 else "")


def exit_reason_rows(out: Path, b: dict[str, Any], cfg: str) -> dict[str, list[dict[str, Any]]]:
    """``scopt return-edges analyze`` of the base: per return-symbol exit, the unit start and
    the §3.1 reason codes (the pass's ``refused:`` counters name only the first reason)."""
    f = out / "return-edges" / b["mode"] / f"{cfg}.json"
    if not f.is_file():
        args = ["return-edges", "analyze", b["snapshot"], "--prep", b["prep"], "--elf", b["elf"],
                "--out", f]
        if b.get("profile"):
            args += ["--profile", b["profile"]]
        cli(args, out / "logs" / f"return-edges-{b['mode']}-{cfg}.log")
    doc = rd(f) or {"exits": []}
    from scopt.importer.import_cache import open_snapshot

    starts = {u.uid: (u.vaddrs[0] & ~1 if u.vaddrs else None)
              for u in open_snapshot(b["snapshot"]).units()}
    elf = b3conf.BY_NAME[cfg].elf
    want = {s for s, _ in FIXTURES[cfg].exit_reasons} | ({"f_b3_20_ret"} if cfg == "20" else set())
    res: dict[str, list[dict[str, Any]]] = {}
    for s_ in want:
        va = sym(s_, elf)
        res[s_] = [{"uid": e["uid"], "start": starts.get(e["uid"]), "admitted": e["admitted"],
                    "reasons": [x["code"] for x in e["reasons"]]}
                   for e in doc["exits"] if int(e["exit_vaddr"], 16) == va]
    return res


def static_one(out: Path, mode: str, cfg: str) -> dict[str, Any]:
    import wp21_integ as W
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    fx = FIXTURES[cfg]
    b = base_info(out, mode, cfg)
    if b is None:
        return {"ok": False, "why": "no base"}
    tag = f"{mode}-{cfg}"
    store = out / "store" / mode / cfg
    rmtree(store)
    r = optimize_one(out, b, tag, store, params=FIXTURE_PARAMS)
    r["fixture"] = fx.fixture
    r["refused_specialize_returns"] = refusal_counts(r.get("passes") or [])
    if r.get("artifact_dir"):
        adir = Path(r["artifact_dir"])
        ri = rd(b["ri"])
        lay = find_envlayout(ri["qemu_sha256"])
        in_vaddrs = [u.vaddrs[0] if u.vaddrs else None
                     for u in open_snapshot(b["snapshot"]).units()]
        vd = W.variant_diff(adir / "cache.sc", lay.offset("regs[0]"), in_vaddrs)
        r["version"] = vd["version"]
        r["semantic_table"] = vd["semantic_table"]
        r["slots"] = vd["slots"]
        r["variants"] = [{k: v.get(k) for k in ("index", "c_vaddr", "size_equal", "relocations",
                                                 "patches", "sites")} for v in vd["variants"]]
        r["validation"] = W._validation(adir)
        spec = [s for s in vd["slots"] if s["kind"] == "SPEC"]
        r["n_spec"], r["n_general"] = len(spec), len(vd["slots"]) - len(spec)
        r["static_rows_ok"], r["static_rows_rule"] = static_rows_ok(r["validation"], vd["semantic_table"],
                                                                    len(spec))
        r["v22"] = r["validation"]["codes"].get("V22")
        want_vc = {sym(v, b3conf.BY_NAME[cfg].elf) for v in fx.vc}
        r["spec_to_vc"] = sorted({s["target_vaddr"] for s in spec
                                  if int(s["target_vaddr"], 16) in want_vc})
        r["publish"] = publish(out, adir, out / "pubstore" / mode / cfg, tag)
    else:
        r["static_rows_ok"] = False
    # fixture expectations (§9.5)
    items: dict[str, Any] = {"published": r.get("status") == "published",
                             "static_rows": bool(r.get("static_rows_ok")),
                             "publish_verified": bool((r.get("publish") or {}).get("exit") == 0 and
                                                      (r.get("publish") or {}).get("same_id"))}
    if fx.spec:
        items["spec_link_to_vc"] = len(r.get("spec_to_vc") or []) == len(fx.vc)
    for x in fx.refusals:
        items[f"refused:{x}"] = (r["refused_specialize_returns"].get(x) or 0) > 0
    if fx.exit_reasons or cfg == "20":
        r["exit_reasons"] = exit_reason_rows(out, b, cfg)
        for s_, code in fx.exit_reasons:
            rows_ = r["exit_reasons"].get(s_) or []
            items[f"exit {s_}: {code}"] = bool(rows_) and all(code in x["reasons"] for x in rows_)
            starts = {x["start"] for x in rows_}
            items[f"no SPEC slot from {s_}'s unit"] = not any(
                s["src_vaddr"] and int(s["src_vaddr"], 16) in starts
                for s in (r.get("slots") or []) if s["kind"] == "SPEC")
    if cfg == "20":
        # the suffix units of f_b3_20 (split by an interrupt inside the window) get no SPEC
        # link (fact_not_local_to_return_tb), whether or not they are in the snapshot
        f20 = sym("f_b3_20")
        suf = [x for x in r["exit_reasons"].get("f_b3_20_ret", []) if x["start"] != f20]
        r["f_b3_20_suffix_units"] = len(suf)
        items["SPEC only from the full unit f_b3_20"] = all(
            int(s["src_vaddr"], 16) == f20 for s in (r.get("slots") or [])
            if s["kind"] == "SPEC" and int(s["target_vaddr"], 16) == sym("vc_b3_20"))
        items["suffix units refused (fact_not_local_to_return_tb) when present"] = (
            not suf or (r["refused_specialize_returns"].get("fact_not_local_to_return_tb")
                        or 0) > 0)
    r["items"] = items
    r["ok"] = all(items.values())
    log("artifact", tag, r.get("status"), r.get("reason"), "spec", r.get("n_spec"), "gen",
        r.get("n_general"), "rows", r.get("static_rows_ok"), "refused",
        r["refused_specialize_returns"], "ok", r["ok"])
    return r


def step_artifacts(out: Path, cfgs: list[str] | None = None) -> dict[str, Any]:
    ensure_selftest(out)
    prev = rd(out / "artifacts.json") or {"rows": {}}
    todo = [(m, c) for m in MODES for c in (cfgs or list(FIXTURES))]
    res = pmap(lambda mc: static_one(out, *mc), todo)
    for (m, c), r in zip(todo, res):
        prev["rows"][f"{m}/{c}"] = r
    rows = prev["rows"]
    prev["n"] = len(rows)
    prev["n_published"] = sum(1 for r in rows.values() if r.get("status") == "published")
    prev["all_static_rows_ok"] = all(r.get("static_rows_ok") for r in rows.values()
                                     if r.get("status") == "published")
    dump(out / "artifacts.json", prev)
    return prev


# ---------------------------------------------------------------------------
# dynamic (O1/O2a/O2b/O3/O5, both modes)
# ---------------------------------------------------------------------------

def published_dir(out: Path, mode: str, cfg: str) -> Path | None:
    a = ((rd(out / "artifacts.json") or {}).get("rows") or {}).get(f"{mode}/{cfg}") or {}
    p = (a.get("publish") or {}).get("artifact_dir")
    return Path(p) if p and a.get("status") == "published" else None


def probe_rows(sc: Path) -> list[dict[str, Any]]:
    from scopt.run.diag import artifact_probe_addrs

    return artifact_probe_addrs(sc)


def general_tc_of(sc: Path, vaddr_rows: list[dict[str, Any]]) -> dict[str, int]:
    """host address of the general C record of every variant (label -> addr)."""
    from scopt.formats.sc_semantic import read_semantic_cache

    f = read_semantic_cache(sc)
    if f.ext is None:
        return {}
    base = f.sc.header.code_address
    by_tb = {f.sc.records[i].tb_offset: f.sc.records[i] for i in f.sc.valid_slots}
    out = {}
    for v in f.ext.variants:
        r = by_tb.get(v.general_tb_offset)
        if r is not None:
            out[f"variant {v.variant_id} general C"] = base + r.tc_offset
    return out


def dyn_targets(sc: Path, slots: list[dict[str, Any]]) -> tuple[list[str], dict[int, str]]:
    """Hit targets: every C' and the F of every SPEC slot. Returns (labels, variant
    index -> C' label)."""
    labs, cl = [], {}
    for p in probe_rows(sc):
        if p["kind"] == "variant":
            labs.append(p["what"])
            cl[p["variant"]] = p["what"]
        elif p["kind"] == "fallback" and "(SPEC" in p["what"]:
            labs.append(p["what"])
    return labs, cl


def fixture_inputs(cfg: str) -> list[str]:
    fx = FIXTURES[cfg]
    return list(fx.inputs) if fx.inputs else b3conf.BY_NAME[cfg].input_names()


def c_prime_labels_for_vc(sc: Path, cfg: str) -> list[str]:
    """C' labels of the variants whose SPEC slot targets one of the fixture's vc symbols."""
    from scopt.formats.sc_semantic import KIND_SPEC, read_semantic_cache

    f = read_semantic_cache(sc)
    if f.ext is None:
        return []
    want = {sym(v, b3conf.BY_NAME[cfg].elf) for v in FIXTURES[cfg].vc}
    out = []
    for lk in f.ext.links:
        s = f.ext.slots[lk.slot_index]
        if s.kind == KIND_SPEC and s.target_vaddr in want:
            out.append(f"variant {f.ext.variants[lk.variant_index].variant_id} C'")
    return sorted(set(out))


def summarize_dyn(doc: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for c in doc.get("cases") or []:
        runs = c.get("runs") or {}
        row: dict[str, Any] = {
            "fixture_input": c.get("fixture_input"), "mode": c.get("mode"),
            "oracles": {o["oracle"]: o["verdict"] for o in c.get("oracles", [])},
            "details": {o["oracle"]: (o.get("detail") or "")[:300]
                        for o in c.get("oracles", []) if o["verdict"] not in ("pass",)},
            "hits": {t["what"]: t.get("hits") for t in ((c.get("hits") or {}).get("targets")
                                                        or [])},
            "runs": {k: {x: v.get(x) for x in ("run_dir", "verdict", "exit_code", "appended",
                                               "deploy_refused")} for k, v in runs.items()}}
        for side in ("artifact", "reference"):
            rdir = (runs.get(side) or {}).get("run_dir")
            if rdir and (Path(rdir) / "run.json").is_file():
                rj = json.loads((Path(rdir) / "run.json").read_text())
                row["runs"][side]["qemu_sha256"] = rj.get("qemu_sha256")
            if rdir and (Path(rdir) / "v6-stats.json").is_file():
                st = json.loads((Path(rdir) / "v6-stats.json").read_text())
                row.setdefault("stats", {})[side] = {
                    k: st.get(k) for k in ("loaded", "links_armed", "n_links", "n_variants",
                                           "n_slots", "refused", "disarm_tlb_events",
                                           "disarm_tlb_links", "flush_resets",
                                           "variant_exits", "variant_entry_exits",
                                           "variant_restores", "invariant_checks",
                                           "invariant_violations", "activations",
                                           "variants_built") if k in st}
        cases.append(row)
    return {"verdict": doc.get("verdict"), "failures": doc.get("failures"),
            "incomplete": doc.get("incomplete"), "cases": cases}


def dynamic_one(out: Path, mode: str, cfg: str, *, art: Path | None = None, tag: str | None = None,
                oracles: tuple[str, ...] = ("O1", "O2a", "O2b", "O3", "O5"),
                o3: dict[str, Any] | None = None, negative: bool = False,
                hit_labels: list[str] | None = None, inputs: list[str] | None = None,
                test_policies: tuple[str, ...] = ()) -> dict[str, Any]:
    from scopt.run.oracles import OracleRefused, dynamic_oracles

    gi = gate_info()
    b = base_info(out, mode, cfg)
    art = art or published_dir(out, mode, cfg)
    if b is None or art is None:
        return {"ok": False, "why": "no base or no published artifact"}
    tag = tag or f"{mode}-{cfg}"
    od = out / "dyn" / tag
    rmtree(od)
    ins = inputs or fixture_inputs(cfg)
    st = rd(out / "artifacts.json") or {}
    srow = (st.get("rows") or {}).get(f"{mode}/{cfg}") or {}
    labels, _cl = dyn_targets(art / "cache.sc", srow.get("slots") or [])
    if hit_labels is not None:
        labels = hit_labels
    if o3 is None:
        o3 = {"stats": {}, "hits": {}}
        if FIXTURES[cfg].spec and not negative:
            o3["stats"]["links_armed"] = ">0"
            for lab in c_prime_labels_for_vc(art / "cache.sc", cfg):
                o3["hits"][lab] = ">0"
        for md, k, req in FIXTURES[cfg].stats:
            if md in ("*", mode) and not negative:
                o3["stats"][k] = req
        if not o3["stats"] and not o3["hits"]:
            o3["stats"]["invariant_violations"] = "==0"
    t0 = time.time()
    try:
        doc = dynamic_oracles(
            art, [b["toml"]], od, oracles=oracles, baseline=b["snapshot"], qemu=gi["exe"],
            timeout_s=300.0, inputs=[FIX / "inputs" / f"{i}.bin" for i in ins],
            mode=mode, fixture_expected=FIX / "expected.py", fixture_inputs=ins,
            o3_requirements=o3, hit_targets=labels or None, allow_negative_control=negative,
            test_policies=test_policies)
    except OracleRefused as e:
        return {"ok": False, "refused": str(e)}
    r = summarize_dyn(doc)
    r.update(tag=tag, artifact=str(art), o3_requirements=o3, hit_labels=labels,
             seconds=round(time.time() - t0, 1), file=str(od / "dynamic_oracles.json"))
    r["gate_binary"] = all(str(((c["runs"].get(s) or {}).get("qemu_sha256") or "")).endswith(
        gi["sha"]) for c in r["cases"] for s in ("artifact", "reference"))
    r["ok"] = r["verdict"] == "pass" and r["gate_binary"]
    log("dynamic", tag, r["verdict"], [c["oracles"] for c in r["cases"]], "hits",
        [c["hits"] for c in r["cases"]], "ok", r["ok"])
    return r


def extra_hits(out: Path, mode: str, cfg: str, dyn: dict[str, Any]) -> dict[str, Any] | None:
    """F-B3-6: hits of the general C (the ``bx r3`` arbitrary entry and the f5 path reach it
    without R's SPEC link)."""
    from scopt.run.diag import gdb_hits

    if cfg != "6" or not dyn.get("cases"):
        return None
    art = Path(dyn["artifact"])
    tg = general_tc_of(art / "cache.sc", [])
    res = {}
    for c in dyn["cases"]:
        rdir = (c["runs"].get("artifact") or {}).get("run_dir")
        if not rdir:
            continue
        d = out / "dyn-extra" / f"{mode}-{cfg}-{c['fixture_input']}"
        rmtree(d)
        h = gdb_hits(rdir, d, sorted(tg.items()), timeout_s=300)
        res[c["fixture_input"]] = {t["what"]: t["hits"] for t in h["targets"]}
        res[c["fixture_input"]]["ok"] = h["ok"]
    return res


def step_dynamic(out: Path, cfgs: list[str] | None = None) -> dict[str, Any]:
    prev = rd(out / "dynamic.json") or {"rows": {}}
    todo = [(m, c) for m in MODES for c in (cfgs or list(FIXTURES))
            if published_dir(out, m, c) is not None]

    def one(mc):
        m, c = mc
        r = dynamic_one(out, m, c)
        r["extra_hits"] = extra_hits(out, m, c, r)
        return r
    res = pmap(one, todo)
    for (m, c), r in zip(todo, res):
        prev["rows"][f"{m}/{c}"] = r
    rows = prev["rows"]
    prev["n"] = len(rows)
    prev["n_pass"] = sum(1 for r in rows.values() if r.get("ok"))
    dump(out / "dynamic.json", prev)
    log("dynamic", prev["n_pass"], "/", prev["n"])
    return prev


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

STEPS = ["gate", "bases", "artifacts", "dynamic", "gdbdiag", "stale", "o4", "mutants",
         "roundtrip", "compute", "verdict"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "runs/wp28")
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--configs", default="", help="comma list (artifacts/dynamic only)")
    ap.add_argument("--ref", type=Path, default=ROOT / "runs/wp28",
                    help="compare-ref / carry / compute_static: the reference gate run")
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cfgs = [c for c in a.configs.split(",") if c] or None
    import wp28_steps as S2  # the later steps (gdbdiag, stale, o4, mutants, ...)

    S2.REF = a.ref.resolve()

    for st in a.steps.split(","):
        t0 = time.time()
        log("== step", st)
        if st == "gate":
            step_gate(out)
        elif st == "bases":
            step_bases(out)
        elif st == "artifacts":
            step_artifacts(out, cfgs)
        elif st == "dynamic":
            step_dynamic(out, cfgs)
        else:
            getattr(S2, "step_" + st.replace("-", "_"))(out)
        log("== step", st, "done", round(time.time() - t0, 1), "s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
