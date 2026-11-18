#!/usr/bin/env python3
"""WP29: the B4 gate (design §11 "B4 gate", plan ``## WP29``) on the frozen gate binary.

    cd wt/scopt-wp29
    PYTHONPATH=$PWD python tests/qemu/wp29_gate.py --out runs/wp29 [steps...]

Every dynamic item runs on the frozen gate binary ``qint-v6-gate-1`` (exe sha256 in
``runs/wp22/gate-binary.json``, re-checked by the ``gate`` step and by every run's
``run.json``). All bases are re-collected on that binary (plan Ground rules, D-EV-2):
the fixture bases by ``tests/qemu/wp20_fixture_snapshots.py --qemu <gate>`` into
``OUT/fx`` and the compute base by the ``compute-base`` step.

Steps (default: all except ``timing``, which is ``tests/qemu/wp29_timing.sh``):

``gate``          gate binary record: exe sha256, tag, HEAD, clean tree (``OUT/gate.json``).
``compute-base``  compute (FreeRTOS MPS2 compute) base on the gate binary: TOML rendered
                  with ``qemu_path`` = gate, ``scopt prepare`` (train inputs), bundle and
                  runtime identity, import, ``scopt admission identity`` (v2), census.
``package``       B4 artifacts (D1|D2|D3) of every fixture base (F-B4-1..7, 9, 10; icount
                  and noicount) + the static fixture checks (§9.5); compute: B4 all
                  classes, the D1/D2/D3 ablations (T-B4-11) and the ``icount-entry-sfb``
                  baseline-fix artifact; V12 + host_abs on every rewritten compute unit.
``dynamic``       O1/O2a/O2b/O5 (+ O6 for F-B4-10) of every fixture artifact against its
                  warm base, with the fixture's per-mode oracle; non-vacuity (gdb hardware
                  breakpoint counts on rewritten units); F-B4-4 ``cpu_io_recompile``;
                  F-B4-7 ``events.tsv``; F-B4-9 ``counts.txt`` and no ``cpu_abort``.
``mutants``       every B4 mutant kind on every icount fixture base and on compute,
                  packaged through ``package_b4`` (must be rejected statically); O4: for
                  every kind with a dynamic detector, negative controls (bypass V12-V17)
                  on the detector fixture, searched hottest-unit first until one is
                  detected (at most ``MAX_O4_TRIES`` per kind; every attempt recorded).
``compute``       V10 (held-out inputs 1-3, icount) of the compute artifacts on the gate.
``verdict``       ``OUT/verdict.json`` (gate items 1-5).

Close-out re-gate (design/closeout-logreg.md C.2; ``--out runs/closeout/b4 --ref runs/wp29``):
the WP29 bases are referenced read-only (``OUT/fx``, ``OUT/fx-b3`` and ``OUT/compute`` hold
copies of their ``snap.json`` / ``base.json``; F-B4-11's base is the close-out
qualification base). Extra steps: ``census`` (T-B4-1 census re-run on the existing compute
base), ``pytest-static`` (T-B4-1/2/2b static tests), ``compare-ref`` (every output
``cache.sc`` against ``--ref``; only F-B4-10/F-B4-11 may change) and ``carry-timing``
(``--ref``'s timing evidence copied only when every timed arm is byte-identical). F-B4-11
(``b4-11``) is the ``flag_store_before_helper`` detector: ``o4-extra`` runs every instance
there (icount gated, noicount reported) and checks the predicted divergence.

Never timed except ``wp29_timing.sh`` (holds ``runs/.timing.lock``). Every QEMU run goes
through the WP8/WP26 launcher (``setarch x86_64 -R``, timeout, fresh run directory,
``supercache_flat = false``); gdb is always ``gdb -nx -batch`` with cwd in a run dir.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FX = ROOT / "fixtures" / "b3"
GATE = ROOT / "wt/qemu-v6/build/qemu-system-arm"
GATE_JSON = ROOT / "runs/wp22/gate-binary.json"
WT = Path(__file__).resolve().parents[2]
PY = sys.executable
CONFIGS = ["b4-1", "b4-2", "b4-3", "b4-4", "b4-5", "b4-6", "b4-7", "b4-9", "b4-10", "b4-11"]
MODES = ["icount", "noicount"]
KERNEL_LABEL = {"b4-1": "b4_1_ld", "b4-2": "b4_2_ld", "b4-3": "b4_3_ld", "b4-4": "b4_4_mmio",
                "b4-5": "b4_5_ld", "b4-6": "b4_6_sdiv", "b4-7": "b4_7_ld", "b4-9": "b4_9_a",
                "b4-10": "b4_1_ld", "b4-11": "b4_11_a"}
#: close-out (design/closeout-logreg.md B.2/B.3): F-B4-11 is the flag_store_before_helper
#: detector (variant image ``b3_b411.elf``; its base comes from the close-out qualification
#: run, ``runs/closeout/fixture-b4-11/base``). It is not an item-2 fixture (verdict_wp29
#: FIXTURES_ITEM2); its real B4 artifact is a reported row (B.3 step 5).
B411 = "b4-11"
B411_SUBPHASE_SYMS = {"b4_11_a": "A", "b4_11_b": "B", "b4_11_c": "C"}
REPS = {"icount": 2, "noicount": 4}
COMPUTE_TEMPLATE = ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml"
TRAIN = [ROOT / f"fixtures/inputs/train/freertos-mps2-compute/train-{i}.bin" for i in (1, 2, 3)]
HELDOUT = [ROOT / f"fixtures/inputs/heldout/freertos-mps2-compute/heldout-{i}.bin"
           for i in (1, 2, 3)]
COMPUTE_ARMS = [("b4", "D1|D2|D3"), ("b4-d1", "D1"), ("b4-d2", "D2"), ("b4-d3", "D3")]
MAX_O4_TRIES = 6

sys.path.insert(0, str(FX))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import b3conf  # noqa: E402
from wp25_integ import (gdb_count_symbol, rewritten_probes, static_checks,  # noqa: E402
                        unit_pcs, update_mem_mutants)
import wp25_integ  # noqa: E402

wp25_integ.KERNEL_LABEL.update(KERNEL_LABEL)
_sym_b3 = wp25_integ.sym


def sym(name: str) -> int:
    """A guest symbol of ``b3.elf`` (``b3.nm``); names that exist only in the F-B4-11
    variant image (``b4_11_*``) are resolved in ``b3_b411.elf``."""
    try:
        return _sym_b3(name)
    except KeyError:
        return b3conf.symbols(b3conf.BY_NAME[B411].elf)[name] & ~1


wp25_integ.sym = sym  # rewritten_probes / static_checks resolve kernel labels through it


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dump(p: Path, doc: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")


def rd(p: Path) -> Any:
    return json.loads(p.read_text()) if p.is_file() else None


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def rmtree(d: Path) -> None:
    """Remove ``d`` (published artifacts are read-only: make them writable first)."""
    if not d.exists():
        return
    subprocess.run(["chmod", "-R", "u+w", str(d)], check=True)
    shutil.rmtree(d)


def fresh(d: Path) -> Path:
    if d.exists():
        rmtree(d)
    d.mkdir(parents=True)
    return d


def selftest(out: Path) -> dict[str, Any]:
    from scopt.validate_sym.selftest import load_or_run

    return load_or_run(str(out / "selftest.json"))


def run_qemu_shas(doc: dict[str, Any]) -> list[str]:
    """``qemu_sha256`` of every run (artifact and reference) of a dynamic_oracles doc."""
    qs = set()
    for c in doc.get("cases") or []:
        for side in ("artifact", "reference"):
            rdir = (c.get("runs") or {}).get(side, {}).get("run_dir")
            if rdir and (Path(rdir) / "run.json").is_file():
                qs.add(str(json.loads((Path(rdir) / "run.json").read_text()).get("qemu_sha256")))
    return sorted(qs)


# ---------------------------------------------------------------------------
# gate binary
# ---------------------------------------------------------------------------

def step_gate(out: Path) -> dict[str, Any]:
    g = json.loads(GATE_JSON.read_text())
    wt = Path(g["worktree"])
    sha = sha_file(GATE)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    tag = subprocess.run(["git", "-C", str(wt), "rev-parse", "qint-v6-gate-1^{commit}"],
                         capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True,
                           text=True).stdout.strip()
    doc = {"exe": str(GATE), "exe_sha256": sha, "recorded_sha256": g["exe_sha256"],
           "tag": "qint-v6-gate-1", "tag_commit": tag, "head": head,
           "recorded_commit": g["commit"], "tree_clean": dirty == "",
           "ok": sha == g["exe_sha256"] and head == tag == g["commit"] and dirty == "",
           "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    dump(out / "gate.json", doc)
    log(f"gate: sha {sha[:16]} tag {tag[:10]} head {head[:10]} clean {dirty == ''} -> {doc['ok']}")
    if not doc["ok"]:
        raise SystemExit("gate binary check failed")
    return doc


# ---------------------------------------------------------------------------
# bases
# ---------------------------------------------------------------------------

def snap_info(out: Path, mode: str, cfg: str) -> dict[str, Any]:
    return json.loads((out / "fx" / mode / cfg / "snap.json").read_text())


def load_ri(snapshot: Path, config_text: str) -> tuple[Any, Any, dict[str, Any]]:
    from scopt.formats.bundle_v1 import (admission_facts, extend_runtime_identity,
                                         read_runtime_identity)
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    s = open_snapshot(snapshot)
    ri = read_runtime_identity(snapshot / "runtime_identity.json")
    lay = find_envlayout(ri["qemu_sha256"])
    man = json.loads((snapshot / "manifest.json").read_text())
    adm = admission_facts(manifest=man, qemu_sha256=ri["qemu_sha256"], envlayout=lay,
                          config_text=config_text)
    return s, lay, extend_runtime_identity(ri, adm)


def load(out: Path, mode: str, cfg: str):
    sj = snap_info(out, mode, cfg)
    s, lay, ri2 = load_ri(Path(sj["snapshot"]), Path(sj["base_toml"]).read_text())
    return sj, s, lay, ri2


def unit_entries(profile: str | None) -> dict[str, int]:
    """uid -> entries summed over phases (train profile of the base snapshot)."""
    if not profile or not Path(profile).is_file():
        return {}
    p = json.loads(Path(profile).read_text())
    out: dict[str, int] = {}
    for u in p["units"]:
        out[u["unit"]] = sum(int(c.get("entries") or 0) for c in (u.get("phase") or {}).values()
                             if isinstance(c, dict))
    return out


def _scopt(args: list[str], logf: Path, timeout: float = 1800) -> tuple[int, str]:
    logf.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    p = subprocess.run([PY, "-m", "scopt", *[str(a) for a in args]], cwd=str(WT),
                       capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "PYTHONPATH": str(WT)})
    logf.write_text("$ python -m scopt " + " ".join(str(a) for a in args) + "\n" + p.stdout +
                    "\n--- stderr ---\n" + p.stderr +
                    f"\nexit={p.returncode} seconds={time.monotonic() - t0:.1f}\n")
    return p.returncode, p.stdout


def compute_toml(out: Path) -> Path:
    """The compute TOML with ``[Machine].qemu_path`` = the gate binary (a TOML qemu_path
    wins over FASTDYN_QEMU inside FastDyn; WP26 finding)."""
    t = COMPUTE_TEMPLATE.read_text()
    new, n = re.subn(r'^qemu_path\s*=\s*".*"$', f'qemu_path = "{GATE}"', t, flags=re.M)
    if n != 1:
        raise SystemExit("compute template: qemu_path line not found exactly once")
    p = out / "compute" / "configs" / "compute-sc-gate.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file() or p.read_text() != new:
        p.write_text(new)
    return p


def step_compute_base(out: Path) -> dict[str, Any]:
    d = out / "compute"
    toml = compute_toml(out)
    prep = d / "prep"
    if prep.exists():
        raise SystemExit(f"{prep} exists (fresh base required)")
    logs = d / "logs"
    doc: dict[str, Any] = {"config": str(toml), "config_sha256": sha_file(toml),
                           "qemu": str(GATE), "qemu_sha256": sha_file(GATE),
                           "train_inputs": [str(i) for i in TRAIN]}
    rc, _ = _scopt(["prepare", "-c", toml, "--out", prep, "--qemu", GATE, "--timeout", "300",
                    "--json", *sum((["--inputs", str(i)] for i in TRAIN), [])],
                   logs / "01-prepare.log")
    pj = rd(prep / "prep.json") or {}
    doc.update(prepare_rc=rc, prep_verdict=pj.get("verdict"), sc=pj.get("sc"),
               records=[r.get("records_after") for r in pj.get("runs", [])])
    if rc != 0 or pj.get("verdict") != "PASS":
        dump(d / "base.json", doc)
        raise SystemExit("compute prepare failed")
    rc1, _ = _scopt(["bundle", "identity", "-c", prep / "run.toml", "--manifest",
                     pj["manifest"], "--out", d / "bundle.json"], logs / "02-bundle.log")
    rc2, _ = _scopt(["bundle", "runtime-identity", "--run-dir", prep / "oracle", "--sc", pj["sc"],
                     "--scm", prep / "capture.scm", "--out", d / "runtime_identity.json"],
                    logs / "03-runtime-identity.log")
    rc3, _ = _scopt(["import", prep, "--bundle", d / "bundle.json", "--runtime-identity",
                     d / "runtime_identity.json", "--store", d / "store"], logs / "04-import.log")
    snaps = sorted((d / "store" / "snapshots").iterdir()) \
        if (d / "store" / "snapshots").is_dir() else []
    doc.update(bundle_rc=rc1, runtime_identity_rc=rc2, import_rc=rc3)
    if rc1 or rc2 or rc3 or len(snaps) != 1:
        dump(d / "base.json", doc)
        raise SystemExit("compute bundle/import failed")
    snap = snaps[0]
    doc["snapshot"] = str(snap)
    rc4, _ = _scopt(["admission", "identity", "--snapshot", snap, "--config", toml,
                     "--out", d / "runtime_identity.v2.json"], logs / "05-admission.log")
    doc["admission_rc"] = rc4
    ri2 = rd(d / "runtime_identity.v2.json") or {}
    doc["admission"] = ri2.get("admission")
    rc5, cen = _scopt(["b4", "census", snap, "--runtime", d / "runtime_identity.v2.json"],
                      logs / "06-census.log")
    rc6, cen_all = _scopt(["b4", "census", snap, "--runtime", d / "runtime_identity.v2.json",
                           "--all-units"], logs / "07-census-all.log")
    rc7, cen_tcg = _scopt(["b4", "census", snap, "--runtime", d / "runtime_identity.v2.json",
                           "--all-units", "--mode", "tcg"], logs / "08-census-tcg.log")
    doc["census_rc"] = [rc5, rc6, rc7]
    for k, v in (("census_admitted", cen), ("census_all_units", cen_all),
                 ("census_tcg_m0", cen_tcg)):
        try:
            doc[k] = json.loads(v)
        except ValueError:
            doc[k] = v[-2000:]
    dump(d / "base.json", doc)
    log(f"compute base: {snap.name[:16]} admission rc {rc4}")
    return doc


# ---------------------------------------------------------------------------
# package
# ---------------------------------------------------------------------------

def b4_9_static(s: Any, prop: Any, lay: Any, ri2: dict[str, Any]) -> dict[str, Any]:
    """F-B4-9: in the TB holding b4_9_a (chains by goto_tb into b4_9_s), the last-insn
    ``can_do_io = 1`` store before the goto_tb exit is kept (tb_start_cb_possible)."""
    from scopt.b4_mutants import with_admission
    from scopt.passes.eliminate_state_sync import propose_b4

    a = sym("b4_9_a")
    rows = []
    # what the same pass would delete if the admission said tb_start_cb_possible = false
    ri_f = with_admission(ri2, tb_start_cb_possible=False,
                          tb_start_cb_reasons=["wp29:counterfactual_false"])
    prop_f = propose_b4(s, layout=lay, runtime=ri_f)
    for u in s.units():
        pcs = unit_pcs(u)
        if not pcs or pcs[0] != a:
            continue
        sd = prop.syncdead.get(u.uid)
        sdf = prop_f.syncdead.get(u.uid)

        # the last-insn can_do_io = 1 stores the pass would delete under "false"
        tgt = {d.off: d for d in (sdf.dead if sdf is not None else ())
               if d.field == "neg.can_do_io" and d.block == "insn" and d.old[-1:] == b"\x01"}
        kept = [f"{x.off}:{x.reason}" for x in (sd.refused if sd is not None else ())
                if x.off in tgt]
        deleted = [d.off for d in (sd.dead if sd is not None else ()) if d.off in tgt]
        rows.append({"uid": u.uid, "pcs": [hex(p) for p in pcs],
                     "status": None if sd is None else sd.status,
                     "counterfactual_false_deleted": [f"{o}:{d.text}" for o, d in tgt.items()],
                     "cdio_true_kept": kept, "cdio_true_deleted": deleted})
    ok = bool(rows) and all(r["status"] == "ok" and r["cdio_true_kept"] and
                            not r["cdio_true_deleted"] for r in rows)
    return {"check": "F-B4-9: the last-insn can_do_io = 1 store of TB A (b4_9_a, goto_tb into "
                     "b4_9_s) is kept under tb_start_cb_possible = true",
            "tb_start_cb_possible": ri2["admission"]["tb_start_cb_possible"],
            "tb_start_cb_reasons": ri2["admission"].get("tb_start_cb_reasons"),
            "units": rows, "pass": ok and ri2["admission"]["tb_start_cb_possible"] is True}


def b411_instance(s: Any, lay: Any, m: Any) -> tuple[str | None, str | None]:
    """(sub-phase A/B/C, flag N/Z/C/V) of a ``flag_store_before_helper`` instance on the
    F-B4-11 base: the TB is named by the sub-phase label it starts at, the flag by the
    stored env field (HostOps ``store_field``). ``None`` when either is not resolved."""
    from scopt.analysis.hostops import build_hostops

    starts = {sym(k): v for k, v in B411_SUBPHASE_SYMS.items()}
    u = s.unit(m.uid)
    sub = next((starts[v & ~1] for v in (u.vaddrs or ()) if (v & ~1) in starts), None)
    o = build_hostops(u, lay, s.header.code_address).op_at(m.patches[0].host_off) \
        if m.patches else None
    fld = str(getattr(o, "store_field", "") or "")
    return sub, (fld[:1] if fld[:1] in ("N", "Z", "C", "V") and fld.endswith("F") else None)


def b4_11_static(s: Any, prop: Any, lay: Any, ri2: dict[str, Any]) -> dict[str, Any]:
    """F-B4-11 real-pass row (design/closeout-logreg.md B.3 step 5): every
    ``flag_store_before_helper`` instance (an NZCV store live at a flags-0 helper or plugin
    call that a reader after it observes) is kept by the real B4 pass, with the refusal
    ``observed_at_sync_point``; none is deleted."""
    from scopt.b4_mutants import find_mutants

    rows = []
    for m in find_mutants(s, lay, ri2, per_kind=10 ** 6):
        if m.kind != "flag_store_before_helper" or not m.patches:
            continue
        off = m.patches[0].host_off
        sub, flag = b411_instance(s, lay, m)
        sd = prop.syncdead.get(m.uid)
        deleted = any(p.host_off == off for p in prop.patches.get(m.uid, ())) or (
            sd is not None and any(d.off == off for d in sd.dead))
        reasons = sorted({x.reason for x in (sd.refused if sd is not None else ())
                          if x.off == off})
        rows.append({"uid": m.uid, "host_off": off, "sub": sub, "flag": flag,
                     "note": m.note, "syncdead_status": None if sd is None else sd.status,
                     "deleted": deleted, "kept_reasons": reasons,
                     "ok": not deleted and "observed_at_sync_point" in reasons})
    cover = sorted({f"{r['sub']}-{r['flag']}" for r in rows if r["sub"] and r["flag"]})
    return {"check": "F-B4-11: every flag_store_before_helper instance is kept by the real "
                     "B4 pass (observed_at_sync_point)",
            "instances": len(rows), "subphase_flags": cover, "units": rows,
            "pass": bool(rows) and all(r["ok"] for r in rows)}


def package_one(s: Any, lay: Any, ri2: dict[str, Any], store: Path, cid: str, st: Any,
                classes: str | None = None) -> dict[str, Any]:
    from scopt.passes.eliminate_state_sync import class_ledger_rows, package_b4, propose_b4

    t0 = time.monotonic()
    prop = propose_b4(s, layout=lay, runtime=ri2, classes=classes)
    row: dict[str, Any] = {
        "snapshot_id": s.snapshot_id, "units": len(list(s.units())), "classes": classes,
        "admission": {k: ri2["admission"].get(k) for k in
                      ("tb_start_cb_possible", "tb_start_cb_reasons",
                       "mem_get_value_outside_mem_cb")},
        "by_class": prop.by_class(), "units_edited": len(prop.patches),
        "outcome": prop.result.outcome.value, "reason": prop.result.reason,
        "stats": dict(prop.result.stats),
        "patched_units": {u: dict(Counter(p.justification.split(":", 1)[0] for p in v))
                          for u, v in prop.patches.items()},
        "ledger": [r.to_json() for r in class_ledger_rows(prop).rows]}
    if prop.patches:
        r = package_b4(s, prop, store, candidate_id=cid, layout=lay, runtime=ri2, selftest=st)
        row.update(status=r.status, pkg_reason=r.reason, artifact_id=r.artifact_id,
                   artifact_dir=r.artifact_dir)
        if r.validation is not None:
            v = r.validation
            row["rows"] = dict(Counter(f"{x['code']}:{x['result']}" for x in v.rows))
            row["v12_pass"] = sum(1 for x in v.rows if x["code"] == "V12"
                                  and x["result"] == "pass")
            row["failed"] = [x for x in v.rows if x["result"] == "fail"][:5]
        if r.artifact_dir:
            side = json.loads((Path(r.artifact_dir) / "cache.sc.scopt.json").read_text())
            row["loader_requirement"] = side["loader_requirement"]
            row["cache_sc_sha256"] = sha_file(Path(r.artifact_dir) / "cache.sc")
    else:
        row["status"] = "no_change"
    row["seconds"] = round(time.monotonic() - t0, 1)
    return row, prop


def v12_host_abs(art_dir: Path, patched_units: dict[str, Any]) -> dict[str, Any]:
    """Gate item 3: V12 (hostsym trace equivalence incl. GuestAccess with the S5 env,
    HostStore and calls) and the host_abs assertion on every rewritten unit."""
    v = json.loads((art_dir / "validation.json").read_text())
    rows = v["rows"] if isinstance(v, dict) else v
    v12 = [r for r in rows if r.get("code") == "V12"]
    units_v12 = {}
    for r in v12:
        subj = str(r.get("subject") or r.get("unit") or r.get("check") or "")
        units_v12.setdefault(r.get("result"), []).append(subj)
    text = json.dumps(v12)
    covered = [u for u in patched_units if u in text]
    host_abs_fail = [r for r in rows if "host_abs" in json.dumps(r) and r.get("result") == "fail"]
    return {"v12_rows": len(v12), "v12_by_result": {k: len(x) for k, x in units_v12.items()},
            "rewritten_units": len(patched_units), "rewritten_units_named_in_v12": len(covered),
            "host_abs_fail_rows": len(host_abs_fail),
            "host_abs_rows": sum(1 for r in rows if "host_abs" in json.dumps(r)),
            "pass": bool(v12) and all(r.get("result") == "pass" for r in v12)
            and len(covered) == len(patched_units) and not host_abs_fail}


def step_package(out: Path, only: list[str] | None = None) -> dict[str, Any]:
    st = selftest(out)
    rows = []
    old = (rd(out / "package.json") or {}).get("rows", []) if only else []
    for mode in MODES:
        for cfg in CONFIGS:
            if only and cfg not in only and f"{mode}/{cfg}" not in only:
                continue
            sj, s, lay, ri2 = load(out, mode, cfg)
            d = out / "art" / mode / cfg
            if d.exists():
                rmtree(d)
            d.mkdir(parents=True)
            dump(d / "runtime_identity.v2.json", ri2)
            row, prop = package_one(s, lay, ri2, d / "store", f"wp29-{mode}-{cfg}", st)
            row.update(mode=mode, config=cfg, snapshot=sj["snapshot"],
                       fixture=b3conf.BY_NAME[cfg].fixture)
            row["static"] = static_checks(cfg, s, prop) if cfg in ("b4-4", "b4-5", "b4-7") \
                else {"check": None}
            if cfg == "b4-9":
                row["static"] = b4_9_static(s, prop, lay, ri2)
            if cfg == B411:
                row["static"] = b4_11_static(s, prop, lay, ri2)
            rows.append(row)
            log(f"package {mode} {cfg}: {row['status']} {row['by_class']} units "
                f"{row['units_edited']} static={row['static'].get('pass')} "
                f"v12_pass={row.get('v12_pass')}")
    keep = [r for r in old if not any(r["mode"] == x["mode"] and r["config"] == x["config"]
                                      for x in rows)]
    doc = {"rows": sorted(keep + rows, key=lambda r: (r["mode"], r["config"]))}
    dump(out / "package.json", doc)
    return doc


def sfb_row(out: Path, rc: int | None) -> dict[str, Any]:
    """The ``sfb`` row of ``compute/package.json`` from ``scopt sfb package --out``'s JSON
    (it names the artifact directory ``path``)."""
    sfb = rd(out / "compute" / "sfb.json") or {}
    srow = {"label": "sfb", "rc": rc, "status": sfb.get("status"),
            "artifact_dir": sfb.get("artifact_dir") or sfb.get("path"),
            "artifact_id": sfb.get("artifact_id"), "reason": sfb.get("reason"),
            "moved": sfb.get("moved"), "proposal": sfb.get("proposal"),
            "validation_failed": [x for x in (sfb.get("validation") or [])
                                  if x.get("result") == "fail"],
            "validation_rows": len(sfb.get("validation") or [])}
    if srow["artifact_dir"]:
        srow["cache_sc_sha256"] = sha_file(Path(srow["artifact_dir"]) / "cache.sc")
    return srow


def step_sfb_row(out: Path) -> dict[str, Any]:
    """Rebuild the ``sfb`` row of ``compute/package.json`` from ``compute/sfb.json`` and the
    exit status in ``compute/logs/10-sfb.log`` (without re-packaging: the published sfb
    artifact is the timing arm's source)."""
    pk = rd(out / "compute" / "package.json")
    lg = (out / "compute" / "logs" / "10-sfb.log").read_text()
    m = re.findall(r"exit=(-?\d+)", lg)
    srow = sfb_row(out, int(m[-1]) if m else None)
    srow["row_source"] = "wp29_gate.py sfb-row (compute/sfb.json + logs/10-sfb.log)"
    pk["rows"] = [srow if r["label"] == "sfb" else r for r in pk["rows"]]
    dump(out / "compute" / "package.json", pk)
    log(f"sfb row: {srow['status']} {srow['artifact_dir']} rc={srow['rc']}")
    return srow


def step_package_compute(out: Path) -> dict[str, Any]:
    from scopt.passes.icount_entry_sfb import package_sfb  # noqa: F401

    base = rd(out / "compute" / "base.json")
    snap = Path(base["snapshot"])
    toml = Path(base["config"])
    s, lay, ri2 = load_ri(snap, toml.read_text())
    st = selftest(out)
    store = out / "compute" / "store"
    rows = []
    for label, classes in COMPUTE_ARMS:
        row, prop = package_one(s, lay, ri2, store, f"wp29-compute-{label}", st, classes)
        row["label"] = label
        if row.get("artifact_dir"):
            row["v12_host_abs"] = v12_host_abs(Path(row["artifact_dir"]), row["patched_units"])
        rows.append(row)
        log(f"compute {label}: {row['status']} {row['by_class']} units {row['units_edited']} "
            f"v12={row.get('v12_host_abs', {}).get('pass')}")
    # icount-entry-sfb (a baseline TCG fix, not B4, never counted as caching)
    rc, so = _scopt(["sfb", "package", snap, "--store", store, "--candidate-id",
                     "wp29-compute-sfb", "--out", out / "compute" / "sfb.json"],
                    out / "compute" / "logs" / "10-sfb.log", timeout=7200)
    srow = sfb_row(out, rc)
    rows.append(srow)
    log(f"compute sfb: rc {rc} {srow['status']}")
    doc = {"snapshot": str(snap), "config": str(toml), "runtime_identity_admission":
           ri2["admission"], "rows": rows}
    dump(out / "compute" / "package.json", doc)
    return doc


def step_compute_host_abs(out: Path) -> dict[str, Any]:
    """HostOps census of absolute host-pointer operands on compute (gate item 3): every
    unit of the gate compute base, and the units each compute artifact rewrote
    (``patched_units`` of ``compute/package.json``) -> ``compute/host_abs_ops.json``."""
    from scopt.analysis.hostops import OpKind, build_hostops

    base = rd(out / "compute" / "base.json")
    s, lay, _ri2 = load_ri(Path(base["snapshot"]), Path(base["config"]).read_text())
    kinds = (OpKind.HOST_ABS_STORE, OpKind.HOST_ABS_LOAD)
    per_unit: dict[str, Counter] = {}
    for u in s.units():
        h = build_hostops(u, lay, s.header.code_address)
        per_unit[u.uid] = Counter(o.kind.value for o in h.ops if o.kind in kinds)
    allc = sum(per_unit.values(), Counter())
    arms = {}
    for r in (rd(out / "compute" / "package.json") or {}).get("rows", []):
        pu = r.get("patched_units")
        if not pu:
            continue
        missing = sorted(set(pu) - set(per_unit))
        c = sum((per_unit[x] for x in pu if x in per_unit), Counter())
        arms[r["label"]] = {"rewritten_units": len(pu), "units_not_in_base": len(missing),
                            "host_abs_ops": dict(c), "host_abs_total": sum(c.values())}
    doc = {"schema": "wp29/compute-host-abs/1", "snapshot": base["snapshot"],
           "producer": "tests/qemu/wp29_gate.py compute-host-abs",
           "units_all": len(per_unit), "host_abs_ops_all_units": dict(allc),
           "host_abs_total_all_units": sum(allc.values()),
           "units_with_host_abs": sorted(k for k, v in per_unit.items() if v),
           "by_arm": arms,
           # kept for the report/verdict readers of the earlier file ({"units": N})
           "units": (arms.get("b4") or {}).get("rewritten_units")}
    dump(out / "compute" / "host_abs_ops.json", doc)
    log(f"compute host_abs: all {doc['units_all']} units {doc['host_abs_total_all_units']} "
        f"ops; arms { {k: (v['rewritten_units'], v['host_abs_total']) for k, v in arms.items()} }")
    return doc


def step_recheck_compute(out: Path) -> dict[str, Any]:
    """Re-run ``package-compute`` end to end into ``OUT/recheck-package-compute`` (a fresh
    store, so the published arms the timing used are untouched) and compare every row with
    ``OUT/compute/package.json``: status, by_class, rewritten units and the output
    ``cache.sc`` sha256 must be equal (artifact ids differ: they include the store)."""
    o2 = out / "recheck-package-compute"
    rmtree(o2)
    (o2 / "compute").mkdir(parents=True)
    shutil.copy2(out / "compute" / "base.json", o2 / "compute" / "base.json")
    if (out / "selftest.json").is_file():
        shutil.copy2(out / "selftest.json", o2 / "selftest.json")
    new = step_package_compute(o2)
    ref = {r["label"]: r for r in rd(out / "compute" / "package.json")["rows"]}
    rows = []
    for r in new["rows"]:
        a = ref.get(r["label"]) or {}
        eq = {k: a.get(k) == r.get(k) for k in ("status", "by_class", "cache_sc_sha256")}
        eq["patched_units"] = sorted(a.get("patched_units") or []) == \
            sorted(r.get("patched_units") or [])
        rows.append({"label": r["label"], "equal": eq, "pass": all(eq.values()),
                     "ref_artifact_dir": a.get("artifact_dir"),
                     "recheck_artifact_dir": r.get("artifact_dir"),
                     "cache_sc_sha256": r.get("cache_sc_sha256")})
    doc = {"rows": rows, "pass": bool(rows) and all(x["pass"] for x in rows)}
    dump(o2 / "compare.json", doc)
    log(f"recheck package-compute: {[(x['label'], x['pass']) for x in rows]}")
    return doc


def step_package_sfb_b4(out: Path) -> dict[str, Any]:
    """B4 layered on the ``icount-entry-sfb`` baseline fix (LLVM survey: time B4 on top
    of the entry fix): the sfb artifact re-imported as a snapshot (its derived ``.scm``
    carries full Tier-0 metadata), then ``eliminate-state-sync`` (D1|D2|D3) on it."""
    pk = rd(out / "compute" / "package.json")
    base = rd(out / "compute" / "base.json")
    sfb = next(r for r in pk["rows"] if r["label"] == "sfb")
    art, snap = Path(sfb["artifact_dir"]), Path(base["snapshot"])
    store = out / "compute" / "sfb-snap-store"
    snaps = sorted((store / "snapshots").iterdir()) if (store / "snapshots").is_dir() else []
    if not snaps:
        rc, _ = _scopt(["import", "--sc", art / "cache.sc",
                        *sum((["--scm", str(x)] for x in sorted(art.glob("cache*.scm"))), []),
                        "--manifest", snap / "manifest.json", "--bundle", snap / "bundle.json",
                        "--runtime-identity", snap / "runtime_identity.json", "--store", store],
                       out / "compute" / "logs" / "20-import-sfb.log")
        snaps = sorted((store / "snapshots").iterdir())
    s, lay, ri2 = load_ri(snaps[0], Path(base["config"]).read_text())
    row, _prop = package_one(s, lay, ri2, store, "wp29-compute-sfb-b4", selftest(out))
    row.update(label="sfb+b4", sfb_snapshot=str(snaps[0]), layered_on=sfb["artifact_dir"])
    if row.get("artifact_dir"):
        row["v12_host_abs"] = v12_host_abs(Path(row["artifact_dir"]), row["patched_units"])
    pk["rows"] = [r for r in pk["rows"] if r["label"] != "sfb+b4"] + [row]
    dump(out / "compute" / "package.json", pk)
    log(f"compute sfb+b4: {row['status']} {row['by_class']} units {row['units_edited']}")
    return row


# ---------------------------------------------------------------------------
# dynamic
# ---------------------------------------------------------------------------

def _artifact_files(doc: dict[str, Any], rel: str) -> list[dict[str, Any]]:
    ev = []
    for c in doc["cases"]:
        pair = {}
        for side_ in ("artifact", "reference"):
            rdir = (c["runs"].get(side_) or {}).get("run_dir")
            f = Path(rdir) / rel if rdir else None
            pair[side_] = ("sha256:" + hashlib.sha256(f.read_bytes()).hexdigest(),
                           f.stat().st_size) if f is not None and f.is_file() else None
        ev.append(pair)
    return ev


def _qemu_log_abort(doc: dict[str, Any]) -> list[bool]:
    out = []
    for c in doc["cases"]:
        rdir = (c["runs"].get("artifact") or {}).get("run_dir")
        txt = ""
        if rdir:
            for n in ("stdout.log", "stderr.log", "work/qemu.log", "qemu.log"):
                p = Path(rdir) / n
                if p.is_file():
                    txt += p.read_text(errors="replace")
        out.append("cpu_abort" in txt or "qemu: fatal" in txt or "Aborted" in txt)
    return out


def cpu_abort_gdb_zero(r: dict[str, Any]) -> bool:
    """F-B4-9: the gdb count of ``cpu_abort`` calls in the artifact run resolved and is 0."""
    ca = r.get("cpu_abort_calls_artifact") or {}
    return bool(ca.get("resolved")) and ca.get("hits") == 0


def step_dynamic(out: Path, only: list[str] | None = None) -> dict[str, Any]:
    from scopt.importer.import_cache import open_snapshot
    from scopt.run.diag import gdb_hits
    from scopt.run.oracles import dynamic_oracles

    pk = json.loads((out / "package.json").read_text())
    res = []
    for row in pk["rows"]:
        mode, cfg = row["mode"], row["config"]
        if only and f"{mode}/{cfg}" not in only and cfg not in only:
            continue
        if not row.get("artifact_dir"):
            res.append({"mode": mode, "config": cfg, "verdict": "not_run",
                        "why": f"no artifact ({row['status']})", "pass": False})
            continue
        sj = snap_info(out, mode, cfg)
        art = Path(row["artifact_dir"])
        dd = out / "dyn" / mode / cfg
        if dd.exists():
            rmtree(dd)
        t0 = time.monotonic()
        timer = bool(b3conf.BY_NAME[cfg].timing)
        gated = ["O1", "O2b", "O5"] if (mode == "noicount" and timer) else \
            ["O1", "O2a", "O2b", "O5"]
        if cfg == "b4-10":
            gated.append("O6")
        doc = dynamic_oracles(art, [sj["base_toml"]], dd / "oracles",
                              oracles=tuple(gated), baseline=sj["snapshot"],
                              reps=REPS[mode], qemu=GATE, timeout_s=300,
                              inputs=sj["inputs"], fixture_expected=FX / "expected.py",
                              fixture_inputs=[cfg], quiet=False)
        r: dict[str, Any] = {"mode": mode, "config": cfg, "fixture": row["fixture"],
                             "artifact_dir": str(art), "gated_oracles": gated,
                             "timer_phase": timer, "verdict": doc["verdict"],
                             "failures": doc["failures"], "incomplete": doc["incomplete"],
                             "by_class": row["by_class"],
                             "oracles": [{"rep": c["rep"], **{o["oracle"]: o["verdict"]
                                                              for o in c["oracles"]}}
                                         for c in doc["cases"]],
                             "oracle_detail": [{o["oracle"]: str(o.get("detail"))[:200]
                                                for o in c["oracles"]} for c in doc["cases"]],
                             "runs_qemu_sha256": run_qemu_shas(doc)}
        a0 = Path(doc["cases"][0]["runs"]["artifact"]["run_dir"])
        r0 = Path(doc["cases"][0]["runs"]["reference"]["run_dir"])
        s = open_snapshot(sj["snapshot"])
        prop_units = dict(row["patched_units"])
        probes = rewritten_probes(art, s, prop_units, cfg)
        r["probes"] = [{k: (hex(v) if k == "addr" else v) for k, v in p.items()}
                       for p in probes]
        if probes:
            h = gdb_hits(a0, dd / "hits", [(p["what"], p["addr"]) for p in probes],
                         timeout_s=60 if (mode == "noicount" and timer) else 300)
            clean = {k for k, x in enumerate(h["runs"]) if x["qemu_exit"] == 0}
            good = [t for t in h["targets"] if t["run"] in clean and t["hits"] is not None]
            r["hits"] = [{"what": t["what"], "hits": t["hits"] if t in good else None}
                         for t in h["targets"]]
            r["nonvacuous"] = any(t["hits"] > 0 for t in good)
            r["kernel_unit_rewritten_and_entered"] = any(
                t["hits"] > 0 for t in good
                if next(p for p in probes if p["what"] == t["what"])["holds_label"])
            ran: Counter[str] = Counter()
            for t in good:
                if t["hits"] > 0:
                    ran.update(next(p for p in probes if p["what"] == t["what"])
                               ["stores_deleted"])
            r["deleted_in_executed_probes"] = {k: ran.get(k, 0) for k in ("D1", "D2", "D3")}
        else:
            r["nonvacuous"] = False
        if mode == "noicount" and timer:
            ctl = {}
            for lab_, src in (("artifact_vs_warm", art), ("warm_vs_warm", Path(sj["snapshot"]))):
                cd = dynamic_oracles(src, [sj["base_toml"]], dd / f"o2a-{lab_}",
                                     oracles=("O2a",), baseline=sj["snapshot"], reps=2,
                                     qemu=GATE, timeout_s=300, inputs=sj["inputs"], quiet=False)
                ctl[lab_] = [o["verdict"] for c in cd["cases"] for o in c["oracles"]]
            r["o2a_reported_noicount_timer"] = {
                **ctl, "note": "not gated: without icount the exception sequence of a timer "
                               "phase depends on host time; warm_vs_warm shows it"}
        if cfg == "b4-4":
            ca = gdb_count_symbol(a0, dd / "iorc-artifact", "cpu_io_recompile")
            cr = gdb_count_symbol(r0, dd / "iorc-reference", "cpu_io_recompile")
            r["io_recompile"] = {"artifact": ca, "reference": cr,
                                 "equal": ca["hits"] is not None and ca["hits"] == cr["hits"]
                                 and ca["qemu_exit"] == 0 and cr["qemu_exit"] == 0}
        if cfg == "b4-7":
            ev = _artifact_files(doc, "work/run-artifacts/variable_watch/events.tsv")
            r["events_tsv"] = ev
            r["events_equal"] = bool(ev) and all(p["artifact"] and p["reference"] and
                                                 p["artifact"] == p["reference"] and
                                                 p["artifact"][1] > 0 for p in ev)
        if cfg == "b4-9":
            ev = _artifact_files(doc, "work/run-artifacts/b3_tbirq/counts.txt")
            r["counts_txt"] = ev
            r["counts_equal"] = bool(ev) and all(p["artifact"] and p["reference"] and
                                                 p["artifact"] == p["reference"] for p in ev)
            r["cpu_abort_seen"] = _qemu_log_abort(doc)
            ca = gdb_count_symbol(a0, dd / "abort-artifact", "cpu_abort")
            r["cpu_abort_calls_artifact"] = ca
        r["seconds"] = round(time.monotonic() - t0, 1)
        r["pass"] = (doc["verdict"] == "pass" and r["nonvacuous"]
                     and r.get("io_recompile", {}).get("equal", True)
                     and r.get("events_equal", True) and r.get("counts_equal", True)
                     and not any(r.get("cpu_abort_seen", []))
                     and (cfg != "b4-9" or cpu_abort_gdb_zero(r))
                     and r.get("runs_qemu_sha256") == ["sha256:" + sha_file(GATE)])
        res.append(r)
        log(f"dynamic {mode} {cfg}: oracles={doc['verdict']} nonvacuous={r['nonvacuous']} "
            f"iorc={r.get('io_recompile', {}).get('equal')} events={r.get('events_equal')} "
            f"counts={r.get('counts_equal')} pass={r['pass']}")
        dump(out / "dynamic.partial.json", {"rows": res})
    old = (rd(out / "dynamic.json") or {}).get("rows", []) if only else []
    keep = [x for x in old if not any(x["mode"] == y["mode"] and x["config"] == y["config"]
                                      for y in res)]
    doc = {"rows": sorted(keep + res, key=lambda x: (x["mode"], x["config"]))}
    dump(out / "dynamic.json", doc)
    return doc


def step_deletions(out: Path, only: list[str] | None = None) -> dict[str, Any]:
    """Gate item 2 "D1 and D2 deletions > 0" (and item 4's executed D1 > 0 on F-B4-10):
    gdb hardware-breakpoint entry counts of **every** rewritten unit of each icount fixture
    artifact (untimed diag re-runs of the first artifact run), so the deleted stores of
    executed units are counted per class. ``only``: these configs; the other rows of an
    existing ``deletions.json`` are kept."""
    from scopt.importer.import_cache import open_snapshot
    from scopt.run.diag import gdb_hits

    pk = rd(out / "package.json")
    dy = {(r["mode"], r["config"]): r for r in rd(out / "dynamic.json")["rows"]}
    rows = []
    for row in pk["rows"]:
        mode, cfg = row["mode"], row["config"]
        if mode != "icount" or not row.get("artifact_dir"):
            continue
        if only and cfg not in only and f"{mode}/{cfg}" not in only:
            continue
        d = dy.get((mode, cfg)) or {}
        a0 = Path(d["artifact_dir"]) and None
        run0 = sorted((out / "dyn" / mode / cfg / "oracles").glob("**/run.json"))
        arts = [p.parent for p in run0
                if json.loads(p.read_text()).get("arm") not in ("warm", "reference")]
        sj = snap_info(out, mode, cfg)
        s = open_snapshot(sj["snapshot"])
        probes = rewritten_probes(Path(row["artifact_dir"]), s, dict(row["patched_units"]), cfg,
                                  limit=10_000)
        src = None
        for c in json.loads((out / "dyn" / mode / cfg / "oracles" /
                             "dynamic_oracles.json").read_text())["cases"]:
            src = c["runs"]["artifact"]["run_dir"]
            break
        dd = out / "deletions" / cfg
        rmtree(dd)
        h = gdb_hits(src, dd, [(p["what"], p["addr"]) for p in probes], timeout_s=300)
        clean = {k for k, x in enumerate(h["runs"]) if x["qemu_exit"] == 0}
        by = {p["what"]: p for p in probes}
        ran: Counter[str] = Counter()
        tot: Counter[str] = Counter()
        unknown = 0
        for t_ in h["targets"]:
            p = by[t_["what"]]
            tot.update(p["stores_deleted"])
            if t_["run"] not in clean or t_["hits"] is None:
                unknown += 1
            elif t_["hits"] > 0:
                ran.update(p["stores_deleted"])
        r = {"config": cfg, "fixture": row["fixture"], "rewritten_units": len(probes),
             "units_entered": sum(1 for t_ in h["targets"] if t_["run"] in clean
                                  and (t_["hits"] or 0) > 0),
             "units_unknown": unknown, "deleted_total": {k: tot.get(k, 0) for k in
                                                         ("D1", "D2", "D3")},
             "deleted_in_entered_units": {k: ran.get(k, 0) for k in ("D1", "D2", "D3")},
             "source_run": src, "hits": h["targets"]}
        rows.append(r)
        log(f"deletions {cfg}: entered {r['units_entered']}/{len(probes)} unknown {unknown} "
            f"executed {r['deleted_in_entered_units']}")
        del a0, arts
    old = (rd(out / "deletions.json") or {}).get("rows", []) if only else []
    keep = [x for x in old if x["config"] not in {r["config"] for r in rows}]
    doc = {"rows": sorted(keep + rows, key=lambda x: x["config"])}
    dump(out / "deletions.json", doc)
    return doc


def step_b4_10(out: Path) -> dict[str, Any]:
    """F-B4-10 diagnosis and an O6 control run (warm vs warm with the coverage ring) showing
    the oracle itself runs on the gate. The diagnosis counts, per mode, the units that carry
    FastDyn's LOG_REG coverage block (a HOST_ABS store, or a block the close-out matcher
    ``scopt.decode.logreg`` recognised or refused), how many blocks matched (one atomic
    ``HostLog`` event, design/closeout-logreg.md A), and how many of those units are still
    pinned by HostOps, cyclic in hcfg (a back edge other than the matched spin) or skipped
    by syncdead; and the B4 deletions on the block units (``package.json``)."""
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import OpKind, build_hostops
    from scopt.passes.eliminate_state_sync import propose_b4
    from scopt.run.oracles import dynamic_oracles

    pk = {(r["mode"], r["config"]): r for r in (rd(out / "package.json") or {}).get("rows", [])}
    res: dict[str, Any] = {}
    for mode in MODES:
        sj, s, lay, ri2 = load(out, mode, "b4-10")
        prop = propose_b4(s, layout=lay, runtime=ri2)
        patched = (pk.get((mode, "b4-10")) or {}).get("patched_units") or {}
        units = []
        for u in s.units():
            h = build_hostops(u, lay, s.header.code_address)
            g = build_hcfg(h)
            back = [f"{o.off:#x}->{o.target:#x} {o.text}" for o in h.ops
                    if o.kind == OpKind.JCC and o.target is not None and o.target <= o.off
                    and o.block == "insn"]
            logreg = [o.text for o in h.ops if o.kind == OpKind.HOST_ABS_STORE]
            sd = prop.syncdead.get(u.uid)
            sd_status, sd_reason = (None, "not analysed") if sd is None else (sd.status,
                                                                              sd.reason)
            units.append({"uid": u.uid, "cflags": hex(u.key.cflags), "hostops_ok": h.ok,
                          "pins": list(h.reasons)[:4], "hcfg_cyclic": bool(g.cyclic),
                          "spin_edges": [list(e) for e in getattr(g, "spin_edges", ())],
                          "log_blocks_matched": len(h.log_blocks),
                          "logreg_nomatch": h.logreg_nomatch,
                          "syncdead": sd_status, "syncdead_reason": sd_reason,
                          "rewritten_by_b4": patched.get(u.uid),
                          "backward_insn_jcc": back, "host_abs_stores": logreg})
        cov = [x for x in units if x["host_abs_stores"] or x["log_blocks_matched"]
               or x["logreg_nomatch"] is not None]
        bc = (pk.get((mode, "b4-10")) or {}).get("by_class")
        res[mode] = {
            "units": len(units), "units_with_logreg_store": len(cov),
            "logreg_blocks_matched": sum(x["log_blocks_matched"] for x in cov),
            "logreg_units_matched": sum(1 for x in cov if x["log_blocks_matched"]),
            "logreg_units_unmatched": sum(1 for x in cov if x["logreg_nomatch"] is not None),
            "logreg_unmatched_reasons": dict(Counter(str(x["logreg_nomatch"]) for x in cov
                                                     if x["logreg_nomatch"] is not None)),
            "logreg_units_hostops_pinned": sum(1 for x in cov if not x["hostops_ok"]),
            "logreg_units_pinned_unexplained_host_store": sum(
                1 for x in cov if any(p.startswith("unexplained_host_store") for p in x["pins"])),
            "logreg_units_with_backward_jcc": sum(1 for x in cov if x["backward_insn_jcc"]),
            "logreg_units_hcfg_cyclic": sum(1 for x in cov if x["hcfg_cyclic"]),
            "logreg_units_syncdead": dict(Counter(f"{x['syncdead']}"
                                                  f"{':' + str(x['syncdead_reason']) if x['syncdead_reason'] else ''}"
                                                  for x in cov)),
            "logreg_units_rewritten_by_b4": sum(1 for x in cov if x["rewritten_by_b4"]),
            "b4_by_class": bc,
            "example": cov[0] if cov else None}
        if mode == "icount":
            dd = out / "dyn" / "b4-10-o6-control"
            rmtree(dd)
            doc = dynamic_oracles(Path(sj["snapshot"]), [sj["base_toml"]], dd,
                                  oracles=("O1", "O2b", "O6"), baseline=sj["snapshot"],
                                  reps=1, qemu=GATE, timeout_s=300, inputs=sj["inputs"],
                                  fixture_expected=FX / "expected.py",
                                  fixture_inputs=["b4-10"], quiet=False)
            res["o6_control_warm_vs_warm"] = {
                "verdict": doc["verdict"], "runs_qemu_sha256": run_qemu_shas(doc),
                "oracles": [{o["oracle"]: (o["verdict"], str(o.get("detail"))[:200])
                             for o in c["oracles"]} for c in doc["cases"]]}
    dump(out / "b4-10.json", res)
    log(f"b4-10: {json.dumps({m: {k: v for k, v in res[m].items() if k != 'example'} for m in MODES})}")
    return res


# ---------------------------------------------------------------------------
# mutants: static (every kind, every icount fixture base + compute) and O4
# ---------------------------------------------------------------------------

#: kind -> detector fixtures searched in order (design §9.3 / §9.5; b4_mutants.DETECTORS)
O4_PLAN: list[tuple[str, list[str]]] = [
    ("guest_store_delete", ["b4-7"]),
    ("update_mem_delete", ["b4-7"]),
    ("log_reg_delete", ["b4-10"]),
    ("flag_store_before_ldst", ["b4-2", "b4-5"]),
    ("flag_store_before_helper", ["b4-1", "b4-2", "b4-5", "b4-6", "b4-3"]),
    ("brcond_reload", ["b4-1"]),
    ("cdio_last_insn_direct_chain", ["b4-9"]),
    ("it_block_d4", ["b4-5"]),
    ("it_block_d4_slowpath", ["b4-5"]),
    ("icount_immediate", ["b4-3"]),
    ("prefix_icount_store", ["b4-3"]),
    ("d1_before_exit", ["b4-1"]),
    ("d1_before_udiv", ["b4-6"]),
    ("d3_before_plugin_call", ["b4-7"]),
    ("pc_store_delete", ["b4-1"]),
    ("exit_stub_edit", ["b4-1"]),
    # close-out (design/closeout-logreg.md A.8): real LOG_REG kinds on coverage units,
    # detected by O6 (the coverage ring); reported, not gating
    ("log_reg_ring_store", ["b4-10"]),
    ("log_reg_index_store", ["b4-10"]),
    ("log_reg_value_field", ["b4-10"]),
]


def o6_detects(kind: str) -> bool:
    """LOG_REG kinds are detected by O6 (the coverage ring, design §9.5 F-B4-10), not by
    O1/O2a/O2b (this FastDyn build writes no coverage file)."""
    return kind.startswith("log_reg")


def o4_verdict(kind: str, doc: dict[str, Any]) -> tuple[str, list[str]]:
    """(``pass`` = detected | the dynamic_oracles verdict, the O6 verdicts)."""
    o6 = [o["verdict"] for c_ in doc["cases"] for o in c_["oracles"] if o["oracle"] == "O6"]
    det = doc["verdict"] == "pass" or (o6_detects(kind) and bool(o6) and
                                       all(v == "fail" for v in o6))
    return ("pass" if det else doc["verdict"]), o6
#: design §9.3 B4 entries (the gate's "every B4 mutant of 9.3") -> mutant kinds here
DESIGN_93_B4 = {
    "delete a guest store": ["guest_store_delete"],
    "delete an UPDATE_MEM / LOG_REG store": ["update_mem_delete", "log_reg_delete"],
    "delete a flag store before an LDST": ["flag_store_before_ldst"],
    "delete a flag store before a flags-0 helper": ["flag_store_before_helper"],
    "delete a brcond sync store whose global is reloaded (T-B4-2b)": ["brcond_reload"],
    "delete the last-insn can_do_io=1 before a direct-chain exit (F-B4-9)":
        ["cdio_last_insn_direct_chain"],
    "IT-block D4 mutant": ["it_block_d4", "it_block_d4_slowpath"],
    "change an icount immediate": ["icount_immediate"],
    "put a host_abs pointer inside env": ["host_abs_in_env"],
}


def log_reg_mutants(s: Any, lay: Any) -> list[Any]:
    """F-B4-10 (coverage): NOP a FastDyn LOG_REG host store (coverage PC ring)."""
    from scopt.analysis.hostops import OpKind, build_hostops
    from scopt.analysis.syncdead import nop_bytes
    from scopt.b4_mutants import B4Mutant
    from scopt.passes.base import Patch

    out = []
    for u in s.units():
        h = build_hostops(u, lay, s.header.code_address)
        for o in h.ops:
            if o.kind == OpKind.HOST_ABS_STORE:
                old = u.code[o.off:o.off + o.size]
                p = Patch(target=u.uid, host_off=o.off, old_bytes=old,
                          new_bytes=nop_bytes(o.size), template_id="b4_nop_store",
                          justification=f"D1:log_reg:{o.text}")
                out.append(B4Mutant("log_reg_delete", u.uid, (p,),
                                    f"NOP FastDyn LOG_REG host store {o.text!r}", None,
                                    "F-B4-10"))
    return out


def all_mutants(cfg: str, s: Any, lay: Any, ri2: dict[str, Any], per_kind: int) -> list[Any]:
    from scopt.b4_mutants import find_mutants, with_admission

    ms = list(find_mutants(s, lay, ri2, per_kind=per_kind))
    if ri2["admission"].get("tb_start_cb_possible"):
        # the cdio mutant is a store that is dead only under tb_start_cb_possible = false;
        # search under false, package with true (b4_mutants does the latter)
        rif = with_admission(ri2, tb_start_cb_possible=False,
                             tb_start_cb_reasons=["wp29:search_only"])
        ms += [m for m in find_mutants(s, lay, rif, per_kind=per_kind)
               if m.kind == "cdio_last_insn_direct_chain"]
        for m in ms:
            if m.kind == "cdio_last_insn_direct_chain":
                m.runtime = with_admission(ri2, tb_start_cb_possible=True)
    if cfg == "b4-7":
        ms += update_mem_mutants(s, lay)
    if cfg == "b4-10":
        ms += log_reg_mutants(s, lay)
    return ms


def step_mutants(out: Path, o4: bool = True) -> dict[str, Any]:
    from scopt.b4_mutants import STATIC_ONLY, check_mutant, synthetic_b4

    st = selftest(out)
    rows = []
    pool: dict[tuple[str, str], list[Any]] = {}
    for cfg in CONFIGS:
        sj, s, lay, ri2 = load(out, "icount", cfg)
        ent = unit_entries(sj.get("profile"))
        ms = all_mutants(cfg, s, lay, ri2, per_kind=200)
        # one static check per (kind, unit); hottest units first
        ms.sort(key=lambda m: (-ent.get(m.uid, 0), m.uid))
        seen: set[tuple[str, str]] = set()
        store = out / "mutants-store" / cfg
        store.mkdir(parents=True, exist_ok=True)
        for m in ms:
            if (m.kind, m.uid) in seen:
                continue
            seen.add((m.kind, m.uid))
            pool.setdefault((cfg, m.kind), []).append((m, sj, s, lay, ri2, ent.get(m.uid, 0)))
            if len(pool[(cfg, m.kind)]) > MAX_O4_TRIES:
                continue  # static check of the first MAX_O4_TRIES instances per kind
            c = check_mutant(s, m, store, layout=lay, runtime=ri2, selftest=st,
                             candidate_id=f"wp29-mut-{cfg}-{m.kind}-{m.uid[-14:-2]}")
            rows.append({"config": cfg, "entries": ent.get(m.uid, 0), **m.to_json(),
                         "check": c.to_json()})
        log(f"mutants {cfg}: {len([r for r in rows if r['config'] == cfg])} checked, kinds "
            f"{sorted({k for (c_, k) in pool if c_ == cfg})}")
    # compute (static only)
    base = rd(out / "compute" / "base.json")
    if base and base.get("snapshot"):
        s, lay, ri2 = load_ri(Path(base["snapshot"]), Path(base["config"]).read_text())
        store = out / "mutants-store" / "compute"
        store.mkdir(parents=True, exist_ok=True)
        for m in all_mutants("compute", s, lay, ri2, per_kind=1):
            c = check_mutant(s, m, store, layout=lay, runtime=ri2, selftest=st,
                             candidate_id=f"wp29-mut-compute-{m.kind}-{m.uid[-14:-2]}")
            rows.append({"config": "compute", **m.to_json(), "check": c.to_json()})
        log("mutants compute: checked")
    syn = synthetic_b4()
    rej = sum(1 for r in rows if r["check"]["rejected"])
    doc: dict[str, Any] = {
        "static": {"total": len(rows), "rejected": rej, "rows": rows,
                   "kinds": sorted({r["kind"] for r in rows}),
                   "not_rejected": [r for r in rows if not r["check"]["rejected"]],
                   "synthetic_b4": syn, "static_only": STATIC_ONLY}}
    if o4:
        doc["o4"] = step_o4(out, pool)
    dump(out / "mutants.json", doc)
    return doc


def step_o4(out: Path, pool: dict) -> list[dict[str, Any]]:
    from scopt.passes.eliminate_state_sync import plan_of
    from scopt.run.negative import package_negative
    from scopt.run.oracles import dynamic_oracles

    res = []
    root = out / "negative"
    for kind, cfgs in O4_PLAN:
        cands = [x for c in cfgs for x in pool.get((c, kind), [])]
        # executed units first (train profile of the base), hottest first, per fixture order
        cands.sort(key=lambda x: (x[5] == 0, cfgs.index(x[1]["config"]), -x[5]))
        entry: dict[str, Any] = {"kind": kind, "fixtures": cfgs, "instances": len(cands),
                                 "attempts": []}
        if not cands:
            entry["verdict"] = "not_run"
            entry["why"] = "mutant shape not found in the detector fixture snapshots"
            res.append(entry)
            log(f"O4 {kind}: no instance")
            continue
        for k, (m, sj, s, lay, ri2, ent) in enumerate(cands[:MAX_O4_TRIES]):
            cfg = sj["config"]
            label = f"{cfg}-{kind}-{k}"
            if (root / label).exists():
                rmtree(root / label)
            ri = dict(m.runtime) if m.runtime is not None else ri2
            nr = package_negative(s, plan_of({m.uid: m.patches}), root, label=label,
                                  mutant=kind, detector=m.detector or cfg, layout=lay,
                                  runtime=ri)
            att: dict[str, Any] = {"config": cfg, "uid": m.uid, "unit_entries": ent,
                                   "note": m.note,
                                   "patches": [p.to_json() for p in m.patches],
                                   "package": getattr(nr, "status", None),
                                   "package_reason": getattr(nr, "reason", None)}
            if getattr(nr, "path", None) is None:
                att["verdict"] = "not_run"
                entry["attempts"].append(att)
                continue
            dd = out / "dyn-o4" / label
            if dd.exists():
                rmtree(dd)
            doc = dynamic_oracles(nr.path, [sj["base_toml"]], dd,
                                  oracles=("O1", "O2b", "O6") if o6_detects(kind)
                                  else ("O1", "O2b"),
                                  baseline=sj["snapshot"], reps=1, qemu=GATE, timeout_s=300,
                                  inputs=sj["inputs"], fixture_expected=FX / "expected.py",
                                  fixture_inputs=[cfg], allow_negative_control=True,
                                  quiet=False)
            att["verdict"], att["o6"] = o4_verdict(kind, doc)  # pass = detected
            att["runs_qemu_sha256"] = run_qemu_shas(doc)
            att["detail"] = [{o["oracle"]: (o["verdict"], str(o.get("detail"))[:200])
                              for o in c["oracles"]} for c in doc["cases"]]
            if cfg == "b4-7":
                ev = _artifact_files(doc, "work/run-artifacts/variable_watch/events.tsv")
                att["detected_by_events_tsv"] = any(p["artifact"] and p["reference"] and
                                                    p["artifact"] != p["reference"] for p in ev)
            if cfg == "b4-9":
                ev = _artifact_files(doc, "work/run-artifacts/b3_tbirq/counts.txt")
                att["counts_txt_differs"] = any(p["artifact"] != p["reference"] for p in ev)
                att["cpu_abort_seen"] = _qemu_log_abort(doc)
            entry["attempts"].append(att)
            log(f"O4 {kind} try {k} {cfg} {m.uid[-14:]} entries={ent}: {att['verdict']} "
                f"(o4 {doc['verdict']}, o6 {att.get('o6')})")
            if att["verdict"] == "pass":
                break
        vs = [a["verdict"] for a in entry["attempts"]]
        entry["verdict"] = "pass" if "pass" in vs else ("fail" if "fail" in vs else "not_run")
        entry["detected_at_attempt"] = vs.index("pass") if "pass" in vs else None
        res.append(entry)
        dump(out / "o4.partial.json", res)
    return res


#: second O4 round for kinds the first round did not detect (``o4-extra``): more detector
#: fixtures (B3 fixture bases captured on the gate into ``OUT/fx-b3`` by
#: wp20_fixture_snapshots.py; a B3 fixture is a valid detector for a B4 mutant when its
#: observed event executes the mutated unit) and phase-specific units first.
O4_EXTRA: dict[str, list[str]] = {
    "flag_store_before_ldst": ["8", "8p", "3b", "b4-2", "b4-5"],
    "flag_store_before_helper": [B411, "21", "5", "b4-2", "b4-5", "b4-6"],
    "log_reg_delete": ["b4-10"],
    "pc_store_delete": ["b4-1", "b4-2", "b4-5"],
    "d3_before_plugin_call": ["b4-7", "7", "6h"],
    "log_reg_ring_store": ["b4-10"],
    "log_reg_index_store": ["b4-10"],
    "log_reg_value_field": ["b4-10"],
}
EXTRA_TRIES = int(os.environ.get("WP29_EXTRA_TRIES", "8"))
#: kinds whose detection is judged on one designated detector fixture, on **every**
#: instance there, each with its predicted divergence (design/closeout-logreg.md B.3 step 4:
#: flag_store_before_helper on F-B4-11; icount gated, noicount reported). Disclosed change
#: from design §9.5, which named F-B4-1 (it has no instance of the kind on the gate base).
O4_SPECIFIC: dict[str, str] = {"flag_store_before_helper": B411}


def _fx_dir(out: Path, cfg: str, mode: str = "icount") -> Path:
    return out / ("fx" if cfg.startswith("b4-") else "fx-b3") / mode / cfg


def b411_predicted(s: Any, lay: Any, m: Any, doc: dict[str, Any]) -> dict[str, Any]:
    """The predicted O1 divergence of a ``flag_store_before_helper`` control on F-B4-11
    (``expected.b411_divergence``: the result words of exactly the (pair, poison)
    iterations whose poison bit differs from the true flag, in the instance's sub-phase)
    against the result words the control's artifact run printed. ``predicted_exact`` holds
    only when the differing words (any index) are exactly the predicted ones, with the
    predicted values."""
    import expected  # fixtures/b3/expected.py (FX is on sys.path)

    sub, flag = b411_instance(s, lay, m)
    pred = expected.b411_divergence(sub, flag) if sub and flag else {}
    want = {0: 48, **{0x10 + j: w for j, w in enumerate(expected.b411_words())}}
    rdir = None
    for c_ in doc.get("cases") or []:
        rdir = (c_["runs"].get("artifact") or {}).get("run_dir")
        break
    gn = Path(rdir) / "guest.norm" if rdir else None
    got = expected.parse(gn.read_text())["results"] if gn is not None and gn.is_file() \
        else None
    diff = None if got is None else {k: got.get(k) for k in sorted(set(want) | set(got))
                                     if got.get(k) != want.get(k)}
    return {"sub": sub, "flag": flag, "guest_norm": str(gn) if gn else None,
            "predicted": {hex(k): v for k, v in sorted(pred.items())},
            "observed_divergence": None if diff is None else {hex(k): v for k, v in
                                                               diff.items()},
            "predicted_exact": bool(pred) and diff == pred}


def specific_summary(entry: dict[str, Any], fixture: str) -> dict[str, Any]:
    """Every-instance record of a kind on its designated detector fixture (O4_SPECIFIC)."""
    out: dict[str, Any] = {"fixture": fixture}
    for mode in MODES:
        at = [a for a in entry["attempts"] if a.get("config") == fixture
              and a.get("mode", "icount") == mode]
        out[mode] = {"instances": len(at),
                     "detected_as_predicted": sum(1 for a in at if a.get("verdict") == "pass"
                                                  and a.get("predicted_exact")),
                     "anomalies": [a.get("uid") for a in at if a.get("verdict") == "anomaly"],
                     "not_detected": [f"{a.get('sub')}-{a.get('flag')}" for a in at
                                      if a.get("verdict") not in ("pass", "anomaly")],
                     "subphase_flags": sorted({f"{a.get('sub')}-{a.get('flag')}" for a in at}),
                     "all_detected_as_predicted": bool(at) and all(
                         a.get("verdict") == "pass" and a.get("predicted_exact") for a in at)}
    out["gated_mode"] = "icount"
    out["pass"] = out["icount"]["all_detected_as_predicted"]
    return out


def _o4x_attempt(out: Path, root: Path, dyn_root: Path, label: str, cfg: str,
                 loaded_cfg: tuple, m: Any, st: Any, mode: str = "icount"
                 ) -> tuple[dict[str, Any], dict[str, Any]]:
    """One O4 control of mutant ``m`` on fixture ``cfg`` in ``mode`` (``loaded_cfg`` is that
    mode's base: static check, packaging as a negative control, O1/O2b — plus O6 for the
    LOG_REG kinds — against warm, one rep; on F-B4-11 also the predicted divergence);
    returns the attempt record and the static row."""
    from scopt.b4_mutants import check_mutant
    from scopt.passes.eliminate_state_sync import plan_of
    from scopt.run.negative import package_negative
    from scopt.run.oracles import dynamic_oracles

    kind = m.kind
    sj, s, lay, ri2, ent = loaded_cfg
    store = out / "mutants-store" / f"extra-{cfg}"
    store.mkdir(parents=True, exist_ok=True)
    c = check_mutant(s, m, store, layout=lay, runtime=ri2, selftest=st,
                     candidate_id=f"wp29-xmut-{cfg}-{kind}-{m.uid[-14:-2]}")
    srow = {"config": cfg, "mode": mode, "entries": ent.get(m.uid, 0), **m.to_json(),
            "check": c.to_json()}
    rmtree(root / label)
    ri = dict(m.runtime) if m.runtime is not None else ri2
    nr = package_negative(s, plan_of({m.uid: m.patches}), root, label=label,
                          mutant=kind, detector=cfg, layout=lay, runtime=ri)
    att: dict[str, Any] = {"config": cfg, "uid": m.uid, "unit_entries": ent.get(m.uid, 0),
                           "note": m.note, "static_rejected": c.rejected, "mode": mode,
                           "patches": [p.to_json() for p in m.patches]}
    if getattr(nr, "path", None) is None:
        att["verdict"] = "not_run"
        return att, srow
    dd = dyn_root / label
    rmtree(dd)
    orc = ("O1", "O2b", "O6") if o6_detects(kind) else ("O1", "O2b")
    inp = b3conf.BY_NAME[cfg].input_names()[0]
    doc = dynamic_oracles(nr.path, [sj["base_toml"]], dd, oracles=orc,
                          baseline=sj["snapshot"], reps=1, qemu=GATE, timeout_s=300,
                          inputs=sj["inputs"][:1], fixture_expected=FX / "expected.py",
                          fixture_inputs=[inp], allow_negative_control=True, quiet=False)
    att["o4_verdict"] = doc["verdict"]
    att["runs_qemu_sha256"] = run_qemu_shas(doc)
    att["detail"] = [{o["oracle"]: (o["verdict"], str(o.get("detail"))[:200])
                      for o in c_["oracles"]} for c_ in doc["cases"]]
    att["verdict"], att["o6"] = o4_verdict(kind, doc)
    if cfg == B411 and kind == "flag_store_before_helper":
        att.update(b411_predicted(s, lay, m, doc))
        if att["verdict"] == "pass" and not att["predicted_exact"]:
            # O1 differs, but not exactly in the predicted records: reported as an anomaly,
            # never counted as the specific detection (design/closeout-logreg.md B.3 step 4)
            att["verdict"] = "anomaly"
    return att, srow


def step_o4_extra(out: Path, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """Second O4 round: candidates from every listed fixture, units that occur in only one
    fixture snapshot (phase code, not shared startup/report code) first, then by train
    entries; for ``log_reg_delete`` the detector is O6 (the LOG_REG coverage ring at
    benchmark_end, design §9.5 F-B4-10), read from the control's own O6 row."""
    st = selftest(out)
    mu = rd(out / "mutants.json")
    first = {r["kind"]: r for r in mu.get("o4") or []}
    todo = kinds or [k for k in O4_EXTRA if (first.get(k) or {}).get("verdict") != "pass"]
    loaded: dict[str, Any] = {}
    uid_cfgs: dict[str, set[str]] = {}
    for cfg in sorted({c for k in todo for c in O4_EXTRA[k]} | set(CONFIGS)):
        d = _fx_dir(out, cfg)
        if not (d / "snap.json").is_file():
            continue
        sj = json.loads((d / "snap.json").read_text())
        s, lay, ri2 = load_ri(Path(sj["snapshot"]), Path(sj["base_toml"]).read_text())
        loaded[cfg] = (sj, s, lay, ri2, unit_entries(sj.get("profile")))
        for u in s.units():
            uid_cfgs.setdefault(u.uid, set()).add(cfg)
    res, static_rows = [], []
    root = out / "negative-extra"
    for kind in todo:
        cands = []
        for cfg in O4_EXTRA[kind]:
            if cfg not in loaded:
                continue
            sj, s, lay, ri2, ent = loaded[cfg]
            seen = set()
            for m in all_mutants(cfg, s, lay, ri2, per_kind=400):
                key = (m.uid, m.patches[0].host_off if m.patches else None)
                if m.kind != kind or key in seen:
                    continue
                seen.add(key)  # every instance (e.g. each of NF/ZF/CF/VF) is its own try
                cands.append((len(uid_cfgs.get(m.uid, ())) > 1, ent.get(m.uid, 0) == 0,
                              -ent.get(m.uid, 0), cfg, m))
        cands.sort(key=lambda x: x[:3])
        entry: dict[str, Any] = {"kind": kind, "fixtures": O4_EXTRA[kind],
                                 "instances": len(cands), "attempts": [],
                                 "ordering": "units unique to one fixture first, then train "
                                             "entries descending"}
        spec_fx = O4_SPECIFIC.get(kind)
        if spec_fx is not None:
            # every instance on the designated detector, icount (gated) then noicount
            # (reported); the other listed fixtures only if one icount instance is missed
            for mode in MODES:
                d = _fx_dir(out, spec_fx, mode)
                if not (d / "snap.json").is_file():
                    entry.setdefault("missing_bases", []).append(f"{mode}/{spec_fx}")
                    continue
                sj = json.loads((d / "snap.json").read_text())
                s, lay, ri2 = load_ri(Path(sj["snapshot"]), Path(sj["base_toml"]).read_text())
                lc = (sj, s, lay, ri2, unit_entries(sj.get("profile")))
                seen_ = set()
                for j, m in enumerate(x for x in all_mutants(spec_fx, s, lay, ri2,
                                                             per_kind=10 ** 6)
                                      if x.kind == kind and x.patches):
                    key = (m.uid, m.patches[0].host_off)
                    if key in seen_:
                        continue
                    seen_.add(key)
                    att, srow = _o4x_attempt(out, root, out / "dyn-o4-extra",
                                             f"{spec_fx}-{mode}-{kind}-s{j}", spec_fx, lc, m,
                                             st, mode=mode)
                    att["round"] = "specific"
                    static_rows.append(srow)
                    entry["attempts"].append(att)
                    log(f"O4x {kind} {mode} {spec_fx} {att.get('sub')}-{att.get('flag')} "
                        f"{m.uid[-14:]}@{key[1]}: {att['verdict']} predicted_exact "
                        f"{att.get('predicted_exact')}")
            entry["specific_detector"] = specific_summary(entry, spec_fx)
            if entry["specific_detector"]["pass"]:
                cands = []  # detected on every instance of the designated detector
            else:
                cands = [x for x in cands if x[3] != spec_fx]
        for k, (shared, _z, nent, cfg, m) in enumerate(cands[:EXTRA_TRIES]):
            att, srow = _o4x_attempt(out, root, out / "dyn-o4-extra", f"{cfg}-{kind}-x{k}",
                                     cfg, loaded[cfg], m, st)
            att["unit_shared_with_other_fixtures"] = shared
            static_rows.append(srow)
            entry["attempts"].append(att)
            det = att["verdict"] == "pass"
            log(f"O4x {kind} try {k} {cfg} {m.uid[-14:]} entries={-nent} shared={shared}: "
                f"{att['verdict']} (o4 {att.get('o4_verdict')}, o6 {att.get('o6')})")
            if det:
                break
        vs = [a["verdict"] for a in entry["attempts"] if a.get("mode", "icount") == "icount"]
        entry["verdict"] = "pass" if "pass" in vs else ("fail" if "fail" in vs else "not_run")
        entry["detected_at_attempt"] = vs.index("pass") if "pass" in vs else None
        if spec_fx is not None:
            # the kind is judged on its designated detector only (every instance there)
            entry["verdict"] = "pass" if entry["specific_detector"]["pass"] else "fail"
        res.append(entry)
        dump(out / "o4-extra.partial.json", res)
    mu["o4_extra"] = [r for r in mu.get("o4_extra") or [] if r["kind"] not in
                      {x["kind"] for x in res}] + res
    mu["static"]["rows"] += static_rows
    mu["static"]["total"] = len(mu["static"]["rows"])
    mu["static"]["rejected"] = sum(1 for r in mu["static"]["rows"] if r["check"]["rejected"])
    mu["static"]["not_rejected"] = [r for r in mu["static"]["rows"] if not r["check"]["rejected"]]
    mu["static"]["kinds"] = sorted({r["kind"] for r in mu["static"]["rows"]})
    dump(out / "mutants.json", mu)
    return res


#: kinds whose O4 coverage is completed by ``o4-cover`` (every executed instance)
O4_COVER_KINDS = ["flag_store_before_helper"]


def _all_fixture_snapshots(out: Path) -> dict[str, tuple]:
    """Every icount gate fixture base in ``OUT/fx`` and ``OUT/fx-b3``, loaded."""
    loaded = {}
    for root in (out / "fx" / "icount", out / "fx-b3" / "icount"):
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if not (d / "snap.json").is_file():
                continue
            sj = json.loads((d / "snap.json").read_text())
            s, lay, ri2 = load_ri(Path(sj["snapshot"]), Path(sj["base_toml"]).read_text())
            loaded[d.name] = (sj, s, lay, ri2, unit_entries(sj.get("profile")))
    return loaded


def step_o4_cover(out: Path, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """O4 coverage completion for a kind that no round detected: enumerate every instance
    ``(unit, host offset)`` of the kind in every gate fixture snapshot (B4 and B3), keep
    the ones whose unit the fixture's train profile entered, and run an O4 control for each
    executed instance that no earlier round tried (on the fixture that enters it most).
    The kind's ``o4_extra`` entry gets the new attempts and a ``coverage`` record."""
    st = selftest(out)
    mu = rd(out / "mutants.json")
    loaded = _all_fixture_snapshots(out)
    order = {c: i for i, c in enumerate(CONFIGS)}
    res = []
    for kind in kinds or O4_COVER_KINDS:
        inst: dict[tuple[str, int], dict[str, Any]] = {}
        for cfg, (sj, s, lay, ri2, ent) in loaded.items():
            for m in all_mutants(cfg, s, lay, ri2, per_kind=1000):
                if m.kind != kind or not m.patches:
                    continue
                r = inst.setdefault((m.uid, m.patches[0].host_off),
                                    {"uid": m.uid, "host_off": m.patches[0].host_off,
                                     "note": m.note, "entries": {}, "mutant": {}})
                r["entries"][cfg] = ent.get(m.uid, 0)
                r["mutant"][cfg] = m
        rounds = [r for r in (mu.get("o4") or []) + (mu.get("o4_extra") or [])
                  if r["kind"] == kind]
        tried = {(a["uid"], a["patches"][0]["host_off"]) for r in rounds
                 for a in r.get("attempts") or [] if a.get("patches")
                 and a.get("verdict") in ("pass", "fail")}
        executed = {k: r for k, r in inst.items() if any(v > 0 for v in r["entries"].values())}
        todo = sorted(k for k in executed if k not in tried)
        log(f"O4 cover {kind}: {len(inst)} instances, {len(executed)} executed, "
            f"{len(tried & set(executed))} tried before, {len(todo)} to run")
        new = []
        root = out / "negative-cover"
        for j, key in enumerate(todo):
            r = executed[key]
            cfg = sorted(r["entries"], key=lambda c: (-r["entries"][c], order.get(c, 99), c))[0]
            att, srow = _o4x_attempt(out, root, out / "dyn-o4-cover", f"{cfg}-{kind}-c{j}",
                                     cfg, loaded[cfg], r["mutant"][cfg], st)
            att["round"] = "cover"
            new.append((att, srow))
            log(f"O4 cover {kind} {cfg} {key[0][-14:]}@{key[1]}: {att['verdict']}")
        ent_ = next((r for r in mu.get("o4_extra") or [] if r["kind"] == kind), None)
        if ent_ is None:
            ent_ = {"kind": kind, "fixtures": [], "instances": 0, "attempts": []}
            mu.setdefault("o4_extra", []).append(ent_)
        ent_["attempts"] = [a for a in ent_["attempts"] if a.get("round") != "cover"] + \
            [a for a, _ in new]
        tried_now = tried | {(a["uid"], a["patches"][0]["host_off"]) for a, _ in new
                             if a.get("verdict") in ("pass", "fail")}
        ent_["coverage"] = {
            "fixtures_enumerated": sorted(loaded),
            "instances": len(inst), "executed_instances": len(executed),
            "executed_instances_tried": len(tried_now & set(executed)),
            "untried_executed": [f"{u}@{o}" for (u, o) in sorted(set(executed) - tried_now)],
            "per_instance": [{"uid": u, "host_off": o, "note": executed[(u, o)]["note"],
                              "entries": executed[(u, o)]["entries"],
                              "tried": (u, o) in tried_now} for (u, o) in sorted(executed)]}
        vs = [a["verdict"] for a in ent_["attempts"]]
        ent_["verdict"] = "pass" if "pass" in vs else ("fail" if "fail" in vs else "not_run")
        ent_["detected_at_attempt"] = vs.index("pass") if "pass" in vs else None
        if kind in O4_SPECIFIC:
            # judged on the designated detector's every-instance record only (o4-extra)
            ent_["verdict"] = "pass" if (ent_.get("specific_detector") or {}).get("pass") \
                else "fail"
        keys = {(r_["uid"], r_["patches"][0]["host_off"], r_["config"]) for _, r_ in new}
        mu["static"]["rows"] = [x for x in mu["static"]["rows"] if not (
            x["kind"] == kind and (x["uid"], x["patches"][0]["host_off"], x["config"]) in keys
            and x.get("round") == "cover")] + [{**r_, "round": "cover"} for _, r_ in new]
        st_ = mu["static"]
        st_["total"] = len(st_["rows"])
        st_["rejected"] = sum(1 for x in st_["rows"] if x["check"]["rejected"])
        st_["not_rejected"] = [x for x in st_["rows"] if not x["check"]["rejected"]]
        st_["kinds"] = sorted({x["kind"] for x in st_["rows"]})
        res.append(ent_)
        dump(out / "mutants.json", mu)
    return res


# ---------------------------------------------------------------------------
# host_abs_in_env: a real instance on F-B4-7 (static with and without env_abs; O4)
# ---------------------------------------------------------------------------

#: the env displacement the repointed host_abs pointer gets: the middle of
#: ``vfp.zregs`` (env + 3312 .. env + 11504 on the gate layout), a region a Cortex-M3 guest
#: (no FPU) never reads. WP19's synthetic mutant uses env + 0x40 (``xregs[0]``); on the
#: real process the env base moves by up to a few hundred bytes between processes whose
#: paths differ (seen: 0xc0 between two diag re-runs), so a displacement deep inside an
#: 8 KiB guest-dead region keeps the written field the same in every run.
HOST_ABS_ENV_DISP = 0x1CF0
HOST_ABS_MARGIN = 0x800  # the field at disp +- margin must still be vfp.zregs
HOST_ABS_CFG = "b4-7"


def host_abs_sites(s: Any, lay: Any) -> list[dict[str, Any]]:
    """HOST_ABS_STORE ops of ``s`` whose pointer register is loaded by the ``movabs reg,
    imm64`` right before the store (FastDyn's UPDATE_MEM / LOG_REG shape): the unit, the
    movabs offset (imm64 at +2), the store and the absolute pointer."""
    import capstone
    from scopt.analysis.hostops import OpKind, build_hostops

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    out = []
    for u in s.units():
        h = build_hostops(u, lay, s.header.code_address)
        sts = [o for o in h.ops if o.kind == OpKind.HOST_ABS_STORE and o.mem_base]
        if not sts:
            continue
        insns = list(md.disasm(bytes(u.code[:u.tc_size]), 0))
        for o in sts:
            mv = next((i for i in insns if i.address + i.size == o.off), None)
            if mv is None or mv.mnemonic != "movabs" or mv.size != 10 or \
                    mv.op_str.split(",")[0].strip() != o.mem_base:
                continue
            imm = int.from_bytes(bytes(u.code[mv.address + 2:mv.address + 10]), "little")
            if imm != o.abs_addr:
                continue
            out.append({"uid": u.uid, "tb_offset": u.tb_offset, "movabs_off": mv.address,
                        "movabs": f"{mv.mnemonic} {mv.op_str}", "store_off": o.off,
                        "store": o.text, "reg": o.mem_base, "abs_addr": imm})
    return out


def host_addr(sc_path: Path, tb_offset: int, off: int) -> int:
    """Run-time host address of byte ``off`` of the unit at ``tb_offset`` in ``sc_path``."""
    from scopt.formats.sc_v5 import read_sc

    f = read_sc(sc_path)
    i = next(k for k, r in enumerate(f.records) if r is not None and r.tb_offset == tb_offset)
    return f.header.code_address + f.records[i].tc_offset + off


def gdb_env_probe(run_dir: Path, out: Path, addr: int, reg: str,
                  timeout_s: float = 300) -> dict[str, Any]:
    """Untimed gdb ``-nx`` diag re-run of ``run_dir`` (scopt.run.diag): stop at the first
    execution of host ``addr`` (a hardware breakpoint), read ``rbp`` (TCG_AREG0 = env on
    the x86-64 backend) and the pointer register ``reg``, then run to the end."""
    from scopt.run.diag import _drop_big, _gdb_cmd, prepare_diag

    prep = prepare_diag(run_dir, out)
    cmds = ["set pagination off", "set confirm off", "set startup-with-shell off",
            "set width 0",
            "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH "
            "nostop noprint pass",
            "break cpu_exec", "run", "delete", f"hbreak *0x{addr:x}", "continue",
            f'printf "WP29ENV rip=%#lx rbp=%#lx ptr=%#lx\\n", $rip, $rbp, ${reg}',
            "x/2i $rip", "delete", "continue"]
    g = _gdb_cmd(timeout_s, cmds, prep["argv"])
    p = subprocess.run(g, cwd=str(prep["cwd"]), env=prep["env"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=timeout_s + 60)
    (Path(prep["out"]) / "gdb.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    m = re.search(r"WP29ENV rip=(0x[0-9a-f]+) rbp=(0x[0-9a-f]+) ptr=(0x[0-9a-f]+)", p.stdout)
    em = re.search(r"exited (?:normally|with code (\d+))", p.stdout)
    _drop_big(Path(prep["out"]))
    return {"run_dir": str(run_dir), "dir": str(prep["out"]), "addr": f"0x{addr:x}",
            "hit": bool(m), "rip": m.group(1) if m else None,
            "env_rbp": m.group(2) if m else None, "ptr": m.group(3) if m else None,
            "qemu_exit": (0 if em and em.group(1) is None else
                          (int(em.group(1), 8) if em else None))}


def check_mutant_env(s: Any, m: Any, store: Path, *, layout: Any, runtime: dict[str, Any],
                     selftest: Any, env_abs: tuple[int, int] | None,
                     candidate_id: str) -> dict[str, Any]:
    """:func:`scopt.b4_mutants.check_mutant` with V12's ``env_abs`` (the process's env
    bounds) passed to ``package_b4``; ``env_abs = None`` is the default (fail-closed) mode."""
    from scopt.analysis.obs_contract import build_contract
    from scopt.passes.base import Outcome, PassResult
    from scopt.passes.eliminate_state_sync import ALL_CLASSES, B4Proposal, package_b4

    con = build_contract(layout, runtime)
    prop = B4Proposal(PassResult(Outcome.CANDIDATE, "mutant", m.kind), {m.uid: m.patches},
                      ALL_CLASSES, con)
    r = package_b4(s, prop, store, candidate_id=candidate_id, layout=layout, runtime=runtime,
                   selftest=selftest, env_abs=env_abs)
    rows = [{"code": x["code"], "check": x["check"], "reason": x.get("reason"),
             "detail": str(x.get("detail"))[:240]}
            for x in (r.validation.rows if r.validation is not None else [])
            if x["result"] == "fail"]
    return {"kind": m.kind, "uid": m.uid, "status": r.status, "rejected": r.status == "rejected",
            "reason": r.reason, "failed_rows": rows,
            "env_abs": None if env_abs is None else [hex(x) for x in env_abs]}


def _bypassed_rows(art: Path) -> list[str]:
    """The failing (bypassed) rows recorded in a negative control's validation.json."""
    v = rd(art / "validation.json") or {}
    rows = v.get("rows") if isinstance(v, dict) else v
    return sorted({f"{r.get('code')}:{r.get('reason') or r.get('check')}" for r in rows or []
                   if isinstance(r, dict) and r.get("result") == "fail"})


def step_host_abs(out: Path) -> dict[str, Any]:
    """Real ``host_abs_in_env`` controls on F-B4-7 (design §9.3 "put a host_abs pointer
    inside env"; b4_mutants.DETECTORS -> F-B4-7). The FastDyn UPDATE_MEM ``movabs reg,
    imm64`` is repointed to ``env + HOST_ABS_ENV_DISP``. The env address is read with gdb
    in a diag re-run of the gate warm reference run (``rbp`` at the store; ``setarch -R``
    makes it deterministic), and checked again at the patched store of the mutant's own
    artifact run. Static: V12 without ``env_abs`` (pointer unbound -> fail closed) and with
    ``env_abs`` (pointer inside env). Dynamic: O4 (O1/O2b against warm)."""
    from scopt.b4_mutants import B4Mutant, check_mutant
    from scopt.passes.base import Patch
    from scopt.passes.eliminate_state_sync import plan_of
    from scopt.run.negative import package_negative
    from scopt.run.oracles import dynamic_oracles

    st = selftest(out)
    cfg = HOST_ABS_CFG
    sj, s, lay, ri2 = load(out, "icount", cfg)
    ent = unit_entries(sj.get("profile"))
    sites = sorted(host_abs_sites(s, lay), key=lambda x: (-ent.get(x["uid"], 0), x["uid"]))
    for x in sites:
        x["unit_entries"] = ent.get(x["uid"], 0)
    hd = out / "host-abs"
    hd.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"config": cfg, "mode": "icount", "qemu": str(GATE),
                           "env_disp": hex(HOST_ABS_ENV_DISP), "sites": sites,
                           "env_bounds_rel": list(lay.env_bounds()),
                           "env_disp_field": str(lay.classify(HOST_ABS_ENV_DISP, 1)),
                           "env_disp_margin_fields": [
                               str(lay.classify(HOST_ABS_ENV_DISP + d, 1))
                               for d in (-HOST_ABS_MARGIN, HOST_ABS_MARGIN)],
                           "static": [], "attempts": []}
    dead = all("vfp.zregs" in str(getattr(lay.classify(HOST_ABS_ENV_DISP + d, 1), "name", ""))
               for d in (-HOST_ABS_MARGIN, 0, HOST_ABS_MARGIN))
    if not sites or not dead:
        doc["verdict"] = "not_run"
        doc["why"] = "no movabs+HOST_ABS_STORE site in the F-B4-7 snapshot" if not sites \
            else "HOST_ABS_ENV_DISP +- margin is not inside vfp.zregs on this layout"
        dump(out / "host-abs.json", doc)
        return doc
    # 1. env address: diag re-run of the gate warm reference run of F-B4-7 (icount)
    ref = out / "dyn" / "icount" / cfg / "oracles" / "c0-i0-r0-reference"
    hot = sites[0]
    a_store = host_addr(Path(sj["sc"]), hot["tb_offset"], hot["store_off"])
    pr = gdb_env_probe(ref, fresh(hd / "env-probe-warm"), a_store, hot["reg"])
    pr["ptr_equals_captured_abs"] = pr["ptr"] is not None and \
        int(pr["ptr"], 16) == hot["abs_addr"]
    doc["env_probe_warm"] = pr
    log(f"host_abs env probe (warm): {pr}")
    if not (pr["hit"] and pr["ptr_equals_captured_abs"]):
        doc["verdict"] = "not_run"
        doc["why"] = "env probe did not reach the UPDATE_MEM store or the run's layout " \
                     "differs from the capture"
        dump(out / "host-abs.json", doc)
        return doc
    env = int(pr["env_rbp"], 16)
    lo, hi = lay.env_bounds()
    env_abs = (env + lo, env + hi)
    newp = env + HOST_ABS_ENV_DISP
    doc["env"] = hex(env)
    doc["env_abs"] = [hex(env_abs[0]), hex(env_abs[1])]
    doc["new_ptr"] = hex(newp)
    store = out / "mutants-store" / "host-abs"
    store.mkdir(parents=True, exist_ok=True)
    root = out / "negative-host-abs"
    rows = []
    for k, site in enumerate(sites):
        u = next(x for x in s.units() if x.uid == site["uid"])
        o = site["movabs_off"] + 2
        p = Patch(target=u.uid, host_off=o, old_bytes=bytes(u.code[o:o + 8]),
                  new_bytes=newp.to_bytes(8, "little"), template_id="b4_nop_store",
                  justification="D1:mutant")
        m = B4Mutant("host_abs_in_env", u.uid, (p,),
                     f"{site['movabs']} -> movabs {site['reg']}, {newp:#x} (env + "
                     f"{HOST_ABS_ENV_DISP:#x}); then {site['store']}", None, "F-B4-7")
        cid = f"wp29-hab-{cfg}-{u.uid[-14:-2]}"
        c0 = check_mutant(s, m, store, layout=lay, runtime=ri2, selftest=st,
                          candidate_id=cid + "-noenv")
        c1 = check_mutant_env(s, m, store, layout=lay, runtime=ri2, selftest=st,
                              env_abs=env_abs, candidate_id=cid + "-env")
        doc["static"].append({"site": k, "uid": u.uid, "without_env_abs": c0.to_json(),
                              "with_env_abs": c1})
        rows.append({"config": cfg, "entries": site["unit_entries"], **m.to_json(),
                     "check": c0.to_json(), "check_with_env_abs": c1})
        log(f"host_abs static {u.uid[-14:]}: noenv {c0.status}/{c0.reason} "
            f"env {c1['status']}/{c1['reason']}")
        # 2. O4 (every site: the hot loop unit, then the once-entered first iteration)
        label = f"{cfg}-host_abs_in_env-{k}"
        rmtree(root / label)
        # the repointed imm64 is an unexplained reference for the importer, so the control
        # is packaged as a byte mutant (scopt.run.negative step 2: the plan's patch is
        # applied by the linker, ``mutate`` is the identity, the re-import and its rows
        # V8/V3 are skipped); V12 (host_abs_aliases_env) and V15 are bypassed as usual
        nr = package_negative(s, plan_of({m.uid: m.patches}), root, label=label,
                              mutant="host_abs_in_env", detector=cfg, layout=lay, runtime=ri2,
                              mutate=lambda link: link)
        att: dict[str, Any] = {"config": cfg, "uid": u.uid, "unit_entries": site["unit_entries"],
                               "note": m.note, "patches": [p.to_json()],
                               "static_rejected": c0.rejected and c1["rejected"],
                               "package": getattr(nr, "status", None),
                               "package_reason": getattr(nr, "reason", None),
                               "packaged_as": "byte mutant (re-import skipped; V8/V3 absent)"}
        if getattr(nr, "path", None) is None:
            att["verdict"] = "not_run"
            doc["attempts"].append(att)
            continue
        att["bypassed_rows"] = _bypassed_rows(Path(nr.path))
        dd = out / "dyn-host-abs" / label
        rmtree(dd)
        d = dynamic_oracles(nr.path, [sj["base_toml"]], dd, oracles=("O1", "O2b"),
                            baseline=sj["snapshot"], reps=1, qemu=GATE, timeout_s=300,
                            inputs=sj["inputs"], fixture_expected=FX / "expected.py",
                            fixture_inputs=[cfg], allow_negative_control=True, quiet=False)
        att["verdict"] = d["verdict"]  # pass = detected
        att["runs_qemu_sha256"] = run_qemu_shas(d)
        att["detail"] = [{o_["oracle"]: (o_["verdict"], str(o_.get("detail"))[:200])
                          for o_ in c_["oracles"]} for c_ in d["cases"]]
        ev = _artifact_files(d, "work/run-artifacts/variable_watch/events.tsv")
        att["events_tsv_differs"] = any(e["artifact"] and e["reference"] and
                                        e["artifact"] != e["reference"] for e in ev)
        # 3. the pointer really is inside env in (a diag re-run of) the mutant's run
        arun = next(dd.rglob("c0-i0-r0-artifact"), None)
        if arun is not None:
            pa = gdb_env_probe(arun, fresh(hd / f"env-probe-{label}"),
                               host_addr(Path(nr.path) / "cache.sc", site["tb_offset"],
                                         site["store_off"]), site["reg"])
            if pa["ptr"] is not None and pa["env_rbp"] is not None:
                pv, ev_ = int(pa["ptr"], 16), int(pa["env_rbp"], 16)
                pa["ptr_minus_env"] = hex(pv - ev_)
                pa["ptr_inside_env"] = ev_ + lo <= pv < ev_ + hi
                pa["field_written"] = str(lay.classify(pv - ev_, 1))
                pa["env_shift_vs_warm_probe"] = hex(ev_ - env)
            att["env_probe_artifact"] = pa
        doc["attempts"].append(att)
        log(f"host_abs O4 {label} entries={site['unit_entries']}: {d['verdict']} "
            f"probe={att.get('env_probe_artifact')}")
    vs = [a["verdict"] for a in doc["attempts"]]
    doc["verdict"] = "pass" if "pass" in vs else ("fail" if "fail" in vs else "not_run")
    dump(out / "host-abs.json", doc)
    # merge into mutants.json: static rows + an O4 entry (round "host-abs")
    mu = rd(out / "mutants.json")
    if mu is not None:
        mu["static"]["rows"] = [r for r in mu["static"]["rows"]
                                if not (r["kind"] == "host_abs_in_env" and
                                        r.get("config") == cfg)] + rows
        st_ = mu["static"]
        st_["total"] = len(st_["rows"])
        st_["rejected"] = sum(1 for r in st_["rows"] if r["check"]["rejected"])
        st_["not_rejected"] = [r for r in st_["rows"] if not r["check"]["rejected"]]
        st_["kinds"] = sorted({r["kind"] for r in st_["rows"]})
        entry = {"kind": "host_abs_in_env", "fixtures": [cfg], "instances": len(sites),
                 "attempts": doc["attempts"], "verdict": doc["verdict"],
                 "detected_at_attempt": vs.index("pass") if "pass" in vs else None,
                 "env": doc.get("env"), "evidence": str(out / "host-abs.json")}
        mu["o4_extra"] = [r for r in mu.get("o4_extra") or []
                          if r["kind"] != "host_abs_in_env"] + [entry]
        dump(out / "mutants.json", mu)
    return doc


# ---------------------------------------------------------------------------
# compute V10 on the gate binary (held-out, icount)
# ---------------------------------------------------------------------------

def step_compute(out: Path, only: list[str] | None = None) -> dict[str, Any]:
    pk = rd(out / "compute" / "package.json")
    base = rd(out / "compute" / "base.json")
    rows = []
    for a in pk["rows"]:
        if only and a["label"] not in only:
            continue
        if not a.get("artifact_dir"):
            rows.append({"label": a["label"], "verdict": "not_run", "why": a.get("status")})
            continue
        label, adir = a["label"], a["artifact_dir"]
        dd = out / "compute-v10" / label
        if dd.exists():
            rmtree(dd)
        cmd = ["validate", adir, "--dynamic", "-c", base["config"], "--out", dd, "--qemu", GATE,
               "--timeout", "300", "--json", "--baseline", base["snapshot"],
               *sum((["--inputs", str(i)] for i in HELDOUT), [])]
        t0 = time.monotonic()
        rc, so = _scopt(cmd, out / "compute-v10" / f"{label}.log")
        try:
            d = json.loads(so)
        except ValueError:
            d = {}
        qs = sorted({str(rd(Path(x["artifact"]["run_dir"]) / "run.json").get("qemu_sha256"))
                     for x in (d.get("runs") or [])
                     if isinstance(x.get("artifact"), dict) and x["artifact"].get("run_dir")
                     and (Path(x["artifact"]["run_dir"]) / "run.json").is_file()})
        rows.append({"label": label, "artifact_dir": adir, "by_class": a.get("by_class"),
                     "rc": rc, "verdict": d.get("verdict"), "appended": d.get("appended"),
                     "failures": d.get("failures"), "runs_qemu_sha256": qs,
                     "seconds": round(time.monotonic() - t0, 1)})
        log(f"V10 {label}: rc={rc} verdict={d.get('verdict')}")
    old = (rd(out / "compute-v10.json") or {}).get("rows", []) if only else []
    rows = [r for r in old if r["label"] not in {x["label"] for x in rows}] + rows
    doc = {"config": base["config"], "inputs": [str(i) for i in HELDOUT], "qemu": str(GATE),
           "rows": rows}
    dump(out / "compute-v10.json", doc)
    return doc


# ---------------------------------------------------------------------------
# close-out re-gate helpers (design/closeout-logreg.md C.2): re-check of the T-B4-1 census
# and the static tests with the current code, byte comparison with the reference gate run
# (WP29) and carrying its timing evidence only when every timed artifact is byte-identical
# ---------------------------------------------------------------------------

#: (mode, config) whose fixture artifact is expected to change in the close-out re-gate:
#: F-B4-10 (coverage units admitted) and the new F-B4-11. Any other change is a finding.
EXPECTED_CHANGES = {(m, c) for m in MODES for c in ("b4-10", B411)}


def step_census(out: Path) -> dict[str, Any]:
    """T-B4-1 census (``scopt b4 census``: admitted, all units, TCG M0) re-run with the
    current code on the existing compute base; ``compute/base.json`` gets the new values and
    keeps the previous ones as ``*_ref`` (they must be equal: the compute workload has no
    coverage block)."""
    d = out / "compute"
    base = rd(d / "base.json")
    snap, ri = base["snapshot"], d / "runtime_identity.v2.json"
    if not ri.is_file():
        raise SystemExit(f"{ri} missing")
    logs = d / "logs-census"
    new = {}
    for k, extra, n in (("census_admitted", [], "06"), ("census_all_units", ["--all-units"], "07"),
                        ("census_tcg_m0", ["--all-units", "--mode", "tcg"], "08")):
        rc, so = _scopt(["b4", "census", snap, "--runtime", ri, *extra], logs / f"{n}-{k}.log")
        try:
            new[k] = json.loads(so)
        except ValueError:
            new[k] = so[-2000:]
        new[k + "_rc"] = rc
    eq = {}
    for k in ("census_admitted", "census_all_units", "census_tcg_m0"):
        if k + "_ref" not in base:
            base[k + "_ref"] = base.get(k)
        eq[k] = isinstance(new[k], dict) and isinstance(base[k + "_ref"], dict) and \
            new[k].get("dead") == base[k + "_ref"].get("dead")
        base[k] = new[k]
    base["census_recheck"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                              "dead_equal_to_ref": eq, "rc": [new[k + "_rc"] for k in (
                                  "census_admitted", "census_all_units", "census_tcg_m0")]}
    dump(d / "base.json", base)
    log(f"census: dead equal to the reference {eq}")
    return base["census_recheck"]


def step_pytest_static(out: Path) -> dict[str, Any]:
    """T-B4-1/T-B4-2/T-B4-2b static tests with the current code -> ``pytest-t-b4-static.log``."""
    p = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/unit/test_syncdead.py", "tests/acceptance/test_b4_static.py",
                        "tests/unit/test_icount_entry_sfb.py"], cwd=str(WT), capture_output=True,
                       text=True, env={**os.environ, "PYTHONPATH": str(WT)})
    (out / "pytest-t-b4-static.log").write_text(p.stdout + p.stderr)
    log(f"pytest t-b4-static: rc {p.returncode}: {(p.stdout.strip().splitlines() or [''])[-1]}")
    return {"rc": p.returncode}


def step_compare_ref(out: Path, ref: Path) -> dict[str, Any]:
    """Output ``cache.sc`` sha256 of every fixture and compute artifact of this run against
    the reference gate run ``ref`` (``package.json``, ``compute/package.json``). Only
    ``EXPECTED_CHANGES`` may differ; anything else is listed in ``unexpected_changes``."""
    pk = {(r["mode"], r["config"]): r for r in (rd(out / "package.json") or {}).get("rows", [])}
    rk = {(r["mode"], r["config"]): r for r in (rd(ref / "package.json") or {}).get("rows", [])}
    rows, unexpected = [], []
    for key in sorted(set(pk) | set(rk)):
        a, b = pk.get(key) or {}, rk.get(key) or {}
        eq = a.get("cache_sc_sha256") == b.get("cache_sc_sha256") and \
            a.get("status") == b.get("status") and a.get("by_class") == b.get("by_class")
        rows.append({"mode": key[0], "config": key[1], "equal": eq,
                     "status": [b.get("status"), a.get("status")],
                     "by_class": [b.get("by_class"), a.get("by_class")],
                     "cache_sc_sha256": [b.get("cache_sc_sha256"), a.get("cache_sc_sha256")],
                     "expected_change": key in EXPECTED_CHANGES})
        if not eq and key not in EXPECTED_CHANGES:
            unexpected.append(f"{key[0]}/{key[1]}")
    cp = {r["label"]: r for r in (rd(out / "compute" / "package.json") or {}).get("rows", [])}
    cr = {r["label"]: r for r in (rd(ref / "compute" / "package.json") or {}).get("rows", [])}
    crow = []
    for lab in sorted(set(cp) | set(cr)):
        a, b = cp.get(lab) or {}, cr.get(lab) or {}
        eq = bool(a.get("cache_sc_sha256")) and a.get("cache_sc_sha256") == b.get(
            "cache_sc_sha256") and a.get("by_class") == b.get("by_class")
        crow.append({"label": lab, "equal": eq,
                     "cache_sc_sha256": [b.get("cache_sc_sha256"), a.get("cache_sc_sha256")]})
        if not eq:
            unexpected.append(f"compute/{lab}")
    doc = {"ref": str(ref), "fixtures": rows, "compute": crow,
           "unexpected_changes": unexpected, "pass": not unexpected}
    dump(out / "compare-ref.json", doc)
    log(f"compare-ref: unexpected changes {unexpected}")
    return doc


def step_carry_timing(out: Path, ref: Path) -> dict[str, Any]:
    """Carry the reference run's timing evidence (``timing-analysis.json``,
    ``timing-load.log``, ``timing-reading.md``; copied, sha256 recorded in ``carried.json``)
    only when the ``cache.sc`` of every timed candidate arm of ``ref/timing/campaign.json``
    is byte-identical to an artifact of this run (``compute/package.json``). Otherwise the
    timing must be re-run (``wp29_timing.sh`` under the lock) and this step refuses."""
    camp = rd(ref / "timing" / "campaign.json") or {}
    arms = (camp.get("campaign") or {}).get("arms") or []
    have = {r.get("cache_sc_sha256"): r["label"]
            for r in (rd(out / "compute" / "package.json") or {}).get("rows", [])
            if r.get("cache_sc_sha256")}
    rows = []
    for a in arms:
        src = Path(a["source"])
        f = src / "cache.sc"
        sha = sha_file(f) if f.is_file() else None
        if a.get("role") == "reference":
            # warm: the base snapshot itself; equal by the sha256 manifest of the bases
            rows.append({"arm": a["name"], "role": "reference", "source": str(src),
                         "cache_sc_sha256": sha, "equal": sha is not None})
            continue
        rows.append({"arm": a["name"], "role": a.get("role"), "source": str(src),
                     "cache_sc_sha256": sha, "equal": sha is not None and sha in have,
                     "this_run_label": have.get(sha)})
    ok = bool(rows) and all(r["equal"] for r in rows)
    doc: dict[str, Any] = {"ref": str(ref), "timed_arms": rows, "byte_identical": ok}
    if ok:
        copied = {}
        for n in ("timing-analysis.json", "timing-load.log", "timing-reading.md"):
            s_ = ref / n
            if s_.is_file():
                shutil.copy2(s_, out / n)
                copied[n] = {"from": str(s_), "sha256": sha_file(out / n)}
        doc["carried"] = copied
    car = rd(out / "carried.json") or {}
    car["timing"] = doc
    dump(out / "carried.json", car)
    log(f"carry-timing: byte_identical {ok}")
    if not ok:
        raise SystemExit("a timed artifact changed: re-run wp29_timing.sh under the lock")
    return doc


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

def step_verdict(out: Path) -> dict[str, Any]:
    from verdict_wp29 import build_verdict

    doc = build_verdict(out)
    dump(out / "verdict.json", doc)
    print(json.dumps({k: v.get("pass") if isinstance(v, dict) else v
                      for k, v in doc["gate"].items()}, indent=1))
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "runs/wp29")
    ap.add_argument("--only", default="", help="package/dynamic: comma list of cfg or mode/cfg")
    ap.add_argument("--no-o4", action="store_true")
    ap.add_argument("--ref", type=Path, default=ROOT / "runs/wp29",
                    help="compare-ref / carry-timing: the reference gate run")
    ap.add_argument("steps", nargs="*", default=["gate", "compute-base", "package",
                                                 "package-compute", "compute-host-abs", "package-sfb-b4", "dynamic", "deletions", "b4-10", "mutants", "o4-extra", "host-abs", "o4-cover",
                                                 "compute", "verdict"])
    a = ap.parse_args(argv)
    out = a.out.resolve()
    only = [x for x in a.only.split(",") if x] or None
    for st in a.steps:
        log(f"step {st}")
        if st == "gate":
            step_gate(out)
        elif st == "compute-base":
            step_compute_base(out)
        elif st == "package":
            step_package(out, only)
        elif st == "package-compute":
            step_package_compute(out)
        elif st == "sfb-row":
            step_sfb_row(out)
        elif st == "compute-host-abs":
            step_compute_host_abs(out)
        elif st == "recheck-compute":
            step_recheck_compute(out)
        elif st == "package-sfb-b4":
            step_package_sfb_b4(out)
        elif st == "dynamic":
            step_dynamic(out, only)
        elif st == "o4-extra":
            step_o4_extra(out, only)
        elif st == "host-abs":
            step_host_abs(out)
        elif st == "o4-cover":
            step_o4_cover(out, only)
        elif st == "deletions":
            step_deletions(out, only)
        elif st == "b4-10":
            step_b4_10(out)
        elif st == "mutants":
            step_mutants(out, o4=not a.no_o4)
        elif st == "compute":
            step_compute(out, only)
        elif st == "census":
            step_census(out)
        elif st == "pytest-static":
            step_pytest_static(out)
        elif st == "compare-ref":
            step_compare_ref(out, a.ref.resolve())
        elif st == "carry-timing":
            step_carry_timing(out, a.ref.resolve())
        elif st == "verdict":
            step_verdict(out)
        else:
            raise SystemExit(f"unknown step {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
