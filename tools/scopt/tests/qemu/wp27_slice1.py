#!/usr/bin/env python3
"""WP27 integration stage: slice 1 of the first vertical slice (plan ``## WP27`` steps 6-7,
T-B3-8; design §12 "First vertical slice", §5.5, §9.4) on the **frozen gate binary**
(``runs/wp22/gate-binary.json``: ``qint-v6-gate-1``, exe sha256 ``56d8bfb3…``).
Nothing is timed and no timing lock is taken.

    wp27_slice1.py --out runs/wp27/slice1 [--steps bases,summary,artifact,dynamic,o4,xrehy]
                   [--modes icount,noicount]

Run from the scopt worktree with the tools-env python. Every QEMU run goes through the
scopt/FastDyn launcher (``setarch x86_64 -R``, timeout, fresh run directory,
``supercache_flat = false``); gdb is always ``gdb -nx -batch`` (hardware breakpoints) with
cwd = FastDyn's cwd, never inside a QEMU tree.

Steps:

* ``bases``: WP24 phase F-B3-1 (``b3-1.toml.in``) and its coverage twin F-B3-23
  (``b3-23.toml.in``: F-B3-1 with ``coverage = true``, ``edge_coverage = true``) prepared,
  imported and profiled (``steady@b3_bench_start``, untimed) on the gate binary in icount
  (shift 0) and noicount (WP20's driver); RuntimeIdentity v2 of each base
  (``scopt admission identity``: gate exe sha, EnvLayout ``sha256:32440e25…``).
* ``summary``: ``scopt return-edges summaries --certify`` on the F-B3-1 bases (the
  CalleeSummary row the slice-1 FactRef must cite).
* ``artifact`` (step 6): ``scopt optimize --pipeline b3 --b3 --disable chain-returns
  --disable chain-static`` (the ``b3-spec`` arm) on each F-B3-1 base. Checked from the
  output ``cache.sc`` (``read_sc6``, independent of the manager): ``.sc`` v6 with exactly
  one variant, one slot (SPEC, R = ``f_b3_1`` -> Vc = ``vc_b3_1``) and one link; the
  variant bytes equal §3.5's full-pipeline form (capstone diff against the general C);
  V1-V8, V11-V21, V23 pass (V9/V22 skip); CP/BF/DHO ledger rows with their byte deltas;
  the multi_tb FactRef cites the CalleeSummary row. Then ``scopt publish`` (verified
  copy into a store); the published copy is what the dynamic step deploys.
* ``dynamic`` (step 7, T-B3-8 O1/O2a/O2b/O3): ``scopt validate --dynamic --oracles
  O1,O2a,O2b,O3,O5`` on the gate binary against the base's warm cache: O1 with the
  fixture's per-mode oracle, O2a ``-d int``, O2b, O3 = loader stats ``links_armed == 1``
  and gdb hit counts of ``C'.tc.ptr``, R's stub ``S`` and its fallback ``F``; "≈ N" is
  C' hits in [N-5, N] with N = 1000 iterations, F hits in [1, 5] (the warm-up: the link
  arms after C's first activation, §5.5). The v6 stats file of the artifact run is read
  directly too (``invariant_violations == 0``, no disarm).
* ``o4`` (step 7, T-B3-8 O4): a **falsified-fact** negative control. The slice-1 plan is
  rebuilt by hand (same R, slot, C, guard key; NativeLinker v2), first as a **positive
  control** with the manager's own patches and FactRef (packaged with ``package_b3`` and
  hostsym: must publish and give the manager's variant bytes), then as the mutant: the
  variant folded for the claimed fact ``r0 == 1`` (the ``jne`` becomes ``jmp``: the
  ``cbz r0`` is never taken), packaged by WP26's ``package_negative`` (V12-V17 bypassed;
  hostsym must reject it statically: V14). ``scopt validate --dynamic --negative-control
  --oracles O1,O2a,O2b,O3``: O4 pass = the mutant changed O1 (and O3 shows it ran).
* ``xrehy`` (step 7, X-REHY with the variant, coverage config, design §5.5 / §15): the
  real pipeline on the F-B3-23 base (recorded; WP20 found R's facts refused under
  coverage). The coverage C contains FastDyn's inline LOG_REG ring store, so a variant of
  it is the case X-REHY asks about (C' is not rehydrated; it runs C's baked pointers). The
  variant is built with the same three §3.5 patches located by capstone in the coverage C,
  packaged with ``package_b3`` + hostsym (``allow_uncertified`` only if hostsym cannot
  certify; then it is test-only and recorded as such), deployed on the gate binary and
  checked with O1/O2a/O2b/O3/O6: O6 compares FastDyn's LOG_REG ring
  (``core_cc_list.log_buf``, last 16384 TB start PCs, read at ``benchmark_end`` by the
  diag twin) with warm's, and needs C' hits > 0.

Writes ``OUT/wp27-slice1.json`` and ``OUT/verdict.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
sys.path.insert(0, str(WT))
sys.path.insert(0, str(HERE))
ROOT = Path("/data/extra_copy/fastdyn")
FIX = ROOT / "fixtures/b3"
PY = sys.executable
CPUS = os.environ.get("WP27_CPUS", "16-23")
os.environ.setdefault("WP21_CPUS", CPUS)

import wp21_integ as W  # noqa: E402  (bases, variant_diff, check_case, _validation, cli)
import wp27_slice0 as S0  # noqa: E402  (step_summary, _ledger_pass_rows, _multi_tb_facts)

N_ITER = 1000  # F-B3-1 / F-B3-23: KERNEL b3_1, f_b3_1, 1000 iterations
B3_SPEC_DISABLE = ("chain-returns", "chain-static")
SELF_PASSES = ("constant-propagation", "branch-folding", "dead-host-ops")
EXPECT_DELTA = {"constant-propagation": 3, "branch-folding": 6, "dead-host-ops": 4}
# V10 is dynamic; V9 is identity only; V22 needs WP23 (not approved)
WANT_SKIP = {"V9", "V22"}


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def sha256(p: Path) -> str:
    return W.sha256(p)


def cli(args: list[str], logf: Path, *, timeout: float = 1800) -> tuple[int, str]:
    return W.cli(args, logf, timeout=timeout)


def fixture_addr(sym: str) -> int:
    return S0.fixture_addr(sym)


def _rmtree(p: Path) -> None:
    """Remove a run/store directory (store artifacts are read-only: ``remove_tree``)."""
    if p.exists():
        from scopt.formats.artifact import remove_tree

        remove_tree(p)


# ---------------------------------------------------------------------------
# bases
# ---------------------------------------------------------------------------

def step_bases(out: Path, modes: list[str], exe: Path, exe_sha: str) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for cfg in ("1", "23"):
        r = W.step_bases(out, modes, exe, exe_sha, cfg=cfg)
        for m in modes:
            ri = json.loads(W.base_info(out, m, cfg)["ri"].read_text())
            r[m]["admission"] = ri.get("admission")
        res[cfg] = r
    for m in modes:
        res[m] = {"ok": all(res[c][m]["ok"] for c in ("1", "23"))}
    return res


# ---------------------------------------------------------------------------
# step 6: the slice-1 artifact (b3-spec arm)
# ---------------------------------------------------------------------------

def _optimize(out: Path, b: dict[str, Any], tag: str, store: Path,
              disable: tuple[str, ...] = B3_SPEC_DISABLE) -> dict[str, Any]:
    args = ["optimize", str(b["snapshot"]), "--pipeline", "b3", "--b3", "--store", str(store),
            "--profile", str(b["profile"]), "--runtime-identity", str(b["ri"]), "--prep",
            str(b["prep"]), "--elf", str(b["elf"]), "--selftest",
            str(out / "hostsym_selftest.json"), "--json"]
    for p in disable:
        args += ["--disable", p]
    t0 = time.time()
    rc, so = cli(args, out / "logs" / f"optimize-{tag}.log")
    try:
        doc = json.loads(so)
    except ValueError:
        return {"exit": rc, "error": "no JSON on stdout", "ok": False}
    return {"exit": rc, "seconds": round(time.time() - t0, 1), "status": doc.get("status"),
            "reason": doc.get("reason"), "artifact_dir": doc.get("artifact_dir"),
            "artifact_id": doc.get("artifact_id"), "rejection_path": doc.get("rejection_path"),
            "pass_byte_delta": doc.get("pass_byte_delta"), "downgraded": doc.get("downgraded"),
            "passes": [{"pass": p.get("pass"), "outcome": p.get("outcome"),
                        "reason": p.get("reason"),
                        "refused": {k: v for k, v in (p.get("stats") or {}).items()
                                    if k.startswith("refused:")}}
                       for p in doc.get("passes", [])]}


def ext_summary(sc: Path) -> dict[str, Any]:
    """Version, variants, slots and links of a ``.sc`` file (``read_sc6``)."""
    from scopt.formats.sc_v6 import read_sc6

    f = read_sc6(sc)
    d: dict[str, Any] = {"version": f.version, "loadable": f.loadable, "problem": f.problem}
    if f.ext is None:
        d.update(variants=0, slots=[], links=[])
        return d
    d["variants"] = len(f.ext.variants)
    d["slots"] = [{"kind": {1: "GENERAL", 2: "SPEC"}.get(s.kind, s.kind), "slot_n": s.slot_n,
                   "src_tb_offset": hex(s.src_tb_offset), "target_vaddr": hex(s.target_vaddr)}
                  for s in f.ext.slots]
    d["links"] = [{"slot": lk.slot_index, "variant": lk.variant_index} for lk in f.ext.links]
    d["variant_bodies_sha256"] = [__import__("hashlib").sha256(bytes(b)).hexdigest()
                                  for b in f.variant_bodies]
    d["artifact_id8"] = bytes(f.ext.artifact_id8).hex()
    return d


def step_artifact(out: Path, modes: list[str], summary: dict[str, Any]) -> dict[str, Any]:
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    vc = fixture_addr("vc_b3_1")
    rfn = fixture_addr("f_b3_1")
    res: dict[str, Any] = {}
    for m in modes:
        b = W.base_info(out, m)
        ri = json.loads(b["ri"].read_text())
        lay = find_envlayout(ri["qemu_sha256"])
        in_vaddrs = [u.vaddrs[0] if u.vaddrs else None
                     for u in open_snapshot(b["snapshot"]).units()]
        store = out / "store" / m
        r = _optimize(out, b, f"b3spec-{m}", store)
        if r.get("artifact_dir"):
            adir = Path(r["artifact_dir"])
            r["cache_sc_sha256"] = sha256(adir / "cache.sc")
            r["ext"] = ext_summary(adir / "cache.sc")
            r["validation"] = W._validation(adir)
            vd = W.variant_diff(adir / "cache.sc", lay.offset("regs[0]"), in_vaddrs)
            r["sc"] = vd
            r["bytes_ok"], r["bytes_why"] = W.check_case("full", vd)
            codes = r["validation"]["codes"]
            r["static_rows_ok"] = (r["validation"]["verdict"] == "accepted" and
                                   all(codes.get(c) == "pass" for c in W.STATIC_ROWS
                                       if c not in WANT_SKIP) and
                                   all(codes.get(c) in ("pass", "skip") for c in WANT_SKIP))
            e = r["ext"]
            slot = (e.get("slots") or [{}])[0]
            r["one_variant_one_spec_slot_one_link"] = (
                e.get("version") == 6 and e.get("variants") == 1 and len(e.get("slots")) == 1 and
                slot.get("kind") == "SPEC" and len(e.get("links")) == 1 and
                e["links"][0] == {"slot": 0, "variant": 0})
            spec = [s for s in vd["slots"] if s["kind"] == "SPEC"]
            r["spec_r_c"] = spec
            r["spec_is_f_b3_1_to_vc"] = (len(spec) == 1 and spec[0]["src_vaddr"] == hex(rfn) and
                                         int(spec[0]["target_vaddr"], 16) == vc)
            led = S0._ledger_pass_rows(adir)
            r["ledger"] = led
            delta = r.get("pass_byte_delta") or {}
            r["ledger_ok"] = all((led.get(p) or {}).get("verdict") == "candidate" and
                                 (led.get(p) or {}).get("bytes_changed") == delta.get(p) ==
                                 EXPECT_DELTA[p] for p in SELF_PASSES)
            fr = S0._multi_tb_facts(adir)
            r["multi_tb_facts"] = fr
            hashes = set((summary.get(m) or {}).get("row_hashes") or [])
            r["factref_summary_row_ok"] = bool(fr) and all(
                f.get("summary_row_hash") in hashes and f.get("field") == "r0" and
                f.get("source") == "patch" and int(str(f.get("value")), 0) == 0 for f in fr)
            # verified copy into a store (scopt publish): what the dynamic step deploys
            pub = out / "pubstore" / m
            rc, so = cli(["publish", str(adir), "--store", str(pub), "--json"],
                         out / "logs" / f"publish-{m}.log")
            try:
                pj = json.loads(so)
            except ValueError:
                pj = {}
            r["publish"] = {"exit": rc, "artifact_id": pj.get("artifact_id"),
                            "artifact_dir": pj.get("artifact_dir"), "status": pj.get("status")}
            r["publish"]["same_id"] = str(pj.get("artifact_id", "")).removeprefix(
                "sha256:") == adir.name
            r["publish"]["cache_sc_equal"] = bool(pj.get("artifact_dir")) and sha256(
                Path(pj["artifact_dir"]) / "cache.sc") == r["cache_sc_sha256"]
        r["ok"] = (r.get("status") == "published" and r.get("bytes_ok") and
                   r.get("static_rows_ok") and r.get("one_variant_one_spec_slot_one_link") and
                   r.get("spec_is_f_b3_1_to_vc") and r.get("ledger_ok") and
                   r.get("factref_summary_row_ok") and (r.get("publish") or {}).get("exit") == 0
                   and (r.get("publish") or {}).get("same_id") and
                   (r.get("publish") or {}).get("cache_sc_equal"))
        log("artifact", m, r.get("status"), r.get("reason"), r.get("pass_byte_delta"),
            "ext", {k: (r.get("ext") or {}).get(k) for k in ("version", "variants", "links")},
            "bytes_ok", r.get("bytes_ok"), r.get("bytes_why"), "rows_ok", r.get("static_rows_ok"),
            "ledger_ok", r.get("ledger_ok"), "factref", r.get("factref_summary_row_ok"),
            "publish", r.get("publish"), "ok", r["ok"])
        res[m] = r
    return res


# ---------------------------------------------------------------------------
# step 7: O1/O2a/O2b/O3/O5 on the gate binary
# ---------------------------------------------------------------------------

def probe_labels(sc: Path) -> dict[str, str]:
    from scopt.run.diag import artifact_probe_addrs

    lab = {}
    for p in artifact_probe_addrs(sc):
        if p["kind"] == "variant":
            lab.setdefault("C'", p["what"])
        elif p["kind"] == "stub":
            lab.setdefault("S", p["what"])
        elif p["kind"] == "fallback":
            lab.setdefault("F", p["what"])
    return lab


def run_dynamic(out: Path, tag: str, adir: Path, b: dict[str, Any], exe: Path, timeout: float,
                oracles: str, o3: dict[str, Any] | None, *, fixture_input: str,
                negative: bool = False, extra: list[str] = ()) -> dict[str, Any]:
    od = out / "dyn" / tag
    _rmtree(od)
    args = ["validate", str(adir), "--dynamic", "-c", str(b["toml"]), "--baseline",
            str(b["snapshot"]), "--inputs", str(b["input"]), "--fixture-input", fixture_input,
            "--fixture-expected", str(FIX / "expected.py"), "--timeout", str(timeout),
            "--qemu", str(exe), "--out", str(od), "--json", "--oracles", oracles, *extra]
    if o3 is not None:
        args += ["--o3", json.dumps(o3)]
        for lab in (o3.get("hits") or {}):
            args += ["--hit-target", lab]
    if negative:
        args.append("--negative-control")
    rc, _ = cli(args, out / "logs" / f"dynamic-{tag}.log")
    dj = od / "dynamic_oracles.json"
    doc = json.loads(dj.read_text()) if dj.is_file() else None
    case = ((doc or {}).get("cases") or [{}])[0]
    runs = case.get("runs") or {}
    r: dict[str, Any] = {"exit": rc, "file": str(dj), "verdict": (doc or {}).get("verdict"),
                         "failures": (doc or {}).get("failures"),
                         "incomplete": (doc or {}).get("incomplete"),
                         "oracles": {o["oracle"]: o["verdict"] for o in case.get("oracles", [])},
                         "details": {o["oracle"]: o.get("detail") for o in case.get("oracles", [])},
                         "data": {o["oracle"]: o.get("data") for o in case.get("oracles", [])},
                         "runs": runs,
                         "hits": {t["what"]: t.get("hits")
                                  for t in ((case.get("hits") or {}).get("targets") or [])}}
    for side in ("artifact", "reference"):
        rd = (runs.get(side) or {}).get("run_dir")
        if not rd:
            continue
        rj_p = Path(rd) / "run.json"
        if rj_p.is_file():
            rj = json.loads(rj_p.read_text())
            r.setdefault("run_json", {})[side] = {
                **{k: rj.get(k) for k in ("verdict", "exit_code", "appended", "qemu_sha256")},
                "results": S0.guest_results(rj.get("guest_norm") or [])}
        sp = Path(rd) / "v6-stats.json"
        if sp.is_file():
            st = json.loads(sp.read_text())
            r.setdefault("v6_stats", {})[side] = st
    return r


def _stats_view(st: dict[str, Any] | None) -> dict[str, Any]:
    if not st:
        return {}
    return {k: st.get(k) for k in ("loaded", "links_armed", "links", "variants", "slots",
                                   "disarm_tlb_events", "disarm_tlb_links", "disarm_flush_links",
                                   "disarm_forbid_links", "invariant_checks",
                                   "invariant_violations", "forbidden", "forbid_reason",
                                   "variant_exits", "variant_entry_exits", "refused")
            if k in st}


def step_dynamic(out: Path, modes: list[str], art: dict[str, Any], exe: Path, exe_sha: str,
                 timeout: float) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for m in modes:
        a = art.get(m) or {}
        pdir = (a.get("publish") or {}).get("artifact_dir")
        if not (a.get("ok") and pdir):
            res[m] = {"ok": False, "why": "no published slice-1 artifact"}
            continue
        b = W.base_info(out, m)
        lab = probe_labels(Path(pdir) / "cache.sc")
        o3 = {"stats": {"links_armed": "==1"},
              "hits": {lab["C'"]: f">={N_ITER - 5}", lab["S"]: f">={N_ITER - 5}",
                       lab["F"]: "<=5"}}
        r = run_dynamic(out, f"slice1-{m}", Path(pdir), b, exe, timeout, "O1,O2a,O2b,O3,O5", o3,
                        fixture_input="1")
        r["probe_labels"] = lab
        h = r["hits"]
        c_hits, s_hits, f_hits = h.get(lab["C'"]), h.get(lab["S"]), h.get(lab["F"])
        st = (r.get("v6_stats") or {}).get("artifact")
        sv = _stats_view(st)
        r["stats_view"] = sv
        rjs = r.get("run_json") or {}
        items = {
            "o1_equal_warm": r["oracles"].get("O1") == "pass",
            "o2a_equal_warm": r["oracles"].get("O2a") == "pass",
            "o2b_equal_warm": r["oracles"].get("O2b") == "pass",
            "o3_pass": r["oracles"].get("O3") == "pass",
            "o5_reported": r["oracles"].get("O5") == "reported",
            "verdict_pass": r["verdict"] == "pass" and r["exit"] == 0,
            "armed_eq_1": sv.get("links_armed") == 1,
            "no_invariant_violation": sv.get("invariant_violations") == 0,
            "c_prime_hits_about_n_minus_warmup": isinstance(c_hits, int) and
            N_ITER - 5 <= c_hits <= N_ITER - 1,
            "f_taken_only_in_warmup": isinstance(f_hits, int) and 1 <= f_hits <= 5,
            "c_prime_plus_f_eq_stub": isinstance(s_hits, int) and isinstance(c_hits, int) and
            isinstance(f_hits, int) and c_hits + f_hits == s_hits,
            "z_n_nz_0_both": all((rjs.get(s) or {}).get("results") == {0: N_ITER, 1: 0}
                                 for s in ("artifact", "reference")),
            "proofs_pass": all((rjs.get(s) or {}).get("verdict") == "PASS" and
                               (rjs.get(s) or {}).get("appended") == 0
                               for s in ("artifact", "reference")),
            "gate_binary": all(str((rjs.get(s) or {}).get("qemu_sha256", "")).endswith(exe_sha)
                               for s in ("artifact", "reference")),
        }
        r["items"] = items
        r["ok"] = all(items.values())
        log("dynamic", m, r["verdict"], r["oracles"], "hits", h, "stats", sv, "ok", r["ok"])
        res[m] = r
    return res


# ---------------------------------------------------------------------------
# hand-built slice-1 plan (O4 control and mutant; X-REHY variant)
# ---------------------------------------------------------------------------

def _unit_at(s: Any, vaddr: int) -> Any:
    hit = [u for u in s.units() if u.vaddrs and (u.vaddrs[0] & ~1) == vaddr]
    if len(hit) != 1:
        raise SystemExit(f"{len(hit)} units at {vaddr:#x}")
    return hit[0]


def find_sites(code: bytes, r0_off: int) -> dict[str, tuple[int, int, str]]:
    """§3.5's three sites in C's host code: the r0 env load ``mov r32,[rbp+regs[0]]``
    (L), the ``test`` of that register (T) and the ``jcc`` after it (J), as
    ``(offset, size, asm)``."""
    cs = W._cs()
    ins = list(cs.disasm(bytes(code), 0))
    want = "dword ptr [rbp]" if r0_off == 0 else f"dword ptr [rbp + {r0_off:#x}]"
    for k, i in enumerate(ins):
        if i.mnemonic == "mov" and i.op_str.endswith(want):
            reg = i.op_str.split(",")[0]
            t = next((x for x in ins[k + 1:k + 4] if x.mnemonic == "test" and
                      x.op_str == f"{reg}, {reg}"), None)
            if t is None:
                continue
            j = ins[ins.index(t) + 1]
            if not j.mnemonic.startswith("j") or j.mnemonic == "jmp":
                continue
            return {n: (x.address, x.size, f"{x.mnemonic} {x.op_str}")
                    for n, x in (("L", i), ("T", t), ("J", j))}
    return {}


def nopfill(n: int) -> bytes:
    """Register-free nops as the B3 passes write them (``66 90`` pairs, then ``90``)."""
    return b"\x66\x90" * (n // 2) + b"\x90" * (n % 2)


def jcc_to_jmp(code: bytes, off: int, size: int) -> bytes:
    """``0f 8x rel32`` -> ``e9 rel32+1; 90`` (same target), ``7x rel8`` -> ``eb rel8``."""
    import struct

    if size == 6 and code[off] == 0x0F and 0x80 <= code[off + 1] <= 0x8F:
        rel = struct.unpack_from("<i", code, off + 2)[0]
        return b"\xe9" + struct.pack("<i", rel + 1) + b"\x90"
    if size == 2 and 0x70 <= code[off] <= 0x7F:
        return b"\xeb" + bytes([code[off + 1]])
    raise ValueError(f"not a jcc at {off:#x}")


def build_plan(s: Any, *, r_vaddr: int, vc: int, patches: tuple[Any, ...],
               facts: tuple[Any, ...]) -> Any:
    """The slice-1 plan: R grown with one SPEC slot (canonical guard) to Vc, one
    variant of C with ``patches`` and ``facts`` (as ``scopt.link`` builds for the
    manager; the guard key comes from C's record, as in WP22's stress builder)."""
    from scopt.guard import CS_CONDEXEC_MASK, CS_THUMB, PSTATE_SS, GuardTarget
    from scopt.link import (B3Plan, GuardedExitSpec, VariantSpec, capture_join, exit_class_of,
                            free_slots, lookup_tails, tail_problem)

    R, C = _unit_at(s, r_vaddr), _unit_at(s, vc)
    tails = lookup_tails(R)
    if len(tails) != 1 or tail_problem(R, tails[0], s.header.code_address):
        raise SystemExit(f"R {r_vaddr:#x} has no single admissible lookup tail")
    t = tails[0]
    jr = capture_join(s)
    xc = exit_class_of(R, t, jr.slots[R.slot].chunk.guest_bytes)
    n = list(free_slots(R))[0]
    g = GuardedExitSpec(t.off, xc if xc in ("return", "static") else "static",
                        (GuardTarget(vc, n, "SPEC"),), C.key.flags & ~PSTATE_SS,
                        (C.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB)
    return B3Plan(grow={R.uid: (g,)},
                  variants=(VariantSpec(C.uid, R.uid, n, patches=patches, facts=facts),)), R, C, n


def _hs(lay: Any, ri: dict[str, Any], rows: list[dict[str, Any]], selftest: Path) -> Any:
    from scopt.analysis.obs_contract import build_contract
    from scopt.guard import GuardOffsets
    from scopt.validate_sym.link_check import hostsym_inputs, summary_provenance

    from scopt.validate_sym.selftest import load_or_run

    st = load_or_run(str(selftest))
    return hostsym_inputs(layout=lay, contract=build_contract(lay, ri),
                          offsets=GuardOffsets.from_layout(lay), selftest=st,
                          provenance=summary_provenance(rows))


def _summary_rows(out: Path, m: str) -> list[dict[str, Any]]:
    p = out / "summary" / m / "summaries/callee_summaries.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.is_file() else []


def _fact_from_row(fr: dict[str, Any]) -> Any:
    from scopt.passes.base import FactRef

    return FactRef(fr["fact_id"], fr["field"], int(str(fr["value"]), 0), int(fr["def_insn"]),
                   fr["source"], fr["cert_hash"], fr["summary_row_hash"])


def _rows_summary(val: Any) -> dict[str, Any]:
    codes: dict[str, str] = {}
    rank = {"fail": 3, "not_run": 2, "pass": 1, "skip": 0}
    for r in val.rows:
        c, x = r.get("code"), r.get("result")
        if c not in codes or rank.get(x, 3) > rank.get(codes[c], 3):
            codes[c] = x
    return {"codes": codes, "fails": [{"code": r["code"], "check": r.get("check"),
                                       "reason": r.get("reason"),
                                       "detail": str(r.get("detail"))[:300]}
                                      for r in val.rows if r.get("result") == "fail"]}


# ---------------------------------------------------------------------------
# step 7: O4 falsified-fact negative control
# ---------------------------------------------------------------------------

def step_o4(out: Path, modes: list[str], art: dict[str, Any], exe: Path, exe_sha: str,
            timeout: float) -> dict[str, Any]:
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.formats.sc_v6 import read_sc6
    from scopt.importer.import_cache import open_snapshot
    from scopt.passes.base import Patch
    from scopt.run.negative import BYPASS_ROWS, package_negative
    from scopt.validate import package_b3
    from scopt.validate_sym.link_mutants import synthetic_fact

    rva, vc = fixture_addr("f_b3_1"), fixture_addr("vc_b3_1")
    res: dict[str, Any] = {}
    for m in modes:
        a = art.get(m) or {}
        if not a.get("ok"):
            res[m] = {"ok": False, "why": "no slice-1 artifact"}
            continue
        b = W.base_info(out, m)
        s = open_snapshot(b["snapshot"])
        ri = json.loads(b["ri"].read_text())
        lay = find_envlayout(ri["qemu_sha256"])
        rows = _summary_rows(out, m)
        hs = _hs(lay, ri, rows, out / "hostsym_selftest.json")
        C = _unit_at(s, vc)
        code = bytes(C.code)
        sites = find_sites(code, lay.offset("regs[0]"))
        r: dict[str, Any] = {"sites": {k: [hex(v[0]), v[1], v[2]] for k, v in sites.items()}}
        # the manager's patches, as the capstone diff of the slice-1 cache.sc shows them
        mv = a["sc"]["variants"][0]
        mp = {int(p["off"], 16): bytes.fromhex(p["new"]) for p in mv["patches"]}
        same_sites = sorted(mp) == sorted(o for o, _, _ in sites.values())
        if not same_sites:
            r.update(ok=False, why=f"manager patches {sorted(mp)} are not the §3.5 sites")
            res[m] = r
            continue
        tmpl = {"L": "dho_nop", "T": "dho_nop", "J": "bf_jcc_to_nop"}
        good = tuple(Patch(C.uid, o, code[o:o + n], mp[o], tmpl[k])
                     for k, (o, n, _) in sorted(sites.items(), key=lambda x: x[1][0]))
        r["manager_patches"] = [{"off": hex(p.host_off), "old": p.old_bytes.hex(),
                                 "new": p.new_bytes.hex()} for p in good]
        fr = a.get("multi_tb_facts") or []
        facts = tuple(_fact_from_row(x) for x in fr)
        # positive control: hand-built plan with the manager's patches and FactRef
        plan, R, C2, n = build_plan(s, r_vaddr=rva, vc=vc, patches=good, facts=facts)
        cdir = out / "o4" / "control-store" / m
        _rmtree(cdir)
        pr = package_b3(s, plan, cdir, candidate_id="sha256:" + __import__("hashlib").sha256(
            f"wp27-o4-control-{m}".encode()).hexdigest(), runtime=ri, v6_exes=[exe_sha],
            summaries=rows, hostsym=hs)
        ctl: dict[str, Any] = {"status": pr.status, "reason": pr.reason,
                               "validation": None if pr.validation is None
                               else _rows_summary(pr.validation)}
        if pr.artifact_dir:
            mine = read_sc6(Path(pr.artifact_dir) / "cache.sc")
            theirs = read_sc6(Path(a["artifact_dir"]) / "cache.sc")
            ctl["variant_body_equal_manager"] = [bytes(x) for x in mine.variant_bodies] == \
                [bytes(x) for x in theirs.variant_bodies]
            ctl["records_and_bodies_equal_manager"] = (
                [bytes(x or b"") for x in mine.sc.bodies] ==
                [bytes(x or b"") for x in theirs.sc.bodies])
            ctl["ext"] = ext_summary(Path(pr.artifact_dir) / "cache.sc")
        r["control"] = ctl
        # the mutants: C' folded for r0 != 0 (the jne becomes jmp: the cbz is never taken)
        jo, jn, _ = sites["J"]
        bad = tuple(Patch(C.uid, p.host_off, p.old_bytes,
                          jcc_to_jmp(code, jo, jn) if p.host_off == jo else p.new_bytes,
                          "bf_jcc_to_jmp" if p.host_off == jo else p.template_id)
                    for p in good)
        r["mutant_patches"] = [{"off": hex(p.host_off), "old": p.old_bytes.hex(),
                                "new": p.new_bytes.hex(), "template": p.template_id}
                               for p in bad]
        # 1. falsified_fact_link: the variant cites the false fact r0 == 1 and is folded
        #    for it. Static rejection: V14 (certifier 0, proposer 1) and V19 stale_fact
        #    (no CalleeSummary row holds that fact); both bypassed for the control (V19
        #    beyond WP26's default V12-V17: recorded, and only its callee_summaries
        #    row may fail).
        # 2. fold_flip: the same fold, citing the true certified FactRef r0 == 0
        #    (WP19 mutation suite "fold_flip"). Static rejection: V12 (C' not equivalent
        #    to C under the facts); WP26's default V12-V17 bypass.
        muts = {
            "falsified-fact-r0eq1": {
                "mutant": "falsified_fact_link",
                "facts": (synthetic_fact("r0", 1, f"wp27-o4-{m}"),),
                "bypass": BYPASS_ROWS + ("V19",), "want_static": {"V14", "V19"},
                "allowed_v19_checks": {"callee_summaries"}},
            "fold-flip-true-fact": {
                "mutant": "fold_flip", "facts": facts, "bypass": BYPASS_ROWS,
                "want_static": {"V12"}, "allowed_v19_checks": set()},
        }
        r["mutants"] = {}
        for lab_m, mu in muts.items():
            mr: dict[str, Any] = {"mutant": mu["mutant"], "bypass": list(mu["bypass"]),
                                  "facts": [f.to_json() for f in mu["facts"]]}
            mplan, *_ = build_plan(s, r_vaddr=rva, vc=vc, patches=bad, facts=mu["facts"])
            ndir = out / "o4" / "neg" / m
            _rmtree(ndir / lab_m)
            nr = package_negative(s, mplan, ndir, label=lab_m, mutant=mu["mutant"],
                                  detector="F-B3-1 O1 (z/nz counts)", layout=lay, runtime=ri,
                                  v6_exes=[exe_sha], hostsym=hs, summaries=rows,
                                  bypass=mu["bypass"])
            mr.update(status=nr.status, reason=nr.reason, path=nr.path,
                      bypassed=[{"code": x["code"], "check": x.get("check"),
                                 "reason": x.get("reason"),
                                 "detail": str(x.get("detail"))[:300]} for x in nr.bypassed],
                      blocking=[{"code": x["code"], "check": x.get("check")}
                                for x in nr.blocking])
            codes = {x["code"] for x in nr.bypassed}
            v19_ok = all(x.get("check") in mu["allowed_v19_checks"] and
                         x.get("reason") == "stale_fact"
                         for x in nr.bypassed if x["code"] == "V19")
            items: dict[str, Any] = {
                "packaged": nr.packaged,
                "statically_rejected": nr.packaged and mu["want_static"] <= codes,
                "only_expected_v19_rows": v19_ok}
            if nr.packaged:
                nd = Path(nr.path)
                mr["ext"] = ext_summary(nd / "cache.sc")
                lab = probe_labels(nd / "cache.sc")
                o3 = {"stats": {"links_armed": ">0"}, "hits": {lab["C'"]: ">0"}}
                dyn = run_dynamic(out, f"o4-{lab_m}-{m}", nd, b, exe, timeout,
                                  "O1,O2a,O2b,O3", o3, fixture_input="1", negative=True)
                dyn["probe_labels"] = lab
                dyn["stats_view"] = _stats_view((dyn.get("v6_stats") or {}).get("artifact"))
                mr["dynamic"] = dyn
                o1d = ((dyn.get("data") or {}).get("O1") or {}).get("differences")
                mr["o1_differences"] = o1d
                res_art = ((dyn.get("run_json") or {}).get("artifact") or {}).get("results")
                res_ref = ((dyn.get("run_json") or {}).get("reference") or {}).get("results")
                mr["results"] = {"mutant": res_art, "reference": res_ref}
                items.update({
                    "o4_detected": dyn["oracles"].get("O4") == "pass" and
                    dyn["verdict"] == "pass" and dyn["exit"] == 0,
                    "o1_changed": dyn["oracles"].get("O1") == "fail" and bool(o1d),
                    "mutant_ran": dyn["oracles"].get("O3") == "pass",
                    "mutant_output_nz_path": isinstance(res_art, dict) and
                    res_art.get(1, 0) > 0,
                    "reference_z_n": res_ref == {0: N_ITER, 1: 0}})
            mr["items"] = items
            mr["ok"] = all(items.values())
            r["mutants"][lab_m] = mr
            log("o4", m, lab_m, nr.status, nr.reason, sorted(codes), "dyn",
                (mr.get("dynamic") or {}).get("oracles"), "results", mr.get("results"),
                "ok", mr["ok"])
        r["items"] = {
            "control_published_certified": ctl["status"] == "published",
            "control_equals_manager_variant": bool(ctl.get("variant_body_equal_manager")),
            "manager_patches_at_sites": same_sites,
            **{f"{k}_ok": v["ok"] for k, v in r["mutants"].items()}}
        r["ok"] = all(r["items"].values())
        log("o4", m, "control", ctl.get("status"), ctl.get("variant_body_equal_manager"),
            "ok", r["ok"])
        res[m] = r
    return res


# ---------------------------------------------------------------------------
# step 7: X-REHY with the variant (coverage config F-B3-23)
# ---------------------------------------------------------------------------

def step_xrehy(out: Path, modes: list[str], exe: Path, exe_sha: str,
               timeout: float) -> dict[str, Any]:
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot
    from scopt.passes.base import Patch
    from scopt.validate import package_b3
    from scopt.validate_sym.link_mutants import synthetic_fact

    rva, vc = fixture_addr("f_b3_1"), fixture_addr("vc_b3_1")
    res: dict[str, Any] = {}
    for m in modes:
        b = W.base_info(out, m, "23")
        s = open_snapshot(b["snapshot"])
        ri = json.loads(b["ri"].read_text())
        lay = find_envlayout(ri["qemu_sha256"])
        r: dict[str, Any] = {}
        # (a) the real pipeline on the coverage base
        real = _optimize(out, b, f"xrehy-real-{m}", out / "xrehy" / "store-real" / m)
        if real.get("artifact_dir"):
            real["ext"] = ext_summary(Path(real["artifact_dir"]) / "cache.sc")
        r["real_pipeline"] = real
        # CalleeSummary on the coverage base (does the proposer have r0 == 0 at f's bx lr?)
        sd = out / "xrehy" / "summary" / m
        _rmtree(sd)
        rc, _ = cli(["return-edges", "summaries", str(b["snapshot"]), "--profile",
                     str(b["profile"]), "--prep", str(b["prep"]), "--elf", str(b["elf"]),
                     "--out-dir", str(sd), "--certify"], out / "logs" / f"xrehy-summary-{m}.log")
        rows_p = sd / "summaries/callee_summaries.jsonl"
        rows = [json.loads(x) for x in rows_p.read_text().splitlines() if x.strip()] \
            if rows_p.is_file() else []
        rret = fixture_addr("f_b3_1_ret")
        rr = [x for x in rows if x.get("return_insn_vaddr") == hex(rret)]
        r["summary"] = {"exit": rc, "rows": len(rows), "f_b3_1_rows": [
            {k: x.get(k) for k in ("return_unit", "facts", "refused", "reasons", "status")}
            for x in rr]}
        real_variants = (real.get("ext") or {}).get("variants") or 0
        # (b) the variant: the same three §3.5 patches in the coverage C
        C = _unit_at(s, vc)
        code = bytes(C.code)
        sites = find_sites(code, lay.offset("regs[0]"))
        r["sites"] = {k: [hex(v[0]), v[1], v[2]] for k, v in sites.items()}
        if not sites:
            r["ok"] = False
            r["why"] = "the §3.5 sites were not found in the coverage C"
            res[m] = r
            continue
        tm = {"L": "dho_nop", "T": "dho_nop", "J": "bf_jcc_to_nop"}
        jmn = sites["J"][2].split()[0]
        if jmn not in ("jne", "jnz"):
            r["ok"] = False
            r["why"] = f"unexpected jcc {jmn} (r0 == 0 fold expects jne)"
            res[m] = r
            continue
        patches = tuple(Patch(C.uid, o, code[o:o + n], nopfill(n), tm[k])
                        for k, (o, n, _) in sorted(sites.items(), key=lambda x: x[1][0]))
        r["patches"] = [{"off": hex(p.host_off), "old": p.old_bytes.hex(),
                         "new": p.new_bytes.hex()} for p in patches]
        fr = [f for x in rr for f in x.get("facts", []) if f.get("field") == "r0" and
              f.get("status") == "certified" and int(str(f.get("value")), 0) == 0]
        facts = (_fact_from_row({**fr[0], "summary_row_hash": rr[0].get("row_hash")}),) \
            if fr else (synthetic_fact("r0", 0, f"wp27-xrehy-{m}"),)
        r["fact_source"] = "callee_summary_certified" if fr else "synthetic (no certified row)"
        plan, *_ = build_plan(s, r_vaddr=rva, vc=vc, patches=patches, facts=facts)
        hs = _hs(lay, ri, rows, out / "hostsym_selftest.json")
        cid = "sha256:" + __import__("hashlib").sha256(f"wp27-xrehy-{m}".encode()).hexdigest()
        st = out / "xrehy" / "store" / m
        _rmtree(st)
        pr = package_b3(s, plan, st, candidate_id=cid, runtime=ri, v6_exes=[exe_sha],
                        summaries=rows, hostsym=hs)
        r["package_certified"] = {"status": pr.status, "reason": pr.reason,
                                  "validation": None if pr.validation is None
                                  else _rows_summary(pr.validation)}
        adir = pr.artifact_dir
        r["certified"] = pr.status == "published"
        experimental = False
        if pr.status != "published":
            # allow_uncertified waives only the hostsym certification rows; record it
            st2 = out / "xrehy" / "store-uncertified" / m
            _rmtree(st2)
            pr2 = package_b3(s, plan, st2, candidate_id=cid, runtime=ri, v6_exes=[exe_sha],
                             summaries=rows, hostsym=hs, allow_uncertified=True)
            r["package_uncertified"] = {"status": pr2.status, "reason": pr2.reason,
                                        "validation": None if pr2.validation is None
                                        else _rows_summary(pr2.validation)}
            adir = pr2.artifact_dir if pr2.status == "published" else None
        if not adir:
            # The B3 toolchain fails closed under coverage (R's HostOps pinned by the
            # inline LOG_REG store, hostsym inconclusive on its spin loop): no validated
            # artifact can carry this variant. For the X-REHY *runtime* question the
            # variant is packaged as an experiment through WP26's test-only packager
            # (never publishable, tagged NEGATIVE_CONTROL.json, deploy needs
            # allow_negative_control), bypassing exactly the rows that fail here.
            from scopt.run.negative import package_negative

            fails = {x["code"] for x in (r["package_certified"]["validation"] or {}).get(
                "fails", [])}
            byp = tuple(sorted(fails & {"V12", "V13", "V14", "V19"}))
            r["experiment_bypass"] = list(byp)
            r["experiment_unbypassable"] = sorted(fails - set(byp))
            nd = out / "xrehy" / "experiment" / m
            _rmtree(nd / "xrehy-coverage-variant")
            nr = package_negative(s, plan, nd, label="xrehy-coverage-variant",
                                  mutant=None, detector="X-REHY experiment (not a mutant: the "
                                  "variant is sound for F-B3-23; validation cannot accept a "
                                  "guard under coverage)", layout=lay, runtime=ri,
                                  v6_exes=[exe_sha], hostsym=hs, summaries=rows, bypass=byp)
            r["package_experiment"] = {"status": nr.status, "reason": nr.reason,
                                       "path": nr.path,
                                       "bypassed": [{"code": x["code"], "check": x.get("check"),
                                                     "reason": x.get("reason")}
                                                    for x in nr.bypassed]}
            adir = nr.path if nr.packaged else None
            experimental = bool(adir)
        r["artifact_dir"] = adir
        r["experimental_artifact"] = experimental
        r["real_pipeline_variants"] = real_variants
        if not adir:
            r["ok"] = False
            r["why"] = "the coverage variant artifact could not be packaged"
            res[m] = r
            continue
        r["ext"] = ext_summary(Path(adir) / "cache.sc")
        lab = probe_labels(Path(adir) / "cache.sc")
        o3 = {"stats": {"links_armed": "==1"}, "hits": {lab["C'"]: ">0"}}
        dyn = run_dynamic(out, f"xrehy-{m}", Path(adir), b, exe, timeout,
                          "O1,O2a,O2b,O3,O5,O6", o3, fixture_input="23",
                          negative=experimental)
        dyn["probe_labels"] = lab
        dyn["stats_view"] = _stats_view((dyn.get("v6_stats") or {}).get("artifact"))
        r["dynamic"] = dyn
        o6 = (dyn.get("data") or {}).get("O6") or {}
        ring = {side: {k: (o6.get(side) or {}).get("ring", {}).get(k)
                       for k in ("index", "sha256", "entries", "nonzero", "len")}
                for side in ("reference", "artifact")}
        r["ring"] = ring
        c_hits = dyn["hits"].get(lab["C'"])
        r["items"] = {
            "o1_equal_warm": dyn["oracles"].get("O1") == "pass",
            "o2a_equal_warm": dyn["oracles"].get("O2a") == "pass",
            "o2b_equal_warm": dyn["oracles"].get("O2b") == "pass",
            "o3_armed_and_c_prime_hits": dyn["oracles"].get("O3") == "pass",
            "o6_ring_equal_and_c_prime_hits": dyn["oracles"].get("O6") == "pass",
            "c_prime_hits_about_n_minus_warmup": isinstance(c_hits, int) and
            N_ITER - 5 <= c_hits <= N_ITER - 1,
        }
        if experimental:
            # O4 mode: the driver's verdict is O4's ("fail" = no difference found, which is
            # the expected X-REHY outcome); every oracle above is read individually
            r["items"]["o4_not_detected_as_expected"] = dyn["oracles"].get("O4") == "fail"
        else:
            r["items"]["verdict_pass"] = dyn["verdict"] == "pass" and dyn["exit"] == 0
        r["ok"] = all(r["items"].values())
        log("xrehy", m, "real", real.get("status"), real.get("reason"), real_variants,
            "certified", r["certified"], "dyn", dyn["oracles"], "hits", dyn["hits"],
            "ok", r["ok"])
        res[m] = r
    return res


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--steps", default="bases,summary,artifact,dynamic,o4,xrehy")
    ap.add_argument("--modes", default="icount,noicount")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    W.env()
    exe, exe_sha = W.gate()
    gj = json.loads(W.GATE_JSON.read_text())
    modes = a.modes.split(",")
    steps = a.steps.split(",")
    jf = out / "wp27-slice1.json"
    doc: dict[str, Any] = json.loads(jf.read_text()) if jf.is_file() else {}
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain", "--", "scopt",
                            "experiments", "tests"], capture_output=True, text=True).stdout.strip()
    v6 = gj["worktree"]
    tag_commit = subprocess.run(["git", "-C", v6, "rev-parse", gj["tag"] + "^{commit}"],
                                capture_output=True, text=True).stdout.strip()
    v6_head = subprocess.run(["git", "-C", v6, "rev-parse", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
    v6_dirty = subprocess.run(["git", "-C", v6, "status", "--porcelain", "--untracked-files=no"],
                              capture_output=True, text=True).stdout.strip()
    doc["env"] = {"scopt_head": head, "scopt_dirty": bool(dirty), "gate_exe": str(exe),
                  "gate_sha256": exe_sha, "gate_tag": gj["tag"], "gate_tag_commit": tag_commit,
                  "qint_v6_head": v6_head, "qint_v6_dirty": bool(v6_dirty),
                  "gate_frozen_commit": gj["commit"],
                  "gate_unchanged": tag_commit == gj["commit"] == v6_head and not v6_dirty,
                  "fixture_elf_sha256": sha256(FIX / "b3.elf"),
                  "loadavg_start": Path("/proc/loadavg").read_text().strip(), "cpus": CPUS,
                  "timed": False, "timing_lock_taken": False}
    if not doc["env"]["gate_unchanged"]:
        raise SystemExit(f"gate binary state changed: {doc['env']}")
    if "bases" in steps:
        doc["bases"] = step_bases(out, modes, exe, exe_sha)
    if "summary" in steps:
        doc["summary"] = S0.step_summary(out, modes)
    if "artifact" in steps:
        doc["artifact"] = step_artifact(out, modes, doc.get("summary", {}))
    if "dynamic" in steps:
        doc["dynamic"] = step_dynamic(out, modes, doc.get("artifact", {}), exe, exe_sha,
                                      a.timeout)
    if "o4" in steps:
        doc["o4"] = step_o4(out, modes, doc.get("artifact", {}), exe, exe_sha, a.timeout)
    if "xrehy" in steps:
        doc["xrehy"] = step_xrehy(out, modes, exe, exe_sha, a.timeout)
    doc["env"]["loadavg_end"] = Path("/proc/loadavg").read_text().strip()
    jf.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    keys = ("bases", "summary", "artifact", "dynamic", "o4", "xrehy")
    summary = {k: {m: (doc[k].get(m) or {}).get("ok") for m in modes} for k in keys if k in doc}
    log("summary", json.dumps(summary))
    (out / "verdict.json").write_text(json.dumps(verdict(doc, modes), indent=1, sort_keys=True,
                                                 default=str) + "\n")
    return 0 if all(x for v in summary.values() for x in v.values()) else 1


def verdict(doc: dict[str, Any], modes: list[str]) -> dict[str, Any]:
    """The WP27 integration-stage acceptance rows (plan ``## WP27`` steps 6-7, T-B3-8)."""
    def pf(x: Any) -> str:
        return "pass" if x else "fail"

    def per(k: str) -> dict[str, Any]:
        return {m: (doc.get(k) or {}).get(m) or {} for m in modes}
    b, ar, dy, o4, xr = (per(k) for k in ("bases", "artifact", "dynamic", "o4", "xrehy"))
    rows = []
    rows.append({"criterion": "bases: F-B3-1 and F-B3-23 prepared/imported/profiled on the gate "
                 "binary (icount, noicount), RuntimeIdentity v2 = gate exe",
                 "result": pf(all(b[m].get("ok") for m in modes)),
                 "evidence": {c: {m: {k: ((doc.get("bases") or {}).get(c) or {}).get(m, {}).get(k)
                                      for k in ("snapshot", "prep_verdict", "ri_qemu_sha256",
                                                "ri_envlayout_id", "qemu_is_gate")}
                                  for m in modes} for c in ("1", "23")}})
    rows.append({"criterion": "step 6: v6 writer emits one variant, one SPEC slot, one link "
                 "(b3-spec arm through scopt optimize); static rows pass; §3.5 bytes; published",
                 "result": pf(all(ar[m].get("ok") for m in modes)),
                 "evidence": {m: {k: ar[m].get(k) for k in
                                  ("artifact_id", "cache_sc_sha256", "ext", "pass_byte_delta",
                                   "bytes_ok", "static_rows_ok", "ledger_ok",
                                   "factref_summary_row_ok", "spec_r_c", "publish")}
                              for m in modes}})
    rows.append({"criterion": "step 6: deploy a copy on the gate binary (published copy deployed; "
                 "post-run proof PASS, appended 0, gate exe sha)",
                 "result": pf(all((dy[m].get("items") or {}).get("proofs_pass") and
                                  (dy[m].get("items") or {}).get("gate_binary") for m in modes)),
                 "evidence": {m: dy[m].get("run_json") for m in modes}})
    rows.append({"criterion": "step 7: O1/O2a/O2b equal warm",
                 "result": pf(all(all((dy[m].get("items") or {}).get(k) for k in
                                      ("o1_equal_warm", "o2a_equal_warm", "o2b_equal_warm",
                                       "z_n_nz_0_both")) for m in modes)),
                 "evidence": {m: {"oracles": dy[m].get("oracles"),
                                  "details": dy[m].get("details"),
                                  "file": dy[m].get("file")} for m in modes}})
    rows.append({"criterion": "step 7: O3 armed = 1, C' hits ~ N - warm-up (N = 1000; C' in "
                 "[N-5, N-1], F in [1, 5], C' + F = stub S)",
                 "result": pf(all(all((dy[m].get("items") or {}).get(k) for k in
                                      ("o3_pass", "armed_eq_1", "c_prime_hits_about_n_minus_warmup",
                                       "f_taken_only_in_warmup", "c_prime_plus_f_eq_stub",
                                       "no_invariant_violation")) for m in modes)),
                 "evidence": {m: {"hits": dy[m].get("hits"), "stats": dy[m].get("stats_view")}
                              for m in modes}})
    rows.append({"criterion": "step 7: O4 falsified-fact mutant changes O1 (statically rejected "
                 "by V14/V19; plus the fold_flip mutant under the true fact, rejected by V12; the "
                 "hand-built positive control equals the manager's artifact)",
                 "result": pf(all(o4[m].get("ok") for m in modes)),
                 "evidence": {m: {"items": o4[m].get("items"),
                                  "mutant_patches": o4[m].get("mutant_patches"),
                                  "control": {k: (o4[m].get("control") or {}).get(k) for k in
                                              ("status", "variant_body_equal_manager",
                                               "records_and_bodies_equal_manager")},
                                  "mutants": {k: {x: v.get(x) for x in
                                                  ("mutant", "bypass", "bypassed", "results",
                                                   "o1_differences", "items")} |
                                              {"dyn": {x: (v.get("dynamic") or {}).get(x)
                                                       for x in ("verdict", "oracles", "hits",
                                                                 "file")}}
                                              for k, v in (o4[m].get("mutants") or {}).items()}}
                              for m in modes}})
    rows.append({"criterion": "step 7: X-REHY with the variant (coverage config F-B3-23): O1/O2a/"
                 "O2b equal, O6 LOG_REG ring equal warm, C' hits ~ N",
                 "result": pf(all(xr[m].get("ok") for m in modes)),
                 "evidence": {m: {"items": xr[m].get("items"), "certified": xr[m].get("certified"),
                                  "real_pipeline": {k: (xr[m].get("real_pipeline") or {}).get(k)
                                                    for k in ("status", "reason", "passes")},
                                  "package_certified": xr[m].get("package_certified"),
                                  "package_uncertified": xr[m].get("package_uncertified"),
                                  "ring": xr[m].get("ring"),
                                  "dyn": {k: (xr[m].get("dynamic") or {}).get(k) for k in
                                          ("verdict", "oracles", "hits", "file")}}
                              for m in modes}})
    tb38 = all(r["result"] == "pass" for r in rows[3:6])
    rows.append({"criterion": "T-B3-8: F-B3-1 O1/O2a/O2b/O3/O4 on the gate binary",
                 "result": pf(tb38), "evidence": "rows 'step 7: O1/O2a/O2b', 'O3', 'O4' above"})
    return {"schema": "wp27.verdict/1", "stage": "integration (slice 1, gate binary)",
            "env": doc.get("env"),
            "verdict": "pass" if all(r["result"] == "pass" for r in rows) else "fail",
            "rows": rows, "timed": False, "timing_lock_taken": False}


if __name__ == "__main__":
    sys.exit(main())
