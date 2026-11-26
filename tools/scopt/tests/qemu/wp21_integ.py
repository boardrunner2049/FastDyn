#!/usr/bin/env python3
"""WP21 integration stage (plan ``## WP21``, Acceptance (integration); design §3.4-§3.9).

    wp21_integ.py --out OUT [--steps bases,static,identity,publish,dynamic,indirect]
                  [--modes icount,noicount]

Run from the scopt worktree with the tools-env python. Nothing is timed and no timing
lock is taken. Every QEMU run goes through the scopt/FastDyn launcher (``setarch x86_64
-R``, timeout, fresh run directory, ``supercache_flat = false``).

Steps:

* ``bases``: WP24 phase F-B3-1 (``fixtures/b3/b3-1.toml.in``) prepared, imported and
  profiled (steady phase ``steady@b3_bench_start``, untimed) **on the frozen gate binary**
  (``runs/wp22/gate-binary.json``) in icount (shift 0) and noicount, with WP20's driver
  (``tests/qemu/wp20_fixture_snapshots.py``); then ``scopt admission identity`` writes the
  real RuntimeIdentity v2 of each base (gate exe sha, its EnvLayout id).
* ``static`` (T-B3-6, static part): ``scopt optimize --pipeline b3 --b3`` (the CLI) on each
  base with that RuntimeIdentity, for the full pipeline and CP, BF, DHO disabled in turn
  and all three disabled. Each artifact's ``validation.json`` is read, and the variant
  bytes are checked **independently of the manager** from the output ``cache.sc``: the
  variant body ``[tc, end)`` of the ext table is diffed against the general C record of
  the same file; each differing C instruction is decoded with capstone and classified as
  a relocation (same instruction, same absolute target, or the same target relative to
  its own TB struct) or a patch. The patches must be exactly §3.5's expected sites (the
  r0 env load ``mov r32,[rbp+regs[0]]``, the ``test`` of that register, the ``jcc``) in
  the form each ablation predicts.
* ``identity`` (T-B3-7 on the gate base): the identity pipeline and b3 with all six passes
  disabled give a ``cache.sc`` sha256-equal to the base's.
* ``publish``: ``scopt validate ART`` (static re-validation) and ``scopt publish ART``
  (verified copy incl. WP18's hostsym coverage check) for every published B3 artifact.
* ``dynamic`` (supplementary, reported; WP27/WP28 own the dynamic gate):
  ``scopt validate --dynamic --oracles O1,O2a,O2b,O3,O5`` on the gate binary for every
  published B3 artifact; O3 requires ``links_armed > 0`` and C' hits > 0 for an artifact
  with a SPEC link.
* ``indirect`` (supplementary; the LLVM survey's "promote-indirect-exit" stays deferred
  as ``indirect_multiway``): WP24 phase F-B3-6 on the gate binary; the full b3 artifact
  puts no slot on the unit ending in ``bx r3`` and passes O1/O2a/O2b.

Writes ``OUT/wp21-integ.json`` and ``OUT/verdict.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
sys.path.insert(0, str(WT))
sys.path.insert(0, str(HERE))
ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FIX = ROOT / "fixtures/b3"
GATE_JSON = ROOT / "runs/wp22/gate-binary.json"
ENVLAYOUT_DIR = ROOT / "runs/wp18/envlayout"
PY = sys.executable
CPUS = os.environ.get("WP21_CPUS", "16-23")  # keep off the cores of a running timing job

PASSES = ("specialize-returns", "constant-propagation", "branch-folding", "dead-host-ops",
          "chain-returns", "chain-static")
CASES: dict[str, tuple[str, ...]] = {
    "full": (),
    "no_cp": ("constant-propagation",),
    "no_bf": ("branch-folding",),
    "no_dho": ("dead-host-ops",),
    "no_cp_bf_dho": ("constant-propagation", "branch-folding", "dead-host-ops"),
}
# §3.5 expected form of the three sites (L = r0 env load, T = test, J = jcc) per case
EXPECT = {
    "full": {"L": "nop", "T": "nop", "J": "folded"},
    "no_cp": {"L": "same", "T": "nop", "J": "folded"},
    "no_bf": {"L": "xor", "T": "same", "J": "same"},
    "no_dho": {"L": "xor", "T": "same", "J": "folded"},
}
# V10 is the dynamic differential (no static row; see the dynamic step)
STATIC_ROWS = [f"V{i}" for i in range(1, 22) if i != 10] + ["V23"]


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def env() -> dict[str, str]:
    e = dict(os.environ)
    e["SCOPT_ENVLAYOUT_PATH"] = str(ENVLAYOUT_DIR)
    os.environ["SCOPT_ENVLAYOUT_PATH"] = str(ENVLAYOUT_DIR)
    return e


def cli(args: list[str], logf: Path, *, timeout: float = 1800) -> tuple[int, str]:
    """Run ``python -m scopt ARGS`` (pinned to CPUS); stdout returned, both logged."""
    logf.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    p = subprocess.run(["taskset", "-c", CPUS, PY, "-m", "scopt", *args], cwd=str(WT),
                       env=env(), capture_output=True, text=True, timeout=timeout)
    logf.write_text("$ python -m scopt " + " ".join(args) + "\n--- stdout\n" + p.stdout +
                    "\n--- stderr\n" + p.stderr[-20000:] +
                    f"\nexit={p.returncode} seconds={time.time() - t0:.1f}\n")
    return p.returncode, p.stdout


def gate() -> tuple[Path, str]:
    gj = json.loads(GATE_JSON.read_text())
    exe = Path(gj["exe_path"])
    s = sha256(exe)
    if s != gj["exe_sha256"]:
        raise SystemExit(f"gate exe sha {s} != frozen {gj['exe_sha256']}")
    return exe, s


# ---------------------------------------------------------------------------
# bases
# ---------------------------------------------------------------------------

def base_dir(out: Path, mode: str, cfg: str = "1") -> Path:
    return out / "base" / mode / cfg


def base_info(out: Path, mode: str, cfg: str = "1") -> dict[str, Any]:
    d = base_dir(out, mode, cfg)
    sj = json.loads((d / "snap.json").read_text())
    return {"dir": d, "snapshot": Path(sj["snapshot"]), "prep": Path(sj["prep"]),
            "profile": Path(sj["profile"]), "toml": Path(sj["base_toml"]),
            "ri": d / "runtime_identity.v2.json", "input": Path(sj["inputs"][0]),
            "elf": FIX / sj["elf"], "qemu": sj["qemu"]}


def step_bases(out: Path, modes: list[str], exe: Path, exe_sha: str,
               cfg: str = "1") -> dict[str, Any]:
    res: dict[str, Any] = {}
    todo = [m for m in modes if not (base_dir(out, m, cfg) / "snap.json").is_file()]
    if todo:
        p = subprocess.run(["taskset", "-c", CPUS, PY, str(HERE / "wp20_fixture_snapshots.py"),
                            "--out", str(out / "base"), "--qemu", str(exe), "--configs", cfg,
                            "--modes", ",".join(todo), "--profile", "-j", "2"],
                           cwd=str(WT), env=env(), capture_output=True, text=True)
        (out / f"bases-{cfg}.log").write_text(p.stdout + p.stderr)
        if p.returncode:
            raise SystemExit(f"base capture failed: {p.stdout[-2000:]} {p.stderr[-2000:]}")
    for m in modes:
        b = base_info(out, m, cfg)
        if not b["ri"].is_file():
            rc, _ = cli(["admission", "identity", "--snapshot", str(b["snapshot"]), "--config",
                         str(b["toml"]), "--out", str(b["ri"])], b["dir"] / "logs/06-admission.log")
            if rc:
                raise SystemExit(f"{m}: admission identity failed")
        ri = json.loads(b["ri"].read_text())
        sj = json.loads((b["dir"] / "snap.json").read_text())
        res[m] = {"snapshot": str(b["snapshot"]), "profile": str(b["profile"]),
                  "prep_verdict": sj.get("prep_verdict"), "records": sj.get("records"),
                  "qemu": b["qemu"], "qemu_is_gate": Path(b["qemu"]) == exe,
                  "ri_qemu_sha256": ri.get("qemu_sha256"),
                  "ri_envlayout_id": (ri.get("admission") or {}).get("envlayout_id"),
                  "ri_schema": ri.get("schema"),
                  "elf_sha256": sha256(b["elf"]),
                  "base_cache_sc_sha256": sha256(b["snapshot"] / "cache.sc")}
        res[m]["ok"] = (res[m]["qemu_is_gate"] and res[m]["prep_verdict"] == "PASS" and
                        res[m]["ri_qemu_sha256"] == "sha256:" + exe_sha and
                        res[m]["ri_schema"] == "scopt.runtime_identity/2")
        log("base", cfg, m, res[m]["ok"], res[m]["snapshot"])
    return res


# ---------------------------------------------------------------------------
# independent byte check of the variant in the output cache.sc
# ---------------------------------------------------------------------------

def _cs():
    import capstone

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    return md


def _target(ins) -> int | None:
    """Absolute target of a rel branch/call or a RIP-relative operand, else None."""
    import capstone.x86 as X

    for op in ins.operands:
        if op.type == X.X86_OP_IMM and (ins.mnemonic.startswith("j") or
                                        ins.mnemonic == "call"):
            return op.imm
        if op.type == X.X86_OP_MEM and op.mem.base == X.X86_REG_RIP:
            return ins.address + ins.size + op.mem.disp
    return None


def variant_diff(sc_path: Path, r0_off: int, in_vaddrs: list[int | None]) -> dict[str, Any]:
    """Decode the output cache.sc and diff each variant against its general C.

    ``in_vaddrs``: the input snapshot's first guest vaddr per unit in slot order (the
    records' own ``pc`` is 0 for these PC-relative TBs; the artifact keeps the input's
    unit order, as ``scopt.validate.artifact_link`` assumes)."""
    from scopt.formats.sc_semantic import KIND_GENERAL, KIND_SPEC, read_semantic_cache

    f = read_semantic_cache(sc_path)
    out: dict[str, Any] = {"version": f.version, "semantic_table": f.has_ext, "loadable": f.loadable,
                           "problem": f.problem, "variants": [], "slots": []}
    if f.ext is None:
        return out
    recs = {r.tb_offset: (i, r) for i, r in enumerate(f.sc.records) if r is not None}
    va = {}
    for k, i in enumerate(f.sc.valid_slots):
        va[f.sc.records[i].tb_offset] = in_vaddrs[k] if k < len(in_vaddrs) else None

    def vx(tbo):
        return None if va.get(tbo) is None else hex(va[tbo])
    for s in f.ext.slots:
        _, r = recs.get(s.src_tb_offset, (None, None))
        out["slots"].append({"src_vaddr": vx(s.src_tb_offset), "slot_n": s.slot_n,
                             "kind": {KIND_GENERAL: "GENERAL", KIND_SPEC: "SPEC"}.get(s.kind,
                                                                                      s.kind),
                             "target_vaddr": hex(s.target_vaddr)})
    md = _cs()
    for vi, (v, body) in enumerate(zip(f.ext.variants, f.variant_bodies)):
        i, c = recs[v.general_tb_offset]
        cbody = f.sc.bodies[i]
        d: dict[str, Any] = {"index": vi, "c_vaddr": vx(c.tb_offset), "c_tb_offset": hex(c.tb_offset),
                             "v_tb_offset": hex(v.tb_offset), "size_equal": len(cbody) == len(body),
                             "relocations": 0, "patches": []}
        if len(cbody) != len(body):
            out["variants"].append(d)
            continue
        cins = list(md.disasm(cbody, c.tc_offset))
        covered = sum(x.size for x in cins)
        d["c_decoded_bytes"] = covered
        own = v.tb_offset - c.tb_offset
        for ins in cins:
            o = ins.address - c.tc_offset
            old, new = cbody[o:o + ins.size], body[o:o + ins.size]
            if old == new:
                continue
            vins = list(md.disasm(new, v.tc_offset + o))
            reloc = False
            if len(vins) == 1 and vins[0].size == ins.size and vins[0].mnemonic == ins.mnemonic:
                tc_, tv = _target(ins), _target(vins[0])
                if tc_ is not None and tv is not None and (tv == tc_ or tv - tc_ == own):
                    reloc = True
            if reloc:
                d["relocations"] += 1
                continue
            d["patches"].append({"off": hex(o), "old": old.hex(), "new": new.hex(),
                                 "old_asm": f"{ins.mnemonic} {ins.op_str}".strip(),
                                 "new_asm": "; ".join(f"{x.mnemonic} {x.op_str}".strip()
                                                      for x in vins)})
        # §3.5 sites in C: the r0 env load, the test of its register, the jcc after it
        sites: dict[str, Any] = {}
        for k, ins in enumerate(cins):
            if ins.mnemonic == "mov" and ins.op_str.endswith(
                    "dword ptr [rbp]" if r0_off == 0 else f"dword ptr [rbp + {r0_off:#x}]"):
                reg = ins.op_str.split(",")[0]
                nxt = cins[k + 1:k + 4]
                t = next((x for x in nxt if x.mnemonic == "test" and
                          x.op_str == f"{reg}, {reg}"), None)
                if t is None:
                    continue
                j = cins[cins.index(t) + 1]
                if not j.mnemonic.startswith("j") or j.mnemonic == "jmp":
                    continue
                sites = {"L": ins, "T": t, "J": j}
                break
        d["sites"] = {}
        for name, ins in sites.items():
            o = ins.address - c.tc_offset
            new = body[o:o + ins.size]
            vins = list(md.disasm(new, v.tc_offset + o))
            mn = [x.mnemonic for x in vins]
            if new == cbody[o:o + ins.size]:
                form = "same"
            elif all(m == "nop" for m in mn) and sum(x.size for x in vins) == ins.size:
                form = "nop"
            elif mn and mn[0] == "xor" and all(m == "nop" for m in mn[1:]):
                form = "xor"
            elif mn and mn[0] == "jmp" and all(m == "nop" for m in mn[1:]) and \
                    _target(vins[0]) is not None and _target(vins[0]) - v.tc_offset == \
                    _target(ins) - c.tc_offset:
                form = "jmp"
            else:
                form = "other"
            d["sites"][name] = {"off": hex(o), "old_asm": f"{ins.mnemonic} {ins.op_str}",
                                "old": cbody[o:o + ins.size].hex(), "new": new.hex(),
                                "new_asm": "; ".join(f"{x.mnemonic} {x.op_str}".strip()
                                                     for x in vins), "form": form}
        if sites:
            # cbz r0 under r0 == 0: `test r,r` sets ZF=1, so je/jz is taken (-> jmp)
            # and jne/jnz is not (-> nops)
            j = sites["J"].mnemonic
            d["sites"]["J"]["expected_fold"] = "jmp" if j in ("je", "jz") else \
                "nop" if j in ("jne", "jnz") else "?"
        site_offs = {v_["off"] for v_ in d["sites"].values()}
        d["patches_outside_sites"] = [p for p in d["patches"] if p["off"] not in site_offs]
        out["variants"].append(d)
    return out


def check_case(case: str, vd: dict[str, Any]) -> tuple[bool, list[str]]:
    why: list[str] = []
    if case == "no_cp_bf_dho":
        if vd["variants"]:
            why.append(f"{len(vd['variants'])} variants (want 0: downgraded)")
        return not why, why
    if len(vd["variants"]) != 1:
        return False, [f"{len(vd['variants'])} variants (want 1)"]
    v = vd["variants"][0]
    if not v["size_equal"]:
        return False, ["variant size != C size"]
    if set(v["sites"]) != {"L", "T", "J"}:
        return False, [f"sites not found in C: {sorted(v['sites'])}"]
    for k, want in EXPECT[case].items():
        got = v["sites"][k]["form"]
        if want == "folded":
            ok = got == v["sites"]["J"]["expected_fold"]
        else:
            ok = got == want
        if not ok:
            why.append(f"{k}: {got} (want {want})")
    if v["patches_outside_sites"]:
        why.append(f"patches outside the §3.5 sites: {v['patches_outside_sites']}")
    return not why, why


# ---------------------------------------------------------------------------
# static (T-B3-6)
# ---------------------------------------------------------------------------

def _validation(adir: Path) -> dict[str, Any]:
    v = json.loads((adir / "validation.json").read_text())
    codes: dict[str, str] = {}
    # a code has several rows: any fail -> fail; else any pass -> pass (informational
    # skip sub-rows, e.g. V20 tb_start_cb_possible "not needed", do not hide a pass);
    # else skip
    for r in v.get("rows", []):
        c, res = r.get("code"), r.get("result")
        rank = {"fail": 3, "not_run": 2, "pass": 1, "skip": 0}
        if c not in codes or rank.get(res, 3) > rank.get(codes[c], 3):
            codes[c] = res
    nonpass = [{"code": r.get("code"), "check": r.get("check"), "result": r.get("result"),
                "reason": r.get("reason"), "detail": str(r.get("detail"))[:240]}
               for r in v.get("rows", []) if r.get("result") != "pass"]
    return {"verdict": v.get("verdict"), "codes": codes, "non_pass": nonpass,
            "rows": len(v.get("rows", []))}


def step_static(out: Path, modes: list[str]) -> dict[str, Any]:
    from scopt.formats.envlayout_v1 import read_envlayout

    res: dict[str, Any] = {}
    for m in modes:
        b = base_info(out, m)
        ri = json.loads(b["ri"].read_text())
        lay = read_envlayout(ENVLAYOUT_DIR / (ri["qemu_sha256"].split(":")[1] + ".json"))
        r0_off = lay.offset("regs[0]")
        from scopt.importer.import_cache import open_snapshot

        in_vaddrs = [u.vaddrs[0] if u.vaddrs else None
                     for u in open_snapshot(b["snapshot"]).units()]
        rm: dict[str, Any] = {}
        for case, dis in CASES.items():
            store = out / "store" / m
            args = ["optimize", str(b["snapshot"]), "--pipeline", "b3", "--b3", "--store",
                    str(store), "--profile", str(b["profile"]), "--runtime-identity",
                    str(b["ri"]), "--prep", str(b["prep"]), "--elf", str(b["elf"]),
                    "--selftest", str(out / "hostsym_selftest.json"), "--json"]
            for p in dis:
                args += ["--disable", p]
            t0 = time.time()
            rc, so = cli(args, out / "logs" / f"optimize-{m}-{case}.log")
            try:
                doc = json.loads(so)
            except ValueError:
                rm[case] = {"exit": rc, "error": "no JSON on stdout", "ok": False}
                log(m, case, "NO JSON", rc)
                continue
            r: dict[str, Any] = {"exit": rc, "seconds": round(time.time() - t0, 1),
                                 "status": doc.get("status"), "reason": doc.get("reason"),
                                 "path": doc.get("path"), "artifact_dir": doc.get("artifact_dir"),
                                 "pass_byte_delta": doc.get("pass_byte_delta"),
                                 "downgraded": doc.get("downgraded"),
                                 "passes": [{"pass": p.get("pass"), "outcome": p.get("outcome"),
                                             "reason": p.get("reason")}
                                            for p in doc.get("passes", [])]}
            if doc.get("artifact_dir"):
                adir = Path(doc["artifact_dir"])
                r["artifact_id"] = doc.get("artifact_id")
                r["cache_sc_sha256"] = sha256(adir / "cache.sc")
                r["validation"] = _validation(adir)
                vd = variant_diff(adir / "cache.sc", r0_off, in_vaddrs)
                r["sc"] = vd
                r["bytes_ok"], r["bytes_why"] = check_case(case, vd)
                side = json.loads((adir / "cache.sc.scopt.json").read_text())
                man = side.get("x_b3_manager") or {}
                r["manager_downgraded"] = man.get("downgraded")
                r["manager_regions"] = len(man.get("regions") or [])
                codes = r["validation"]["codes"]
                want_skip = {"V9", "V22"} | ({"V14"} if case == "no_cp_bf_dho" else set())
                r["static_rows_ok"] = (r["validation"]["verdict"] == "accepted" and
                                       all(codes.get(c) == "pass" for c in STATIC_ROWS
                                           if c not in want_skip) and
                                       all(codes.get(c) in ("pass", "skip") for c in want_skip))
                r["spec_slots"] = sum(1 for s in vd["slots"] if s["kind"] == "SPEC")
                r["general_slots"] = sum(1 for s in vd["slots"] if s["kind"] == "GENERAL")
            rm[case] = r
            log(m, case, r["status"], r.get("reason"), r.get("pass_byte_delta"),
                "bytes_ok", r.get("bytes_ok"), r.get("bytes_why"), "rows_ok",
                r.get("static_rows_ok"), f"{r['seconds']}s")
        # ablation separation and downgrade (§3.5)
        bodies = {}
        for k in ("no_cp", "no_bf", "no_dho"):
            vs = (rm.get(k, {}).get("sc") or {}).get("variants") or []
            bodies[k] = json.dumps([v.get("patches") for v in vs], sort_keys=True)
        deltas = {k: rm.get(k, {}).get("pass_byte_delta") or {} for k in CASES}
        all_off = rm.get("no_cp_bf_dho", {})
        full_spec = rm.get("full", {}).get("sc", {}).get("slots", [])
        spec_target = [(s["src_vaddr"], s["slot_n"], s["target_vaddr"]) for s in full_spec
                       if s["kind"] == "SPEC"]
        off_general = [(s["src_vaddr"], s["slot_n"], s["target_vaddr"])
                       for s in (all_off.get("sc") or {}).get("slots", [])
                       if s["kind"] == "GENERAL"]
        rm["separation"] = {
            "distinct_variant_bytes": len(set(bodies.values())) == 3,
            "nonzero_deltas": {k: bool(deltas[k]) and all(x > 0 for x in deltas[k].values())
                               for k in ("full", "no_cp", "no_bf", "no_dho")},
            "deltas": deltas,
            "downgrade_all_off": [d.get("reason") for d in (all_off.get("downgraded") or [])],
            "full_spec_slots": spec_target,
            "all_off_spec_slot_now_general": bool(spec_target) and
            all(t in off_general for t in spec_target),
        }
        sep = rm["separation"]
        rm["ok"] = (all(rm.get(k, {}).get("status") == "published" for k in CASES) and
                    all(rm[k].get("bytes_ok") for k in CASES) and
                    all(rm[k].get("static_rows_ok") for k in CASES) and
                    sep["distinct_variant_bytes"] and all(sep["nonzero_deltas"].values()) and
                    sep["downgrade_all_off"] == ["no_simplification"] and
                    sep["all_off_spec_slot_now_general"])
        log(m, "T-B3-6 static", rm["ok"], json.dumps(sep, default=str)[:400])
        res[m] = rm
    return res


# ---------------------------------------------------------------------------
# identity (T-B3-7 on the gate base)
# ---------------------------------------------------------------------------

def step_identity(out: Path, modes: list[str]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for m in modes:
        b = base_info(out, m)
        insha = sha256(b["snapshot"] / "cache.sc")
        rm = {}
        for case, extra in {"identity_pipeline": ["--pipeline", "identity"],
                            "b3_all_disabled": ["--pipeline", "b3", "--b3",
                                                *sum((["--disable", p] for p in PASSES), [])],
                            "b3_only_region_pass": ["--pipeline", "b3", "--b3", "--only",
                                                    "constant-propagation"]}.items():
            args = ["optimize", str(b["snapshot"]), *extra, "--store",
                    str(out / "store-identity" / m), "--profile", str(b["profile"]), "--json"]
            if "--b3" in extra:
                args += ["--runtime-identity", str(b["ri"]), "--prep", str(b["prep"]), "--elf",
                         str(b["elf"]), "--selftest", str(out / "hostsym_selftest.json")]
            rc, so = cli(args, out / "logs" / f"identity-{m}-{case}.log")
            try:
                doc = json.loads(so)
            except ValueError:
                rm[case] = {"exit": rc, "ok": False, "error": "no JSON"}
                continue
            adir = doc.get("artifact_dir")
            o = sha256(Path(adir) / "cache.sc") if adir else None
            v9 = _validation(Path(adir))["codes"].get("V9") if adir else None
            rm[case] = {"exit": rc, "path": doc.get("path"), "status": doc.get("status"),
                        "in_sha256": insha, "out_sha256": o, "identical": o == insha, "V9": v9,
                        "ok": rc == 0 and o == insha and v9 == "pass"}
            log("identity", m, case, rm[case]["ok"])
        rm["ok"] = all(v["ok"] for v in rm.values())
        res[m] = rm
    return res


# ---------------------------------------------------------------------------
# publish / static revalidation
# ---------------------------------------------------------------------------

def step_publish(out: Path, modes: list[str], static: dict[str, Any]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for m in modes:
        b = base_info(out, m)
        rm = {}
        for case in CASES:
            adir = (static.get(m, {}).get(case) or {}).get("artifact_dir")
            if not adir:
                rm[case] = {"ok": False, "why": "no artifact"}
                continue
            rc1, so1 = cli(["validate", adir, "--snapshot", str(b["snapshot"]), "--json"],
                           out / "logs" / f"revalidate-{m}-{case}.log")
            rc2, so2 = cli(["publish", adir, "--store", str(out / "pubstore" / m), "--json"],
                           out / "logs" / f"publish-{m}-{case}.log")
            try:
                pub = json.loads(so2)
            except ValueError:
                pub = {}
            rv = (out / "logs" / f"revalidate-{m}-{case}.log").read_text(errors="replace")
            same = str(pub.get("artifact_id", "")).removeprefix("sha256:") == Path(adir).name
            # `scopt validate A` (WP10 static re-validation) reads cache.sc as v5 only:
            # recorded, not part of the verdict (known gap, see the WP21 int note)
            rm[case] = {"revalidate_exit": rc1,
                        "revalidate_v6_unsupported": rc1 != 0 and
                        "is not loadable: ('version',)" in rv,
                        "publish_exit": rc2, "published_id": pub.get("artifact_id"),
                        "published_status": pub.get("status"), "same_id": same,
                        "ok": rc2 == 0 and same}
            log("publish", m, case, rm[case])
        rm["ok"] = all(v["ok"] for v in rm.values())
        res[m] = rm
    return res


# ---------------------------------------------------------------------------
# dynamic (supplementary)
# ---------------------------------------------------------------------------

def step_dynamic(out: Path, modes: list[str], static: dict[str, Any], exe: Path,
                 timeout: float) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for m in modes:
        b = base_info(out, m)
        rm = {}
        for case in CASES:
            r = static.get(m, {}).get(case) or {}
            adir = r.get("artifact_dir")
            if not adir:
                rm[case] = {"ok": False, "why": "no artifact"}
                continue
            spec = bool(r.get("spec_slots"))
            od = out / "dyn" / m / case
            if od.exists():
                import shutil

                shutil.rmtree(od)
            args = ["validate", adir, "--dynamic", "-c", str(b["toml"]), "--baseline",
                    str(b["snapshot"]), "--inputs", str(b["input"]), "--fixture-input", "1",
                    "--fixture-expected", str(FIX / "expected.py"), "--timeout", str(timeout),
                    "--qemu", str(exe), "--out", str(od), "--json"]
            if spec:
                o3 = json.dumps({"stats": {"links_armed": ">0"}, "hits": {"variant 0 C'": ">0"}})
                args += ["--oracles", "O1,O2a,O2b,O3,O5", "--o3", o3, "--hit-target",
                         "variant 0 C'"]
            else:
                args += ["--oracles", "O1,O2a,O2b,O5"]
            rc, _ = cli(args, out / "logs" / f"dynamic-{m}-{case}.log")
            dj = od / "dynamic_oracles.json"
            doc = json.loads(dj.read_text()) if dj.is_file() else None
            rows = []
            if doc is not None:
                for c in doc.get("cases", []):
                    for o in c.get("oracles", []):
                        rows.append({"oracle": o.get("oracle"), "verdict": o.get("verdict"),
                                     "detail": str(o.get("detail"))[:300]})
            want = ("O1", "O2a", "O2b", "O3", "O5") if spec else ("O1", "O2a", "O2b", "O5")
            got = {x["oracle"]: x["verdict"] for x in rows}
            rm[case] = {"exit": rc, "verdict": None if doc is None else doc.get("verdict"),
                        "file": str(dj), "results": rows,
                        "ok": rc == 0 and doc is not None and doc.get("verdict") == "pass" and
                        all(got.get(o) in ("pass", "reported") for o in want)}
            log("dynamic", m, case, rm[case]["ok"], got)
        rm["ok"] = all(v["ok"] for v in rm.values())
        res[m] = rm
    return res


# ---------------------------------------------------------------------------
# indirect exits stay unguarded (tbb / bx rX deferral, §3.1 rule 2, F-B3-6)
# ---------------------------------------------------------------------------

def step_indirect(out: Path, modes: list[str], exe: Path, exe_sha: str,
                  timeout: float) -> dict[str, Any]:
    """F-B3-6 on the gate binary: the full b3 pipeline must not put a slot on the unit
    that ends in ``bx r3`` (label ``b3_6_bxr3``, ``indirect_multiway``); the artifact
    must still publish and pass O1/O2a/O2b (arbitrary entry reaches the general C)."""
    sys.path.insert(0, str(FIX))
    import b3conf  # noqa: E402
    from scopt.formats.sc_semantic import read_semantic_cache

    bx = b3conf.resolve(str(FIX / "b3.elf"), "b3_6_bxr3")
    bases = step_bases(out, modes, exe, exe_sha, cfg="6")
    res: dict[str, Any] = {"bx_r3_vaddr": hex(bx), "bases": bases}
    for m in modes:
        b = base_info(out, m, "6")
        args = ["optimize", str(b["snapshot"]), "--pipeline", "b3", "--b3", "--store",
                str(out / "store-6" / m), "--profile", str(b["profile"]), "--runtime-identity",
                str(b["ri"]), "--prep", str(b["prep"]), "--elf", str(b["elf"]),
                "--selftest", str(out / "hostsym_selftest.json"), "--json"]
        rc, so = cli(args, out / "logs" / f"optimize-6-{m}.log")
        try:
            doc = json.loads(so)
        except ValueError:
            res[m] = {"exit": rc, "ok": False, "error": "no JSON"}
            continue
        r: dict[str, Any] = {"exit": rc, "status": doc.get("status"),
                             "reason": doc.get("reason"),
                             "passes": [{"pass": p.get("pass"), "outcome": p.get("outcome"),
                                         "reason": p.get("reason"),
                                         "refused": {k: v for k, v in
                                                     (p.get("stats") or {}).items()
                                                     if k.startswith("refused:")}}
                                        for p in doc.get("passes", [])]}
        # the base unit(s) whose guest range holds the bx r3 (TB pc is 0 in the records:
        # use the snapshot's vaddrs) and whether the artifact grew any of them
        from scopt.importer.import_cache import open_snapshot

        snap = open_snapshot(b["snapshot"])
        holders = [u.uid for u in snap.units()
                   if u.vaddrs and u.vaddrs[0] <= bx < u.vaddrs[0] + max(u.guest_size, 1)]
        r["bx_r3_units"] = holders
        adir = doc.get("artifact_dir")
        if adir:
            side = json.loads((Path(adir) / "cache.sc.scopt.json").read_text())
            grown = set((side.get("x_b3") or {}).get("grown") or [])
            r["bx_r3_units_guarded"] = sum(1 for u in holders if u in grown)
            f = read_semantic_cache(Path(adir) / "cache.sc")
            if f.ext is not None:
                r["slots"] = {"SPEC": sum(1 for s in f.ext.slots if s.kind == 2),
                              "GENERAL": sum(1 for s in f.ext.slots if s.kind == 1)}
                r["variants"] = len(f.ext.variants)
            r["validation"] = _validation(Path(adir))
            facts = []
            rp = Path(adir) / "regions.jsonl"
            for line in (rp.read_text().splitlines() if rp.is_file() else []):
                row = json.loads(line)
                if row.get("kind") != "multi_tb":
                    continue
                for e in row.get("entries", []):
                    for fr in (e.get("precondition") or {}).get("facts", []):
                        facts.append({"field": fr.get("field"), "value": fr.get("value"),
                                      "source": fr.get("source")})
            r["spec_facts"] = facts
        spec = bool((r.get("slots") or {}).get("SPEC"))
        od = out / "dyn-6" / m
        if od.exists():
            import shutil

            shutil.rmtree(od)
        dyn_args = ["validate", str(doc.get("artifact_dir")), "--dynamic", "-c",
                    str(b["toml"]), "--baseline", str(b["snapshot"]), "--inputs",
                    str(b["input"]), "--fixture-input", "6", "--fixture-expected",
                    str(FIX / "expected.py"), "--timeout", str(timeout), "--qemu", str(exe),
                    "--out", str(od), "--json", "--oracles",
                    "O1,O2a,O2b,O3,O5" if spec else "O1,O2a,O2b,O5"]
        if spec:
            dyn_args += ["--o3", json.dumps({"stats": {"links_armed": ">0"},
                                             "hits": {"variant 0 C'": ">0"}}),
                         "--hit-target", "variant 0 C'"]
        if doc.get("artifact_dir"):
            rcd, _ = cli(dyn_args, out / "logs" / f"dynamic-6-{m}.log")
            dj = od / "dynamic_oracles.json"
            dd = json.loads(dj.read_text()) if dj.is_file() else None
            r["dynamic"] = {"exit": rcd, "verdict": None if dd is None else dd.get("verdict"),
                            "results": [{"oracle": o.get("oracle"), "verdict": o.get("verdict"),
                                         "detail": str(o.get("detail"))[:240]}
                                        for c in (dd or {}).get("cases", [])
                                        for o in c.get("oracles", [])]}
        r["ok"] = (r.get("status") == "published" and bool(r.get("bx_r3_units")) and
                   r.get("bx_r3_units_guarded") == 0 and
                   (r.get("dynamic") or {}).get("verdict") == "pass")
        log("indirect", m, r["ok"], r.get("bx_r3_units"), r.get("bx_r3_units_guarded"),
            r.get("slots"), r.get("spec_facts"), (r.get("dynamic") or {}).get("verdict"))
        res[m] = r
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--steps", default="bases,static,identity,publish,dynamic,indirect")
    ap.add_argument("--modes", default="icount,noicount")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    env()
    exe, exe_sha = gate()
    modes = a.modes.split(",")
    steps = a.steps.split(",")
    jf = out / "wp21-integ.json"
    doc: dict[str, Any] = json.loads(jf.read_text()) if jf.is_file() else {}
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain", "--", "scopt"],
                           capture_output=True, text=True).stdout.strip()
    doc["env"] = {"scopt_head": head, "scopt_dirty": bool(dirty),
                  "gate_exe": str(exe), "gate_sha256": exe_sha,
                  "fixture_elf_sha256": sha256(FIX / "b3.elf"),
                  "loadavg_start": Path("/proc/loadavg").read_text().strip(),
                  "cpus": CPUS, "timed": False}
    if "bases" in steps:
        doc["bases"] = step_bases(out, modes, exe, exe_sha)
    if "static" in steps:
        doc["static"] = step_static(out, modes)
    if "identity" in steps:
        doc["identity"] = step_identity(out, modes)
    if "publish" in steps:
        doc["publish"] = step_publish(out, modes, doc.get("static", {}))
    if "dynamic" in steps:
        doc["dynamic"] = step_dynamic(out, modes, doc.get("static", {}), exe, a.timeout)
    if "indirect" in steps:
        doc["indirect"] = step_indirect(out, modes, exe, exe_sha, a.timeout)
    doc["env"]["loadavg_end"] = Path("/proc/loadavg").read_text().strip()
    jf.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    summary = {k: {m: (v.get(m) or {}).get("ok") for m in modes}
               for k, v in doc.items() if k in ("bases", "static", "identity", "publish",
                                                   "dynamic", "indirect")}
    log("summary", json.dumps(summary))
    return 0 if all(x for v in summary.values() for x in v.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
