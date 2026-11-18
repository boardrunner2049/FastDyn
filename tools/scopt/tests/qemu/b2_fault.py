#!/usr/bin/env python3
"""F-B2-3 fault attribution driver (design §7, WP15B): the CLI end to end on the
fault fixture (``fixtures/fault``), per image and icount mode.

    b2_fault.py --out OUT [--variants a,b,c,d,fixture] [--modes noicount[,icount]]
                [--reps 3] [--qemu QEMU] [--timeout 120]

Per (mode, image) in ``OUT/<mode>/<image>/`` (every step is a ``python -m scopt``
subprocess run from this worktree; ``cmds.jsonl`` and ``logs/`` record them):

1. render ``fault-<image>.toml.in`` like ``fixtures/lib/fdrun.py`` (integrated
   QEMU, ``supercache = "cache/fault-<image>.sc"``, ``supercache_flat = false``,
   the icount line only in mode ``icount``);
2. ``prepare`` -> ``bundle identity`` / ``runtime-identity`` -> ``import`` (store);
3. the FAULTING units: every snapshot unit whose guest range ``[pc, pc+size)``
   contains the ELF address of a faulting instruction (read from the ELF symbol
   table with pyelftools: ``a_fault_ldr``, ``b_fault_sdiv``, ``c_fault_ldrb``,
   and for (d) the MMIO instruction ``d_mmio_ldr``);
4. artifacts (``optimize``; the test pipeline ``test-move-units`` for moves):
   * ``identity``  the identity pipeline (V9 sha256 equality);
   * ``plus64``    faulting units moved by +64 MiB;
   * ``minus64c``  faulting units moved by -64 MiB, applied by NativeLinker to the
                   re-imported snapshot of a +128 MiB artifact (net +64 MiB). A
                   direct -64 MiB move of the original placement is impossible:
                   every unit's tb_offset is far below 64 MiB, so tb - 64 MiB < 0
                   < ROUND_UP(prologue_end, 64). That attempt is run too and its
                   outcome recorded (``minus64_direct``);
   * ``minusmax``  the largest feasible negative move of the original placement:
                   the faulting units are packed from ROUND_UP(prologue_end, 64),
                   and the non-faulting units that occupied that range are moved
                   to ROUND_UP(max_end, 64) onwards;
   each with V1-V9, ``scopt validate A`` and an in-process per-moved-unit V6
   check (search data decoded at the new placement; every recovery point
   unwinds to the original (insn, words); the faulting insn is a recovery point);
5. ``scopt run --arms nocache,warm,identity,plus64,minus64c,minusmax --reps N``
   (correctness only; the BENCH times are not analysed);
6. ``scopt diag exec`` (``-d exec,int``) on rep 0 of every deploy arm: the TB
   whose ``Trace`` line precedes each ``Taking exception`` line, the
   ``cpu_io_recompile`` rewinds, and (c)/(d) the one-instruction CF_NOIRQ TB key.

Results: ``OUT/<mode>/<image>/fault.json`` and ``OUT/b2-fault.json``.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))
from scopt.decode.search import decode_search, is_pcrel, unwind  # noqa: E402
from scopt.formats.sc_v5 import read_sc, sha256_sparse  # noqa: E402
from scopt.model.cache_snapshot import snapshot_dir_name  # noqa: E402

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FIX = ROOT / "fixtures/fault"
PY = sys.executable
INT_QEMU = ROOT / "wt/qemu-int/build/qemu-system-arm"
PIPE_MOVE = WT / "tests/data/pipelines/test-move-units.toml"
ICOUNT_LINE = "icount = { shift = 0, sleep = false, align = false }"
MIB = 1 << 20
TB_ALIGN = 64
CF_NOIRQ = 0x10000
EXC_RETURN = 8  # "Taking exception 8 [QEMU v7M exception exit]"

# variant -> (symbol of the faulting / MMIO insn, variant id printed by the handler)
VARIANT_SYMS = {"a": ("a_fault_ldr", 0xA), "b": ("b_fault_sdiv", 0xB),
                "c": ("c_fault_ldrb", 0xC), "d": ("d_mmio_ldr", None)}
IMAGES = {"a": ["a"], "b": ["b"], "c": ["c"], "d": ["d"], "fixture": ["a", "b", "c", "d"]}
ARMS = ["nocache", "warm", "identity", "plus64", "minus64c", "minusmax"]
DEPLOY_ARMS = ARMS[1:]
MOVED_ARMS = ("plus64", "minus64c", "minusmax")

_TRACE_RE = re.compile(r"^Trace (\d+): (0x[0-9a-f]+) \[([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)/"
                       r"([0-9a-f]+)\]")
_EXC_RE = re.compile(r"^Taking exception (\d+) \[([^\]]*)\]")
_REWIND_RE = re.compile(r"^cpu_io_recompile: rewound execution of TB to ([0-9a-f]+)")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


# ---------------------------------------------------------------------------
# pure helpers (unit-tested in tests/acceptance/test_b2_fault_attribution.py)
# ---------------------------------------------------------------------------

def render_config(template: str, image: str, qemu: str, mode: str) -> str:
    """``fixtures/lib/fdrun.py render()`` for arm ``sc`` (icount) / ``sc-noicount``."""
    sc_lines = f'supercache = "cache/fault-{image}.sc"\nsupercache_flat = false'
    return (template.replace("@SUPERCACHE@", sc_lines)
            .replace("@ICOUNT@", ICOUNT_LINE if mode == "icount" else "")
            .replace("@QEMU@", qemu))


def elf_symbols(elf: Path) -> dict[str, int]:
    from elftools.elf.elffile import ELFFile
    out: dict[str, int] = {}
    with open(elf, "rb") as f:
        st = ELFFile(f).get_section_by_name(".symtab")
        for s in st.iter_symbols():
            if s.name:
                out[s.name] = s["st_value"]
    return out


def units_containing(units: list[dict[str, Any]], addr: int) -> list[dict[str, Any]]:
    """Units (dicts with pc, guest_size) whose guest range contains ``addr``."""
    return [u for u in units if u["pc"] <= addr < u["pc"] + u["guest_size"]]


def minusmax_moves(layout: list[tuple[str, int, int]], faulting: list[str],
                   prologue_end: int) -> tuple[dict[str, int], dict[str, int]]:
    """Largest feasible negative placement of ``faulting`` (in slot order).

    ``layout`` is ``[(uid, tb_offset, end_offset)]`` in slot order. The faulting
    units are packed from ``ROUND_UP(prologue_end, 64)`` (the lowest offset the
    loader allows); every other unit that overlaps the packed range is moved to
    ``ROUND_UP(max_end, 64)`` onwards (slot order). Returns
    ``(faulting moves, evicted moves)`` as ``{uid: new_tb_offset}``.
    """
    by = {u: (tb, end) for u, tb, end in layout}
    fset = set(faulting)
    lo = cur = round_up(prologue_end, TB_ALIGN)
    fm: dict[str, int] = {}
    for u, _, _ in layout:
        if u in fset:
            tb, end = by[u]
            fm[u] = cur
            cur = round_up(cur + (end - tb), TB_ALIGN)
    hi = cur
    top = round_up(max(end for _, _, end in layout), TB_ALIGN)
    ev: dict[str, int] = {}
    for u, tb, end in layout:
        if u in fset:
            continue
        if tb < hi and end > lo:
            ev[u] = top
            top = round_up(top + (end - tb), TB_ALIGN)
    return fm, ev


def parse_diag_log(text: str) -> dict[str, Any]:
    """Exceptions with the preceding Trace line, io-recompile rewinds, Trace keys."""
    last: dict[str, Any] | None = None
    exceptions: list[dict[str, Any]] = []
    rewinds: collections.Counter = collections.Counter()
    keys: collections.Counter = collections.Counter()
    tcs: dict[str, set] = collections.defaultdict(set)
    pending: dict[str, Any] | None = None
    for line in text.splitlines():
        m = _TRACE_RE.match(line)
        if m:
            last = {"tc": int(m.group(2), 16), "cs_base": int(m.group(3), 16),
                    "pc": int(m.group(4), 16), "flags": int(m.group(5), 16),
                    "cflags": int(m.group(6), 16)}
            k = key_str(last)
            keys[k] += 1
            tcs[k].add(last["tc"])
            pending = None
            continue
        m = _EXC_RE.match(line)
        if m:
            pending = {"excp": int(m.group(1)), "name": m.group(2), "lines": [line],
                       "trace_before": dict(last) if last else None}
            exceptions.append(pending)
            continue
        if pending is not None and line.startswith("..."):
            pending["lines"].append(line)
            continue
        pending = None
        m = _REWIND_RE.match(line)
        if m:
            rewinds[f"0x{int(m.group(1), 16):x}"] += 1
    return {"exceptions": exceptions, "rewinds": dict(rewinds), "keys": dict(keys),
            "tcs": {k: sorted(v) for k, v in tcs.items()}}


def key_str(t: dict[str, Any]) -> str:
    return (f"pc=0x{t['pc']:x}/flags=0x{t['flags']:x}/cs_base=0x{t['cs_base']:x}/"
            f"cflags=0x{t['cflags']:x}")


def reg_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("Reg ")]


def fault_reports(regs: list[str]) -> list[dict[str, int]]:
    """Six-line handler reports (variant id 0xa/0xb/0xc first) from Reg lines."""
    vals = [(int(ln[4:ln.index(":")]), int(ln.split(":")[1], 16)) for ln in regs]
    out = []
    i = 0
    while i + 5 < len(vals):
        if vals[i] == (0, vals[i][1]) and vals[i][1] in (0xA, 0xB, 0xC) and \
                [r for r, _ in vals[i:i + 6]] == [0, 0, 1, 2, 3, 0]:
            v = [x for _, x in vals[i:i + 6]]
            out.append({"variant": v[0], "pc": v[1], "xpsr": v[2], "cfsr": v[3],
                        "bfar": v[4], "ipsr": v[5]})
            i += 6
        else:
            i += 1
    return out


def compact_values(regs: list[str]) -> str:
    """One-line printed values: per fault report and for (d)'s four lines."""
    parts = []
    reps = fault_reports(regs)
    for r in reps:
        parts.append(f"({chr(ord('a') + r['variant'] - 0xA)}) pc=0x{r['pc']:x} "
                     f"xpsr=0x{r['xpsr']:08x} cfsr=0x{r['cfsr']:08x} bfar=0x{r['bfar']:x} "
                     f"ipsr={r['ipsr']}")
    tail = regs[6 * len(reps):]
    if len(tail) == 4:
        v = [ln.split(":")[1] for ln in tail]
        parts.append(f"(d) cpuid={v[0]} n={int(v[1], 16)} insn=0x{int(v[2], 16):x} sum={v[3]}")
    elif tail:
        parts.append("other: " + ", ".join(tail))
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

class Workload:
    def __init__(self, out: Path, mode: str, image: str, qemu: Path, reps: int,
                 timeout: float) -> None:
        self.out = out / mode / image
        self.mode, self.image, self.qemu, self.reps, self.timeout = mode, image, qemu, reps, timeout
        self.logs = self.out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(WT)
        for k in list(self.env):
            if k.startswith("QEMU_SC_") or k in ("FASTDYN_QEMU_ICOUNT",):
                self.env.pop(k)
        self.checks: list[dict[str, Any]] = []
        self.doc: dict[str, Any] = {"mode": mode, "image": image, "dir": str(self.out)}

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{self.mode}/{self.image}] {msg}"
        print(line, flush=True)
        with open(self.out / "fault.log", "a") as f:
            f.write(line + "\n")

    def check(self, name: str, ok: bool, detail: Any = "", evidence: Any = None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail,
                            "evidence": evidence})
        if not ok:
            self.log(f"FAIL {name}: {str(detail)[:300]}")
        return bool(ok)

    def scopt(self, name: str, *args: Any) -> tuple[int, str, Path]:
        self.n += 1
        argv = [PY, "-m", "scopt", *[str(a) for a in args]]
        t0 = time.monotonic()
        p = subprocess.run(argv, cwd=str(WT), env=self.env, capture_output=True, text=True,
                           timeout=self.timeout * 40)
        dt = time.monotonic() - t0
        logp = self.logs / f"{self.n:02d}-{name}.out"
        logp.write_text(p.stdout + ("\n--- stderr ---\n" + p.stderr if p.stderr else ""))
        with open(self.out / "cmds.jsonl", "a") as f:
            f.write(json.dumps({"n": self.n, "name": name, "argv": argv[2:],
                                "rc": p.returncode, "seconds": round(dt, 3),
                                "log": str(logp)}) + "\n")
        self.log(f"scopt {name} rc={p.returncode} ({dt:.1f}s)")
        return p.returncode, p.stdout, logp

    # -- 1-2: config, prepare, import -------------------------------------
    def setup(self) -> bool:
        tmpl = (FIX / f"fault-{self.image}.toml.in").read_text()
        cfg = self.out / f"fault-{self.image}-{self.mode}-sc.toml"
        cfg.write_text(render_config(tmpl, self.image, str(self.qemu), self.mode))
        self.cfg = cfg
        self.elf = FIX / f"fault_{self.image}.elf"
        self.doc.update(config=str(cfg), config_sha256=sha256_file(cfg), elf=str(self.elf),
                        elf_sha256=sha256_file(self.elf))
        prep = self.out / "prep"
        rc, _, logp = self.scopt("prepare", "prepare", "-c", cfg, "--out", prep, "--qemu",
                                 self.qemu, "--timeout", self.timeout)
        pj = json.loads((prep / "prep.json").read_text()) if (prep / "prep.json").is_file() \
            else {}
        if not self.check("prepare PASS", rc == 0 and pj.get("verdict") == "PASS",
                          pj.get("failures"), str(logp)):
            return False
        self.prep_sc, self.prep_manifest = Path(pj["sc"]), Path(pj["manifest"])
        self.bundle, self.ri = self.out / "bundle.json", self.out / "runtime_identity.json"
        rc1, _, _ = self.scopt("bundle-identity", "bundle", "identity", "-c", prep / "run.toml",
                               "--manifest", self.prep_manifest, "--out", self.bundle)
        rc2, _, _ = self.scopt("runtime-identity", "bundle", "runtime-identity", "--run-dir",
                               prep / "oracle", "--sc", self.prep_sc, "--scm",
                               prep / "capture.scm", "--out", self.ri)
        if not self.check("bundle identity / runtime-identity", rc1 == 0 and rc2 == 0,
                          f"{rc1} {rc2}"):
            return False
        rc, out, logp = self.scopt("import", "import", prep, "--bundle", self.bundle,
                                   "--runtime-identity", self.ri, "--store",
                                   self.out / "store", "--json")
        if not self.check("import", rc == 0, f"rc={rc}", str(logp)):
            return False
        s = json.loads(out)
        self.snapshot_id = s["snapshot_id"]
        self.snap = self.out / "store/snapshots" / snapshot_dir_name(s["snapshot_id"])
        self.doc["snapshot_id"] = self.snapshot_id
        self.doc["snapshot_status"] = s["status"]
        self.check("every unit ok_relocatable (no pins)",
                   set(s["status"]) <= {"ok_relocatable"} or
                   all(v == 0 for k, v in s["status"].items() if k != "ok_relocatable"),
                   s["status"])
        return True

    # -- 3: faulting units ------------------------------------------------
    def faulting(self) -> bool:
        from scopt.importer.import_cache import open_snapshot
        snap = open_snapshot(self.snap)
        self.snap_obj = snap
        h = snap.header
        self.code_address = h.code_address
        syms = elf_symbols(self.elf)
        units = [{"uid": u.uid, "slot": u.slot, "pc": u.vaddrs[0], "guest_size": u.guest_size,
                  "icount": u.icount, "cflags": u.key.cflags, "flags": u.key.flags,
                  "cs_base": u.key.cs_base, "tb_offset": u.tb_offset, "tc_offset": u.tc_offset,
                  "end_offset": u.end_offset} for u in snap.units()]
        self.units = {u["uid"]: u for u in units}
        fault: dict[str, Any] = {}
        all_uids: list[str] = []
        for v in IMAGES[self.image]:
            sym, vid = VARIANT_SYMS[v]
            addr = syms[sym] & ~1
            hits = units_containing(units, addr)
            fault[v] = {"symbol": sym, "elf_address": f"0x{addr:x}", "variant_id": vid,
                        "units": [{k: (f"0x{x:x}" if isinstance(x, int) and k not in
                                       ("slot", "guest_size", "icount") else x)
                                   for k, x in u.items()} for u in hits]}
            self.check(f"({v}) faulting insn {sym}@0x{addr:x} lies in >= 1 snapshot unit",
                       bool(hits), [u["uid"] for u in hits])
            if v in ("c", "d"):
                one = [u for u in hits if u["pc"] == addr and u["icount"] == 1
                       and u["cflags"] & CF_NOIRQ]
                self.check(f"({v}) one-insn CF_NOIRQ TB at 0x{addr:x} is in the snapshot",
                           len(one) == 1, [u["uid"] for u in one])
                fault[v]["noirq_unit"] = one[0]["uid"] if one else None
            for u in hits:
                if u["uid"] not in all_uids:
                    all_uids.append(u["uid"])
        order = {u["uid"]: u["slot"] for u in units}
        self.fault_uids = sorted(all_uids, key=lambda x: order[x])
        self.fault = fault
        self.syms = syms
        self.doc["faulting"] = fault
        self.doc["faulting_units"] = self.fault_uids
        self.doc["header"] = {"code_address": f"0x{h.code_address:x}",
                              "code_size": f"0x{h.code_size:x}",
                              "prologue_end": h.prologue_end,
                              "max_end": f"0x{snap.max_end:x}",
                              "target_page_bits": h.target_page_bits}
        return bool(self.fault_uids)

    # -- 4: artifacts ------------------------------------------------------
    def optimize(self, name: str, snap: Path, store: Path, params: dict | None) -> dict | None:
        args = ["optimize", snap, "--store", store, "--json"]
        if params is None:
            args += ["--pipeline", "identity"]
        else:
            pf = self.out / f"params-{name}.json"
            pf.write_text(json.dumps({"move-units": params}, indent=1) + "\n")
            args += ["--pipeline", PIPE_MOVE, "--params", pf]
        rc, out, logp = self.scopt(f"optimize-{name}", *args)
        try:
            res = json.loads(out)
        except ValueError:
            res = {}
        res["rc"] = rc
        res["log"] = str(logp)
        return res

    def artifact_checks(self, name: str, res: dict, src_snap) -> dict[str, Any]:
        """V1-V9 codes, scopt validate, move map, per-moved-unit V6 at the new placement."""
        A = Path(res["artifact_dir"])
        info: dict[str, Any] = {"artifact_dir": str(A), "artifact_id": res["artifact_id"],
                                "validation": res["validation"],
                                "cache_sc_sha256": sha256_sparse(A / "cache.sc")}
        codes = res["validation"]
        want9 = "pass" if name == "identity" else "skip"
        self.check(f"[{name}] V1-V8 pass, V9 {want9}",
                   all(codes.get(f"V{i}") == "pass" for i in range(1, 9))
                   and codes.get("V9") == want9, codes, str(A / "validation.json"))
        vrows = json.loads((A / "validation.json").read_text())["rows"]
        v6 = [r for r in vrows if r["code"] == "V6"]
        info["V6_rows"] = v6
        self.check(f"[{name}] validation.json V6 rows pass (unwind, reimport_recovery)",
                   bool(v6) and all(r["result"] == "pass" for r in v6),
                   [f"{r['check']}: {r['detail']}" for r in v6])
        rc, _, logp = self.scopt(f"validate-{name}", "validate", A)
        self.check(f"[{name}] scopt validate A (V1-V9 re-validation, MANIFEST)", rc == 0,
                   f"rc={rc}", str(logp))
        side = json.loads((A / "cache.sc.scopt.json").read_text())
        mm = side.get("move_map", {})
        info["move_map"] = mm
        info["pipeline"] = side.get("pipeline")
        # per moved faulting unit: own V6 recomputation on the artifact bytes
        sc = read_sc(A / "cache.sc")
        page_mask = ~((1 << sc.header.target_page_bits) - 1)
        per: dict[str, Any] = {}
        for uid in self.fault_uids:
            u = src_snap.unit(uid)
            su = sc.units()[[x.slot for x in sc.units()].index(u.slot)]
            rec = su.record
            row: dict[str, Any] = {"slot": u.slot, "old_tb_offset": f"0x{u.tb_offset:x}",
                                   "new_tb_offset": f"0x{rec.tb_offset:x}",
                                   "delta": rec.tb_offset - u.tb_offset,
                                   "new_tc_ptr": f"0x{self.code_address + rec.tc_offset:x}"}
            ok = rec.tc_offset - rec.tb_offset == u.tc_offset - u.tb_offset and \
                uid.startswith(rec.tb_uid if rec.tb_uid.startswith("tb:") else
                               f"tb:{rec.tb_uid}")
            rows = decode_search(su.search, rec.icount, is_pcrel(rec.cflags))
            bad = []
            hits = []
            for p in u.recovery:
                got = unwind(rows, p.host_return_offset)
                if got is None or got[0] != p.insn or tuple(got[1]) != tuple(p.words):
                    bad.append({"point": p.id, "got": got, "want": (p.insn, list(p.words))})
                    continue
                gpc = (u.vaddrs[0] & page_mask) | p.words[0] if is_pcrel(rec.cflags) \
                    else p.words[0]
                hits.append((p.source, p.insn, gpc))
            targets = {int(self.fault[v]["elf_address"], 16) for v in self.fault
                       if any(x["uid"] == uid for x in self.fault[v]["units"])}
            fpts = [h for h in hits if h[2] in targets]
            row.update(recovery_points=len(u.recovery), unwind_bad=bad,
                       faulting_insn_points=[{"source": s, "insn": i, "guest_pc": f"0x{g:x}"}
                                             for s, i, g in fpts])
            row["ok"] = ok and not bad and bool(fpts)
            per[uid] = row
        info["moved_units_v6"] = per
        self.check(f"[{name}] static V6 for every faulting unit at its placement in the "
                   "artifact: all recovery points unwind to the original (insn, words); the "
                   "faulting insn is a recovery point",
                   all(r["ok"] for r in per.values()),
                   {k: (v["new_tb_offset"], v["delta"], len(v["unwind_bad"]),
                        v["faulting_insn_points"]) for k, v in per.items()})
        return info

    def artifacts(self) -> bool:
        snap = self.snap_obj
        store = self.out / "store"
        arts: dict[str, Any] = {}
        # identity
        res = self.optimize("identity", self.snap, store, None)
        if not self.check("[identity] published", res["rc"] == 0 and
                          res.get("status") == "published", res.get("reason"), res["log"]):
            return False
        arts["identity"] = self.artifact_checks("identity", res, snap)
        self.check("[identity] V9: sha256(out) == sha256(in)",
                   arts["identity"]["cache_sc_sha256"] == sha256_sparse(self.snap / "cache.sc"))
        # plus64
        res = self.optimize("plus64", self.snap, store,
                            {"deltas": {u: hex(64 * MIB) for u in self.fault_uids}})
        if not self.check("[plus64] published", res["rc"] == 0 and
                          res.get("status") == "published", res.get("reason"), res["log"]):
            return False
        arts["plus64"] = a = self.artifact_checks("plus64", res, snap)
        self.check("[plus64] exactly the faulting units moved, each by +64 MiB",
                   set(a["move_map"]) == set(self.fault_uids) and
                   all(m["delta"] == 64 * MIB for m in a["move_map"].values()), a["move_map"])
        # direct -64 MiB: infeasible; record why and what the pipeline does with it
        pe = round_up(snap.header.prologue_end, TB_ALIGN)
        why = {u: {"tb_offset": f"0x{snap.unit(u).tb_offset:x}",
                   "tb_offset_minus_64MiB": snap.unit(u).tb_offset - 64 * MIB,
                   "lowest_legal_tb_offset": f"0x{pe:x}"} for u in self.fault_uids}
        feasible = [u for u in self.fault_uids if snap.unit(u).tb_offset - 64 * MIB >= pe]
        res = self.optimize("minus64-direct", self.snap, store,
                            {"deltas": {u: "-" + hex(64 * MIB) for u in self.fault_uids}})
        direct = {"feasible_units": feasible, "why": why, "rc": res["rc"],
                  "status": res.get("status"), "log": res["log"]}
        if res.get("artifact_dir"):
            side = json.loads((Path(res["artifact_dir"]) / "cache.sc.scopt.json").read_text())
            direct["pipeline"] = side.get("pipeline")
            direct["moved"] = len(side.get("move_map", {}))
            direct["artifact_dir"] = res["artifact_dir"]
        self.check("direct -64 MiB is infeasible for every faulting unit "
                   "(tb_offset - 64 MiB < ROUND_UP(prologue_end, 64)) and is not applied "
                   "(move-units fails edit_precondition; 0 units moved)",
                   not feasible and direct.get("moved", 0) == 0 and
                   any(p.get("reason") == "edit_precondition" for p in direct.get("pipeline", [])),
                   {k: v for k, v in direct.items() if k != "why"})
        self.doc["minus64_direct"] = direct
        # minus64c: +128 MiB artifact -> re-import -> -64 MiB
        res = self.optimize("plus128", self.snap, store,
                            {"deltas": {u: hex(128 * MIB) for u in self.fault_uids}})
        if not self.check("[plus128] published", res["rc"] == 0 and
                          res.get("status") == "published", res.get("reason"), res["log"]):
            return False
        A128 = Path(res["artifact_dir"])
        arts["plus128"] = self.artifact_checks("plus128", res, snap)
        cstore = self.out / "store-chain"
        rc, out, logp = self.scopt("import-plus128", "import", "--sc", A128 / "cache.sc",
                                   "--scm", A128 / "cache.scm", "--manifest",
                                   self.prep_manifest, "--bundle", self.bundle,
                                   "--runtime-identity", self.ri, "--store", cstore, "--json")
        if not self.check("+128 MiB artifact re-imports (derived .scm) to its "
                          "output_snapshot_id", rc == 0 and
                          json.loads(out)["snapshot_id"] == res["output_snapshot_id"],
                          f"rc={rc}", str(logp)):
            return False
        from scopt.importer.import_cache import open_snapshot
        csnap_dir = cstore / "snapshots" / snapshot_dir_name(res["output_snapshot_id"])
        csnap = open_snapshot(csnap_dir)
        st = json.loads(out)["status"]
        self.check("re-imported +128 MiB snapshot: every unit ok_relocatable",
                   all(v == 0 for k, v in st.items() if k != "ok_relocatable"), st)
        # recovery points of the moved units re-derived from the moved bytes are the same
        same = all([(p.host_return_offset, p.insn, tuple(p.words)) for p in
                    csnap.unit(u).recovery] == [(p.host_return_offset, p.insn, tuple(p.words))
                                               for p in snap.unit(u).recovery]
                   for u in self.fault_uids)
        self.check("re-import of the +128 MiB placement derives the same recovery points "
                   "(host return offset, insn, words) for every faulting unit", same)
        res = self.optimize("minus64c", csnap_dir, cstore,
                            {"deltas": {u: "-" + hex(64 * MIB) for u in self.fault_uids}})
        if not self.check("[minus64c] published", res["rc"] == 0 and
                          res.get("status") == "published", res.get("reason"), res["log"]):
            return False
        arts["minus64c"] = a = self.artifact_checks("minus64c", res, csnap)
        self.check("[minus64c] NativeLinker applied d = -64 MiB to exactly the faulting units",
                   set(a["move_map"]) == set(self.fault_uids) and
                   all(m["delta"] == -64 * MIB for m in a["move_map"].values()), a["move_map"])
        self.check("[minus64c] cache.sc byte-identical to [plus64] (same final placement "
                   "reached through +128 MiB then -64 MiB)",
                   a["cache_sc_sha256"] == arts["plus64"]["cache_sc_sha256"],
                   (a["cache_sc_sha256"], arts["plus64"]["cache_sc_sha256"]))
        # minusmax
        layout = [(u.uid, u.tb_offset, u.end_offset) for u in snap.units()]
        fm, ev = minusmax_moves(layout, self.fault_uids, snap.header.prologue_end)
        res = self.optimize("minusmax", self.snap, store,
                            {"moves": {u: hex(o) for u, o in {**fm, **ev}.items()}})
        if not self.check("[minusmax] published", res["rc"] == 0 and
                          res.get("status") == "published", res.get("reason"), res["log"]):
            return False
        arts["minusmax"] = a = self.artifact_checks("minusmax", res, snap)
        deltas = {u: fm[u] - snap.unit(u).tb_offset for u in fm}
        self.check("[minusmax] every faulting unit moved down (negative delta), the first "
                   "to ROUND_UP(prologue_end, 64)",
                   all(d < 0 for d in deltas.values()) and
                   fm[self.fault_uids[0]] == pe and
                   all(a["move_map"].get(u, {}).get("delta") == d for u, d in deltas.items()),
                   {u: hex(d) for u, d in deltas.items()})
        self.doc["minusmax_plan"] = {"faulting": {u: hex(o) for u, o in fm.items()},
                                     "faulting_deltas": deltas,
                                     "evicted": {u: hex(o) for u, o in ev.items()}}
        self.arts = arts
        self.doc["artifacts"] = arts
        return True

    # -- 5: runs -----------------------------------------------------------
    def runs(self) -> bool:
        d = self.out / "runs"
        args = ["run", "-c", self.cfg, "--arms", ",".join(ARMS), "--warm", self.snap]
        for a in ("identity", "plus64", "minus64c", "minusmax"):
            args += ["--artifact", f"{a}={self.arts[a]['artifact_dir']}"]
        args += ["--reps", self.reps, "--seed", 15, "--qemu", self.qemu, "--timeout",
                 self.timeout, "--no-report", "--out", d]
        rc, _, logp = self.scopt("run-arms", *args)
        rows = [json.loads(x) for x in (d / "runs.jsonl").read_text().splitlines()] \
            if (d / "runs.jsonl").is_file() else []
        self.rows = rows
        cnt = collections.Counter(r["arm"] for r in rows)
        self.check(f"every arm ran x{self.reps}, every run valid (exit 0, BENCH, no stray "
                   "capture files)", rc == 0 and all(cnt[a] == self.reps for a in ARMS)
                   and all(r["valid"] for r in rows), dict(cnt), str(d / "runs.jsonl"))
        dep = [r for r in rows if r["arm"] in DEPLOY_ARMS]
        self.check("deploy arms: post-run proof PASS, appended = 0",
                   all(r["verdict"] == "PASS" and r["appended"] == 0 for r in dep),
                   sorted({(r["arm"], r["verdict"], r["appended"]) for r in dep}))
        self.check("nocache arm: NOT_PROVED (nothing deployed)",
                   all(r["verdict"] == "NOT_PROVED" for r in rows if r["arm"] == "nocache"))
        # printed values
        exp = (FIX / "expected" / f"{self.image}.norm").read_text().splitlines()
        exp_regs = reg_lines(exp)
        by_arm: dict[str, Any] = {}
        for a in ARMS:
            rs = sorted((r for r in rows if r["arm"] == a), key=lambda r: r["rep"])
            regsets = {json.dumps(reg_lines(r["guest_norm"] or [])) for r in rs}
            regs = reg_lines(rs[0]["guest_norm"] or []) if rs else []
            banner = [ln for ln in (rs[0]["guest_norm"] or []) if ln.startswith(
                ("Current CPU type", "[BENCH]"))] if rs else []
            by_arm[a] = {"reps": len(rs), "regs": regs, "values": compact_values(regs),
                         "reps_identical": len(regsets) == 1, "banner": banner,
                         "run_dirs": [r["run_dir"] for r in rs],
                         "appended": [r["appended"] for r in rs],
                         "verdicts": [r["verdict"] for r in rs]}
        self.by_arm = by_arm
        base = by_arm["nocache"]["regs"]
        self.check("printed values identical across reps of every arm",
                   all(v["reps_identical"] for v in by_arm.values()),
                   {a: v["reps_identical"] for a, v in by_arm.items()})
        self.check("printed values identical across nocache, warm, identity, plus64, "
                   "minus64c, minusmax", all(v["regs"] == base for v in by_arm.values()),
                   {a: v["values"] for a, v in by_arm.items()})
        self.check("printed values equal fixtures/fault/expected/<image>.norm (derived from "
                   "the ELF)", base == exp_regs, {"expected": exp_regs, "got": base})
        self.check("banner and BENCH tag equal the expected .norm in every arm",
                   all(v["banner"] == [ln for ln in exp if ln.startswith(
                       ("Current CPU type", "[BENCH]"))] for v in by_arm.values()))
        # stacked PC == ELF address
        reps = fault_reports(base)
        want = {VARIANT_SYMS[v][1]: (v, int(self.fault[v]["elf_address"], 16))
                for v in IMAGES[self.image] if VARIANT_SYMS[v][1]}
        got = {r["variant"]: r["pc"] for r in reps}
        self.check("stacked PC (SP+24) == ELF address of the faulting insn, every fault",
                   {k: v[1] for k, v in want.items()} == got and
                   all(fault_reports(v["regs"]) == reps for v in by_arm.values()),
                   {f"({v[0]})": {"elf": f"0x{v[1]:x}", "stacked_pc":
                                  f"0x{got.get(k, -1):x}"} for k, v in want.items()})
        if "d" in IMAGES[self.image]:
            addr = int(self.fault["d"]["elf_address"], 16)
            tail = base[6 * len(reps):]
            self.check("(d) printed MMIO insn address == ELF address of d_mmio_ldr",
                       len(tail) == 4 and int(tail[2].split(":")[1], 16) == addr, tail)
        return True

    # -- 6: diag exec ------------------------------------------------------
    def diag(self) -> None:
        res: dict[str, Any] = {}
        for a in DEPLOY_ARMS:
            rd = self.by_arm[a]["run_dirs"][0]
            d = self.out / f"diag-{a}"
            rc, _, logp = self.scopt(f"diag-{a}", "diag", "exec", "--run-dir", rd, "--out", d,
                                     "--json", "--timeout", self.timeout)
            dj = d / "diag.json"
            if not self.check(f"[{a}] diag exec ran", rc == 0 and dj.is_file(), f"rc={rc}",
                              str(logp)):
                continue
            doc = json.loads(dj.read_text())
            logf = d / "qemu.log"
            text = logf.read_text(errors="replace")
            parsed = parse_diag_log(text)
            art = self.snap if a == "warm" else Path(self.arts[a]["artifact_dir"])
            sc = read_sc(art / "cache.sc")
            tc2slot = {self.code_address + u.record.tc_offset: u.slot for u in sc.units()}
            slot2uid = {u["slot"]: u["uid"] for u in self.units.values()}
            for e in parsed["exceptions"]:
                t = e["trace_before"]
                if t:
                    s = tc2slot.get(t["tc"])
                    t["slot"] = s
                    t["uid"] = slot2uid.get(s)
                    t["tc"] = f"0x{t['tc']:x}"
            res[a] = {"diag_json": str(dj), "qemu_log": str(logf),
                      "qemu_log_sha256": sha256_file(logf), "qemu_log_bytes": logf.stat().st_size,
                      "exit_code": doc["exit_code"],
                      "all_traces_in_snapshot": doc["all_traces_in_snapshot"],
                      "tc_not_in_snapshot": doc["tc_not_in_snapshot"],
                      "sc": doc["sc"], "exceptions": parsed["exceptions"],
                      "rewinds": parsed["rewinds"], "keys": parsed["keys"],
                      "tcs": {k: [f"0x{x:x}" for x in v] for k, v in parsed["tcs"].items()}}
            self.check(f"[{a}] diag: exit 0, every Trace tc.ptr is a record of the deployed "
                       "cache", doc["exit_code"] == 0 and doc["all_traces_in_snapshot"]
                       and not doc["tc_not_in_snapshot"] and doc["sc"] == str(art / "cache.sc"),
                       {"sc": doc["sc"], "outside": doc["tc_not_in_snapshot"][:3]})
        self.diags = res
        if not res:
            return
        # faults: the TB entered last before each exception is a faulting unit, with the same
        # key in every arm, at its moved address in the moved arms
        def faults(a: str) -> list[dict[str, Any]]:
            return [e for e in res[a]["exceptions"] if e["excp"] != EXC_RETURN]
        nf = len([v for v in IMAGES[self.image] if VARIANT_SYMS[v][1]])
        ref = faults("warm") if "warm" in res else []
        self.check("diag: one fault exception per faulting variant in every deploy arm",
                   all(len(faults(a)) == nf for a in res), {a: len(faults(a)) for a in res})
        self.check("diag: the exception lines (-d int) are identical across deploy arms",
                   all([e["lines"] for e in faults(a)] == [e["lines"] for e in ref]
                       for a in res), [e["lines"] for e in ref])
        tb_rows = {}
        ok_unit, ok_key, ok_moved = True, True, True
        for a in res:
            rows = []
            for i, e in enumerate(faults(a)):
                t = e["trace_before"] or {}
                rows.append({"key": key_str(t) if t else None, "tc": t.get("tc"),
                             "uid": t.get("uid")})
                if t.get("uid") not in self.fault_uids:
                    ok_unit = False
                if i < len(ref) and ref[i]["trace_before"] and t and \
                        key_str(t) != key_str(ref[i]["trace_before"]):
                    ok_key = False
                if a in MOVED_ARMS or a == "identity":
                    want = self.arts[a]["moved_units_v6"].get(t.get("uid"), {}).get("new_tc_ptr")
                    if want != t.get("tc"):
                        ok_moved = False
                else:
                    u = self.units.get(t.get("uid"))
                    if not u or f"0x{self.code_address + u['tc_offset']:x}" != t.get("tc"):
                        ok_moved = False
            tb_rows[a] = rows
        self.fault_tbs = tb_rows
        if nf:
            self.check("diag: the TB entered last before each fault is a faulting unit",
                       ok_unit, tb_rows)
            self.check("diag: that TB has the same key (pc/flags/cs_base/cflags) in every "
                       "deploy arm", ok_key)
            self.check("diag: its tc.ptr is the unit's placement in the deployed cache "
                       "(moved address in plus64/minus64c/minusmax)", ok_moved)
        ref_rw = res.get("warm", {}).get("rewinds")
        self.check("diag: cpu_io_recompile rewinds (guest pc -> count) identical across "
                   "deploy arms", all(res[a]["rewinds"] == ref_rw for a in res),
                   {a: res[a]["rewinds"] for a in res})
        # (c)/(d): the one-insn CF_NOIRQ TB key
        noirq: dict[str, Any] = {}
        for v in IMAGES[self.image]:
            if v not in ("c", "d"):
                continue
            uid = self.fault[v]["noirq_unit"]
            u = self.units[uid]
            k = key_str({"pc": u["pc"], "flags": u["flags"], "cs_base": u["cs_base"],
                         "cflags": u["cflags"]})
            per = {}
            for a in res:
                tcs = res[a]["tcs"].get(k, [])
                if a == "warm":
                    want = f"0x{self.code_address + u['tc_offset']:x}"
                else:
                    want = self.arts[a]["moved_units_v6"][uid]["new_tc_ptr"]
                per[a] = {"count": res[a]["keys"].get(k, 0), "tc": tcs, "expected_tc": want}
            noirq[v] = {"uid": uid, "key": k, "arms": per}
            cnts = {per[a]["count"] for a in per}
            self.check(f"({v}) one-insn CF_NOIRQ TB {k} appears with the same key and the "
                       "same count in every deploy arm", len(cnts) == 1 and 0 not in cnts,
                       {a: per[a]["count"] for a in per})
            self.check(f"({v}) its Trace tc.ptr is the unit's placement in each arm's cache "
                       "(moved in plus64/minus64c/minusmax)",
                       all(per[a]["tc"] == [per[a]["expected_tc"]] for a in per),
                       {a: (per[a]["tc"], per[a]["expected_tc"]) for a in per})
            if v == "d":
                addr = int(self.fault["d"]["elf_address"], 16)
                self.check("(d) cpu_io_recompile rewound to the MMIO insn 64 times in every "
                           "deploy arm", all(res[a]["rewinds"].get(f"0x{addr:x}") == 64
                                             for a in res),
                           {a: res[a]["rewinds"].get(f"0x{addr:x}") for a in res})
        self.noirq = noirq

    # -- 7: negative control ---------------------------------------------
    def negative(self) -> None:
        """A copy of the plus64 cache whose faulting TB's search row for the faulting insn
        names the guest pc 2 bytes earlier: static V6 must flag it, and the printed stacked
        PC must change (the check above is not vacuous)."""
        from scopt.decode.search import SearchRow, encode_search
        from scopt.formats.sc_v5 import SC_CODE_OFFSET, sparse_copy
        tb = (getattr(self, "fault_tbs", None) or {}).get("plus64") or []
        if len(IMAGES[self.image]) != 1 or not tb or not tb[0].get("uid"):
            return
        v = IMAGES[self.image][0]
        uid = tb[0]["uid"]
        addr = int(self.fault[v]["elf_address"], 16)
        d = self.out / "negative"
        d.mkdir(exist_ok=True)
        src = Path(self.arts["plus64"]["artifact_dir"]) / "cache.sc"
        dst = d / "tampered.sc"
        sparse_copy(src, dst)
        sc = read_sc(dst)
        u = self.snap_obj.unit(uid)
        su = next(x for x in sc.units() if x.slot == u.slot)
        rec = su.record
        pcrel = is_pcrel(rec.cflags)
        rows = list(decode_search(su.search, rec.icount, pcrel))
        page_mask = ~((1 << sc.header.target_page_bits) - 1)
        idx = next(p.insn for p in u.recovery if
                   ((u.vaddrs[0] & page_mask) | p.words[0] if pcrel else p.words[0]) == addr)
        old = encode_search(rows, pcrel)
        w = list(rows[idx].words)
        w[0] -= 2
        rows[idx] = SearchRow(tuple(w), rows[idx].host_end)
        new = encode_search(rows, pcrel)
        info: dict[str, Any] = {"unit": uid, "insn": idx, "tampered_sc": str(dst),
                                "search_len": [len(old), len(new)]}
        if len(new) != len(old) or su.search[:len(old)] != old:
            self.check("negative control: tampered search data has the original length",
                       False, info)
            return
        with open(dst, "r+b") as f:
            f.seek(SC_CODE_OFFSET + rec.tc_offset + rec.tc_size)
            f.write(new)
        # static: the per-unit unwind check on the tampered bytes
        sc2 = read_sc(dst)
        su2 = next(x for x in sc2.units() if x.slot == u.slot)
        rows2 = decode_search(su2.search, rec.icount, pcrel)
        bad = [p.id for p in u.recovery if unwind(rows2, p.host_return_offset) is None or
               tuple(unwind(rows2, p.host_return_offset)[1]) != tuple(p.words)]
        info["static_v6_mismatches"] = bad
        rc, _, logp = self.scopt("run-negative", "run", "-c", self.cfg, "--arms",
                                 "warm,tampered", "--warm", self.snap, "--artifact",
                                 f"tampered={dst}", "--reps", 1, "--seed", 15, "--qemu",
                                 self.qemu, "--timeout", self.timeout, "--no-report",
                                 "--out", d / "runs")
        rows_ = [json.loads(x) for x in (d / "runs/runs.jsonl").read_text().splitlines()] \
            if (d / "runs/runs.jsonl").is_file() else []
        t = next((r for r in rows_ if r["arm"] == "tampered"), None)
        regs = reg_lines(t["guest_norm"] or []) if t else []
        reps = fault_reports(regs)
        info.update(run_rc=rc, verdict=t and t["verdict"], appended=t and t["appended"],
                    printed=compact_values(regs), stacked_pc=[f"0x{r['pc']:x}" for r in reps],
                    runs=str(d / "runs/runs.jsonl"))
        self.doc["negative_control"] = info
        self.check("negative control (search row of the faulting insn shifted by -2 in a copy "
                   "of the plus64 cache): static V6 flags it and the run prints a stacked PC "
                   "!= the ELF address", bool(bad) and t is not None and
                   t["verdict"] == "PASS" and bool(reps) and reps[0]["pc"] != addr,
                   {k: info[k] for k in ("unit", "insn", "static_v6_mismatches", "stacked_pc",
                                         "printed", "verdict", "appended")}, str(logp))

    def run(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            if self.setup() and self.faulting() and self.artifacts() and self.runs():
                self.diag()
                self.negative()
        except Exception as e:  # recorded, never hidden
            import traceback
            self.check("driver completed without an exception", False,
                       traceback.format_exc()[-2000:])
            self.log(f"exception: {e!r}")
        self.doc["checks"] = self.checks
        self.doc["failed"] = [c["check"] for c in self.checks if not c["ok"]]
        self.doc["verdict"] = "pass" if self.checks and not self.doc["failed"] else "fail"
        self.doc["arms"] = getattr(self, "by_arm", None)
        self.doc["diag"] = getattr(self, "diags", None)
        self.doc["fault_tbs"] = getattr(self, "fault_tbs", None)
        self.doc["noirq"] = getattr(self, "noirq", None)
        self.doc["seconds"] = round(time.monotonic() - t0, 1)
        (self.out / "fault.json").write_text(json.dumps(self.doc, indent=1, sort_keys=True,
                                                        default=str) + "\n")
        self.log(f"verdict={self.doc['verdict']} failed={self.doc['failed']}")
        return self.doc


def render_tables(out: Path) -> str:
    """Markdown: per (mode, image) the variant x arm table and the artifact placements."""
    s = json.loads((out / "b2-fault.json").read_text())
    md: list[str] = []
    rel = lambda p: os.path.relpath(str(p), str(out))  # noqa: E731
    for wl in s["workloads"]:
        doc = json.loads((out / wl / "fault.json").read_text())
        mode, image = wl.split("/")
        gate = "gated" if mode == "noicount" else "informational (design §9)"
        md.append(f"### `{wl}`: **{doc['verdict']}** ({gate})\n")
        f = doc.get("faulting") or {}
        md.append("Faulting instructions (ELF symbol table) and the units that contain them:\n")
        for v, x in f.items():
            us = ", ".join(f"slot {u['slot']} pc {u['pc']} ({u['icount']} insn"
                           f"{', CF_NOIRQ' if int(u['cflags'], 16) & CF_NOIRQ else ''})"
                           for u in x["units"])
            md.append(f"- ({v}) `{x['symbol']}` = {x['elf_address']}: {us}")
        arts = doc.get("artifacts") or {}
        md.append("\n| artifact | faulting-unit tb_offset (delta) | V1-V8 | V9 | V6 per moved "
                  "unit | artifact |")
        md.append("|---|---|---|---|---|---|")
        for name in ("identity", "plus64", "plus128", "minus64c", "minusmax"):
            a = arts.get(name)
            if not a:
                continue
            pl = "; ".join(f"{r['old_tb_offset']}→{r['new_tb_offset']} ({r['delta']:+#x})"
                           for r in a["moved_units_v6"].values())
            v18 = "pass" if all(a["validation"].get(f"V{i}") == "pass" for i in range(1, 9)) \
                else str(a["validation"])
            v6 = "pass" if all(r["ok"] for r in a["moved_units_v6"].values()) else "FAIL"
            md.append(f"| {name} | {pl} | {v18} | {a['validation'].get('V9')} | {v6} | "
                      f"`{rel(a['artifact_dir'])}` |")
        dd = doc.get("minus64_direct") or {}
        if dd:
            md.append(f"\nDirect −64 MiB: feasible for {len(dd.get('feasible_units', []))} of "
                      f"{len(doc.get('faulting_units', []))} faulting units; the run moved "
                      f"{dd.get('moved')} units (`move-units` outcome "
                      f"{[p.get('reason') for p in dd.get('pipeline') or []]}).")
        md.append("\n| arm | printed values (all reps identical) | = expected | stacked PC = "
                  "ELF | proof / appended | TB entered before each fault (diag) | evidence |")
        md.append("|---|---|---|---|---|---|---|")
        arms = doc.get("arms") or {}
        exp = reg_lines((FIX / "expected" / f"{image}.norm").read_text().splitlines())
        syms = elf_symbols(FIX / f"fault_{image}.elf")
        want = {VARIANT_SYMS[v][1]: syms[VARIANT_SYMS[v][0]] & ~1 for v in IMAGES[image]
                if VARIANT_SYMS[v][1]}
        for arm in ARMS:
            x = arms.get(arm)
            if not x:
                continue
            reps = fault_reports(x["regs"])
            pc_ok = {r["variant"]: r["pc"] for r in reps} == want
            ft = (doc.get("fault_tbs") or {}).get(arm)
            ftxt = "; ".join(f"{t['tc']} {t['key']}" for t in ft) if ft else (
                "n/a (no diag for nocache)" if arm == "nocache" else "no fault")
            ev = f"`{rel(Path(x['run_dirs'][0]).parent)}/`"
            if arm != "nocache" and (doc.get("diag") or {}).get(arm):
                ev += f", `{rel(doc['diag'][arm]['qemu_log'])}`"
            pv = ", ".join(x["verdicts"]) if arm != "nocache" else "NOT_PROVED"
            md.append(f"| {arm} | {x['values']} | {'yes' if x['regs'] == exp else 'NO'} | "
                      f"{('yes' if pc_ok else 'NO') if want else 'n/a (no fault)'} | {pv} / "
                      f"{x['appended']} | {ftxt} | {ev} |")
        nq = doc.get("noirq") or {}
        for v, x in nq.items():
            arms_s = ", ".join(f"{a}: {y['count']}× at {','.join(y['tc'])}"
                               for a, y in x["arms"].items())
            md.append(f"\n({v}) one-insn CF_NOIRQ TB `{x['key']}`: {arms_s}.")
        neg = doc.get("negative_control")
        if neg:
            md.append(f"\nNegative control (`{rel(neg['tampered_sc'])}`): search row of insn "
                      f"{neg['insn']} of `{neg['unit']}` shifted by −2 → static V6 mismatches "
                      f"{neg['static_v6_mismatches']}; run proof {neg['verdict']}, appended "
                      f"{neg['appended']}, printed **{neg['printed']}**.")
        if doc["failed"]:
            md.append("\nFailed checks: " + "; ".join(doc["failed"]))
        md.append(f"\nChecks: {sum(c['ok'] for c in doc['checks'])}/{len(doc['checks'])} pass "
                  f"(`{rel(out / wl / 'fault.json')}`).\n")
    return "\n".join(md) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--variants", default="a,b,c,d,fixture")
    ap.add_argument("--modes", default="noicount")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--qemu", type=Path, default=INT_QEMU)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--render", action="store_true",
                    help="only (re)write OUT/tables.md from an existing run")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    if a.render:
        (out / "tables.md").write_text(render_tables(out))
        print(out / "tables.md")
        return 0
    if out.exists() and any(out.iterdir()):
        ap.error(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    qemu = a.qemu.resolve()
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain"], capture_output=True,
                           text=True).stdout.strip()
    doc: dict[str, Any] = {
        "schema": "scopt.b2_fault/1", "out": str(out), "qemu": str(qemu),
        "qemu_sha256": sha256_file(qemu), "scopt_worktree": str(WT), "scopt_head": head,
        "scopt_dirty": bool(dirty), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fixture_elf_sha256": {p.name: sha256_file(p) for p in sorted(FIX.glob("fault_*.elf"))},
        "reps": a.reps, "arms": ARMS, "workloads": {}}
    t0 = time.monotonic()
    for mode in a.modes.split(","):
        for v in a.variants.split(","):
            w = Workload(out, mode, v, qemu, a.reps, a.timeout).run()
            doc["workloads"][f"{mode}/{v}"] = {"verdict": w["verdict"], "failed": w["failed"],
                                               "fault_json": str(out / mode / v / "fault.json")}
    # design §9: CF_USE_ICOUNT units are excluded from the F-B2 gates until qualified, so
    # the gate verdict covers mode noicount; icount runs are reported (informational).
    gated = {k: w for k, w in doc["workloads"].items() if k.startswith("noicount/")}
    doc["gated_workloads"] = sorted(gated)
    doc["verdict"] = "PASS" if gated and all(w["verdict"] == "pass" for w in gated.values()) \
        else "FAIL"
    doc["verdict_all_modes"] = "PASS" if all(w["verdict"] == "pass" for w in
                                             doc["workloads"].values()) else "FAIL"
    doc["seconds"] = round(time.monotonic() - t0, 1)
    doc["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (out / "b2-fault.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: doc[k] for k in ("verdict", "seconds")}))
    return 0 if doc["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
