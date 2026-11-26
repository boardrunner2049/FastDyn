#!/usr/bin/env python3
"""WP22 integration stage: WP18-written v6 artifacts on WP24 fixtures and the
T-B3-26 loader stress test (design §5.5-5.6, §9.6; plan ``## WP22``).

    wp22_stress.py --qemu QEMU --out OUT [--only CASE ...] [--timeout S]

Every QEMU run uses a fresh run directory made by the scopt harness
(``deploy_new``: FastDyn oracle, fingerprint, sparse copy), ``setarch x86_64
-R``, a timeout and ``supercache_flat=false``; ``QEMU_SC_V6_STATS`` is set in
every run. Nothing is timed. gdb always runs with ``-nx`` and with cwd
outside any QEMU tree.

1. **Base snapshots** on the given binary (``runtime_signature`` binds a
   cache to the exe): for each WP24 config used below, the template is
   rendered for that binary (icount arm, ``fixtures/lib/fdrun.py`` render),
   then ``scopt prepare`` (capture on, the fixture input), ``bundle identity``,
   ``bundle runtime-identity``, ``import`` and ``admission identity``
   (RuntimeIdentity v2 with the EnvLayout of the exe).
2. **Artifacts** written by WP18 (``scopt.validate.package_b3``, NativeLinker
   v2 + ``sc_semantic`` writer, V1-V23 static rows; ``allow_uncertified=True``
   because the variants are identity clones and no WP19 certification input
   exists for this loader test; ``semantic_exes`` = the binary). Links are chosen
   from the fixture's symbols (``b3.nm``/``b3.lst``): R = the TB holding the
   callee's return, C = the TB at the return address Vc, the variant C' = a
   relocated clone of C without patches (so every run must equal the
   fixture's frozen icount oracle).
3. **Plain warm runs** of every artifact (``launch``: post-run proof,
   appended records, fixture oracle ``expected.py`` in icount, loader stats).
4. **Stress scenarios** under ``gdb -nx`` on the QEMU process
   (``wp22_stress_gdb.py``: variant-entry hardware breakpoints, loader
   events with slot state before/after), some with a guest debugger on the
   gdbstub (``arm-none-eabi-gdb -nx``; WP24 scenarios ``diag/f-b3-*.gdb``
   unchanged, WP22 ones in ``tests/qemu/wp22gdb/``).

Each scenario checks: QEMU exit 0, guest results, the slot state at every
loader event (after a TLB-flush disarm or a forbid disarm: every guarded slot
of a live valid R at its reset address with ``jmp_dest == 0``, every built
variant's registered slots unlinked, no link armed; after ``tb_flush``: no
record live, no link armed), no variant entry after the event in the same
epoch, ``invariant_violations == 0``, and the loader's stats counters equal
the counts derived from the observed events. Writes ``OUT/wp22-stress.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
sys.path.insert(0, str(WT))
ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FIX = ROOT / "fixtures/b3"
for p in (FIX, FIX / "diag", ROOT / "fixtures/lib"):
    sys.path.insert(0, str(p))

import expected as EXP  # noqa: E402  (fixtures/b3/expected.py)
import fdrun  # noqa: E402  (fixtures/lib/fdrun.py)
import gdb_guest as GG  # noqa: E402  (fixtures/b3/diag/gdb_guest.py)

from scopt.run.deploy import deploy_new  # noqa: E402
from scopt.run.launcher import (FASTDYN_SRC, file_sha256, launch, load_inputs,  # noqa: E402
                                reset_ram, scrub_env)

PY = sys.executable
GDB_HOST_SCRIPT = HERE / "wp22_stress_gdb.py"
WP22GDB = HERE / "wp22gdb"
GUEST_RE = re.compile(r"^(Current CPU type|\[[0-9]+\]\[[0-9.]+\] : |Reg [0-9]+:|\[BENCH\]|"
                      r"qemu-system-arm:)")
GATE_ENVLAYOUT_DIR = ROOT / "runs/wp18/envlayout"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def sh(cmd: list[Any], logf: Path, *, env: dict[str, str] | None = None) -> int:
    with open(logf, "w") as f:
        return subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT,
                              cwd=str(WT), env=env).returncode


# ---------------------------------------------------------------------------
# fixture symbols
# ---------------------------------------------------------------------------

def nm() -> dict[str, int]:
    out = {}
    for line in (FIX / "b3.nm").read_text().splitlines():
        p = line.split()
        if len(p) == 3:
            out[p[2]] = int(p[0], 16)
    return out


def call_returns(func: str, callee: str) -> list[int]:
    """Return addresses (insn after ``bl <callee>``) inside ``func`` (b3.lst): the
    instructions from ``func``'s address up to the next ``phase_*`` symbol (local
    labels such as ``vc_*`` sit inside a phase)."""
    syms = nm()
    lo = syms[func]
    hi = min((a for k, a in syms.items() if k.startswith("phase_") and a > lo), default=1 << 32)
    out = []
    for ln in (FIX / "b3.lst").read_text().splitlines():
        m = re.match(r"^\s+([0-9a-f]+):\s+(?:[0-9a-f]{4} ?)+\s+bl\s+[0-9a-f]+ <([^>]+)>", ln)
        if m and lo <= int(m.group(1), 16) < hi and m.group(2) == callee:
            out.append(int(m.group(1), 16) + 4)
    return out


def _func_lines(func: str) -> list[tuple[int, str, str]]:
    """(address, mnemonic, operands) of the instructions of ``func`` (b3.lst),
    from ``func``'s address up to the next ``phase_*``/global symbol in b3.nm
    that is not a local label inside it (``vc_*``, ``b3_14_*``)."""
    syms = nm()
    lo = syms[func]
    hi = min((a for k, a in syms.items() if a > lo and not k.startswith(("vc_", "b3_14_"))),
             default=1 << 32)
    out = []
    for ln in (FIX / "b3.lst").read_text().splitlines():
        m = re.match(r"^\s+([0-9a-f]+):\s+(?:[0-9a-f]{4} ?)+\s+(\S+)\s*(.*)$", ln)
        if m and lo <= int(m.group(1), 16) < hi:
            out.append((int(m.group(1), 16), m.group(2), m.group(3)))
    return out


def call_sites(func: str, callee: str) -> list[int]:
    """Addresses of the ``mov`` immediately before each ``bl <callee>`` inside
    ``func``: the start of a call block ``mov r0, r4; bl <callee>`` (the
    caller checks below that this address is a captured TB with a lookup tail)."""
    ls = _func_lines(func)
    out = []
    for i, (a, mn, ops) in enumerate(ls):
        if mn == "bl" and ops.endswith(f"<{callee}>") and i and ls[i - 1][1] == "mov":
            out.append(ls[i - 1][0])
    return out


def call_block_starts(func: str) -> list[int]:
    """Addresses of the instructions of ``func`` that follow a conditional
    branch (fall-through block starts)."""
    ls = _func_lines(func)
    return [ls[i + 1][0] for i, (a, mn, _) in enumerate(ls[:-1])
            if mn.startswith("b") and mn not in ("bl", "bx", "b", "b.n", "b.w", "blx")]


def insn_addrs(func: str, mnemonic: str) -> list[int]:
    """Addresses of the ``mnemonic`` instructions inside ``func`` (b3.lst): the
    instructions from ``func``'s address up to the next symbol in b3.nm."""
    syms = nm()
    lo = syms[func]
    hi = min((a for a in syms.values() if a > lo), default=1 << 32)
    out = []
    for ln in (FIX / "b3.lst").read_text().splitlines():
        m = re.match(r"^\s+([0-9a-f]+):\s+(?:[0-9a-f]{4} ?)+\s+(\S+)", ln)
        if m and lo <= int(m.group(1), 16) < hi and m.group(2) == mnemonic:
            out.append(int(m.group(1), 16))
    return out


# ---------------------------------------------------------------------------
# base snapshots
# ---------------------------------------------------------------------------

@dataclass
class Base:
    cfg: str
    toml: Path
    inp: Path
    snap: Any
    ri: dict[str, Any]
    info: dict[str, Any]


class Bases:
    def __init__(self, qemu: Path, out: Path):
        self.qemu = qemu
        self.out = out / "base"
        self.exe_sha = file_sha256(qemu).split(":")[-1]
        self.env = dict(os.environ)
        self.env["SCOPT_ENVLAYOUT_PATH"] = self._envlayout_dir()
        os.environ["SCOPT_ENVLAYOUT_PATH"] = self.env["SCOPT_ENVLAYOUT_PATH"]
        self.cache: dict[str, Base] = {}

    def _envlayout_dir(self) -> str:
        if (GATE_ENVLAYOUT_DIR / f"{self.exe_sha}.json").is_file():
            return str(GATE_ENVLAYOUT_DIR)
        d = self.out.parent / "envlayout"
        p = d / f"{self.exe_sha}.json"
        if not p.is_file():
            from scopt.analysis.envlayout import produce_envlayout

            d.mkdir(parents=True, exist_ok=True)
            lay = produce_envlayout(self.qemu, exe_sha256="sha256:" + self.exe_sha)
            p.write_bytes(lay.canonical_bytes())
        return str(d)

    def get(self, cfg: str) -> Base:
        if cfg in self.cache:
            return self.cache[cfg]
        o = self.out / cfg
        o.mkdir(parents=True, exist_ok=True)
        text, _ = fdrun.render(str(FIX / f"b3-{cfg}.toml.in"), "sc", f"b3-{cfg}.sc",
                               str(self.qemu), "none")
        toml = o / f"b3-{cfg}.toml"
        toml.write_text(text)
        inp = FIX / "inputs" / f"{cfg}.bin"
        prep = o / "prep"
        if not (prep / "prep.json").is_file():
            rc = sh([PY, "-m", "scopt", "prepare", "-c", toml, "--out", prep, "--inputs", inp,
                     "--qemu", self.qemu, "--timeout", 300], o / "prepare.log", env=self.env)
            pj = json.loads((prep / "prep.json").read_text())
            if rc or pj.get("verdict") != "PASS":
                raise SystemExit(f"{cfg}: prepare failed rc={rc} {pj.get('failures')}")
        pj = json.loads((prep / "prep.json").read_text())
        rc = sh([PY, "-m", "scopt", "bundle", "identity", "-c", prep / "run.toml", "--manifest",
                 pj["manifest"], "--out", o / "bundle.json"], o / "bundle.log", env=self.env)
        rc |= sh([PY, "-m", "scopt", "bundle", "runtime-identity", "--run-dir", prep / "oracle",
                  "--sc", pj["sc"], "--scm", prep / "capture.scm", "--out",
                  o / "runtime_identity.json"], o / "ri.log", env=self.env)
        with open(o / "import.json", "w") as f:
            rc |= subprocess.run([PY, "-m", "scopt", "import", str(prep), "--bundle",
                                  str(o / "bundle.json"), "--runtime-identity",
                                  str(o / "runtime_identity.json"), "--store", str(o / "store"),
                                  "--json"], stdout=f, stderr=subprocess.DEVNULL, cwd=str(WT),
                                 env=self.env).returncode
        if rc:
            raise SystemExit(f"{cfg}: bundle/import failed")
        s = json.loads((o / "import.json").read_text())
        from scopt.importer.import_cache import open_snapshot
        from scopt.model.cache_snapshot import snapshot_dir_name

        sdir = o / "store/snapshots" / snapshot_dir_name(s["snapshot_id"])
        rc = sh([PY, "-m", "scopt", "admission", "identity", "--snapshot", sdir, "--config", toml,
                 "--out", o / "runtime_identity.v2.json"], o / "admission.log", env=self.env)
        if rc:
            raise SystemExit(f"{cfg}: admission identity failed")
        ri = json.loads((o / "runtime_identity.v2.json").read_text())
        snap = open_snapshot(sdir)
        guest = (prep / "guest.norm").read_text() if (prep / "guest.norm").is_file() else ""
        b = Base(cfg, toml, inp, snap, ri,
                 {"snapshot_id": s["snapshot_id"], "status": s["status"], "sc": pj["sc"],
                  "records": len(list(snap.units())), "prep_guest_norm_lines": len(guest.split("\n"))})
        self.cache[cfg] = b
        return b


# ---------------------------------------------------------------------------
# artifacts (WP18 writer)
# ---------------------------------------------------------------------------

@dataclass
class Grow:
    r: int                                  # a guest vaddr inside R
    targets: tuple[tuple[str, int], ...]    # (kind, Vc)


@dataclass
class ArtSpec:
    name: str
    cfg: str
    grows: tuple[Grow, ...] = ()
    identity: bool = False
    notes: str = ""


@dataclass
class Artifact:
    spec: ArtSpec
    base: Base
    dir: Path | None
    status: str
    reason: str | None
    layout: dict[str, Any] = field(default_factory=dict)


def unit_at(s: Any, vaddr: int) -> Any:
    hit = [u for u in s.units() if any((v & ~1) == vaddr for v in u.vaddrs)]
    if len(hit) != 1:
        raise SystemExit(f"{len(hit)} units at {vaddr:#x}")
    return hit[0]


def build_artifact(spec: ArtSpec, base: Base, out: Path, exe_sha: str) -> Artifact:
    from scopt.guard import CS_CONDEXEC_MASK, CS_THUMB, PSTATE_SS, GuardTarget
    from scopt.link import B3Plan, GuardedExitSpec, VariantSpec, capture_join, exit_class_of, \
        free_slots, lookup_tails, tail_problem
    from scopt.validate import package_b3

    s = base.snap
    jr = capture_join(s)
    grow: dict[str, tuple] = {}
    variants = []
    notes = []
    for g in spec.grows:
        R = unit_at(s, g.r)
        tails = lookup_tails(R)
        if len(tails) != 1 or tail_problem(R, tails[0], s.header.code_address):
            raise SystemExit(f"{spec.name}: R {g.r:#x} has no single admissible tail")
        t = tails[0]
        gbytes = jr.slots[R.slot].chunk.guest_bytes
        xc = exit_class_of(R, t, gbytes)
        free = list(free_slots(R))
        tgs = []
        cunits = []
        for (kind, vc), n in zip(g.targets, free):
            C = unit_at(s, vc)
            cunits.append(C)
            tgs.append(GuardTarget(vc, n, kind))
            if kind == "SPEC":
                variants.append(VariantSpec(C.uid, R.uid, n))
            notes.append({"r": hex(g.r), "r_uid": R.uid, "slot_n": n, "kind": kind, "vc": hex(vc),
                          "c_uid": C.uid, "exit_class": xc})
        c0 = cunits[0]
        grow[R.uid] = (GuardedExitSpec(t.off, xc if xc in ("return", "static") else "static",
                                       tuple(tgs), c0.key.flags & ~PSTATE_SS,
                                       (c0.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB),)
    plan = B3Plan(grow=grow, variants=tuple(variants), force_semantic_table=spec.identity)
    store = out / "artifacts" / spec.name
    if store.exists():
        from scopt.formats.artifact import remove_tree

        remove_tree(store)
    cid = "sha256:" + __import__("hashlib").sha256(spec.name.encode()).hexdigest()
    r = package_b3(s, plan, store, candidate_id=cid, runtime=base.ri, allow_uncertified=True,
                   semantic_exes=[exe_sha])
    art = Artifact(spec, base, Path(r.artifact_dir) if r.artifact_dir else None, r.status,
                   r.reason)
    art.layout = {"links": notes, "status": r.status, "reason": r.reason,
                  "fails": None if r.validation is None else
                  [x for x in r.validation.rows if x["result"] == "fail"],
                  "uncertified": None}
    if r.status != "published":
        return art
    from scopt.formats.sc_semantic import read_semantic_cache

    sc = read_semantic_cache(art.dir / "cache.sc")
    ca = sc.sc.header.code_address
    ext = sc.ext
    side = json.loads((art.dir / "sidecar.json").read_text()) if (art.dir / "sidecar.json").is_file() \
        else {}
    art.layout.update(
        version=sc.version, semantic_table=sc.has_ext, code_address=ca,
        uncertified=side.get("x_uncertified"),
        variants=[{"id": i, "tc": ca + v.tc_offset, "tb": ca + v.tb_offset,
                   "general_tb": ca + v.general_tb_offset, "tc_size": v.tc_size}
                  for i, v in enumerate(ext.variants)] if ext else [],
        slots=[{"i": i, "src_tb": ca + x.src_tb_offset, "n": x.slot_n, "kind": x.kind,
                "vc": hex(x.target_vaddr), "guard_off": x.guard_off}
               for i, x in enumerate(ext.slots)] if ext else [],
        links=[{"slot": lk.slot_index, "variant": lk.variant_index} for lk in ext.links]
        if ext else [],
        notes=notes)
    return art


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def fixture_norm(text: str, rc: int | None) -> str:
    out = [re.sub(r"^(\[BENCH\]\[[^]]*\]) elapsed_ns=.*", r"\1 elapsed=<host-time>", ln)
           for ln in text.splitlines() if GUEST_RE.match(ln)]
    return "\n".join(out + [f"exit_code={rc}"]) + "\n"


def plain_run(art: Artifact, out: Path, name: str, qemu: Path, timeout: float) -> dict[str, Any]:
    rd, _ = deploy_new(art.dir, art.base.toml, out / "runs" / name, qemu=qemu)
    stats = rd.root / "v6-stats.json"
    r = launch(rd, timeout_s=timeout, inputs={"input": str(art.base.inp)},
               extra_env={"QEMU_SC_V6_STATS": str(stats)})
    text = Path(r["stdout_log"]).read_text(errors="replace")
    norm = fixture_norm(text, r["exit_code"])
    (rd.root / "guest.fixture.norm").write_text(norm)
    fails = EXP.compare(norm, art.base.cfg, "icount")
    return {"run_dir": str(rd.root), "exit_code": r["exit_code"], "timed_out": r["timed_out"],
            "qemu_sha256": r.get("qemu_sha256"), "proof": (r.get("proof") or {}).get("verdict"),
            "appended": (r.get("proof") or {}).get("appended"), "oracle_failures": fails,
            "stats": json.loads(stats.read_text()) if stats.is_file() else None,
            "v6_log": [ln for ln in text.splitlines() if "supercache" in ln and
                       ("v6" in ln or "invalid" in ln)][:8]}


def gdb_run(art: Artifact, out: Path, name: str, qemu: Path, *, actions=(), guest=None,
            sigint_after: float | None = None, inp: Path | None = None, hmp: bool = False,
            timeout: int = 600) -> dict[str, Any]:
    rd, _ = deploy_new(art.dir, art.base.toml, out / "runs" / name, qemu=qemu)
    reset_ram(rd)
    load_inputs(rd, {"input": str(inp or art.base.inp)})
    qa = json.loads((rd.root / "qemu_argv.json").read_text())
    argv = list(qa["argv"]) + ["-name", "wp22,debug-threads=on"]
    if Path(argv[0]).resolve() != qemu.resolve():
        raise SystemExit(f"oracle argv uses {argv[0]}, not {qemu}")
    mon = Path(f"/tmp/wp22i-{os.getpid()}-{name}.sock")
    if hmp:
        argv += ["-monitor", f"unix:{mon},server=on,wait=off"]
    port = None
    if guest:
        port = GG.free_port()
        argv += ["-S", "-gdb", f"tcp:127.0.0.1:{port}"]
    params = {"result": str(rd.root / "gdb-host.json"), "monitor": str(mon),
              "variants": [{"id": v["id"], "tc": v["tc"]} for v in art.layout["variants"]],
              "actions": [dict(a) for a in actions]}
    env, _ = scrub_env(qemu=qemu, extra={**qa.get("env_set", {}),
                                         "QEMU_SC_V6_STATS": str(rd.root / "v6-stats.json"),
                                         "WP22_GDB_PARAMS": json.dumps(params)})
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10", str(timeout),
           "gdb", "-nx", "-batch", "-x", str(GDB_HOST_SCRIPT), "--args", *argv]
    (rd.root / "cmd.txt").write_text(" ".join(cmd) + "\n")
    t0 = time.monotonic()
    hlog = open(rd.root / "gdb-host.log", "w")
    h = subprocess.Popen(cmd, cwd=str(FASTDYN_SRC), env=env, stdout=hlog,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    gres = None
    if guest:
        want_diag, want_res = GG.expectations(str(guest))
        gcmd = ["timeout", "--kill-after=10", str(timeout), GG.GDB, "-nx", "-batch",
                "-ex", "set confirm off", "-ex", "set pagination off"]
        # wait for the gdbstub (host gdb starts QEMU first)
        for _ in range(600):
            try:
                import socket

                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        gcmd += ["-ex", f"target remote 127.0.0.1:{port}", "-x", str(guest),
                 str(FIX / "b3.elf")]
        with open(rd.root / "gdb-guest.log", "wb") as glog:
            g = subprocess.Popen(gcmd, cwd=str(rd.root), stdout=glog, stderr=subprocess.STDOUT)
            if sigint_after is not None:
                time.sleep(sigint_after)
                g.send_signal(signal.SIGINT)
            grc = g.wait()
        gres = {"script": str(guest), "gdb_exit": grc, "want_diag": want_diag,
                "want_res": {hex(k): v for k, v in want_res.items()}}
    try:
        hrc = h.wait(timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        h.kill()
        hrc = h.wait()
    hlog.close()
    text = (rd.root / "gdb-host.log").read_text(errors="replace")
    host = json.loads(Path(params["result"]).read_text()) if Path(params["result"]).is_file() \
        else {}
    qrc = host.get("exit_code")
    norm = fixture_norm(text, qrc)
    (rd.root / "guest.fixture.norm").write_text(norm)
    st_p = rd.root / "v6-stats.json"
    res = {"run_dir": str(rd.root), "host_gdb_exit": hrc, "qemu_exit": qrc,
           "seconds": round(time.monotonic() - t0, 1), "host": host,
           "stats": json.loads(st_p.read_text()) if st_p.is_file() else None,
           "guest_norm": norm, "pairs": {hex(k): v for k, v in
                                         GG.guest_pairs(norm.splitlines()).items()}}
    if guest:
        diag = {}
        for ln in (rd.root / "gdb-guest.log").read_text(errors="replace").splitlines():
            m = re.match(r"B3DIAG (\w+)=(\S+)", ln)
            if m:
                diag.setdefault(m.group(1), []).append(m.group(2))
        checks = GG.evaluate(diag, GG.guest_pairs(norm.splitlines()), qrc,
                             gres["want_diag"], {int(k, 16): v for k, v in
                                                 gres["want_res"].items()})
        gres.update(diag=diag, checks=checks, ok=all(c["ok"] for c in checks))
        res["guest"] = gres
    return res


# ---------------------------------------------------------------------------
# checks on the host-gdb observations
# ---------------------------------------------------------------------------

def _armed(st: dict[str, Any]) -> int:
    return sum(1 for lk in st.get("links", []) if lk["armed"])


def _disarmed_ok(st: dict[str, Any]) -> list[str]:
    """§5.6 after a disarm (full state): every guarded slot of a live valid R reset
    and unlinked, every built variant's slots unlinked, no link armed."""
    bad = []
    for s in st.get("slots", []):
        if s.get("R") and not s.get("R_invalid") and ((s.get("dest") or 0) & ~1
                                                       or not s.get("at_reset")):
            bad.append(f"slot {s['i']} dest={s.get('dest')} at_reset={s.get('at_reset')}")
    for v in st.get("variants", []):
        vs = v.get("state")
        if vs and not vs["invalid"]:
            for k, sl in enumerate(vs["slots"]):
                if sl and (sl["dest"] & ~1):
                    bad.append(f"variant {v['id']} slot {k} linked dest={sl['dest']:#x}")
    if _armed(st):
        bad.append(f"{_armed(st)} links armed")
    return bad


def _linked_variant_slots(st):
    return sum(1 for v in st.get("variants", []) if v.get("state") and not v["state"]["invalid"]
               for sl in v["state"]["slots"] if sl and (sl["dest"] & ~1))


def loader_events(run: dict[str, Any], name: str | None = None) -> list[dict[str, Any]]:
    return [e for e in run["host"].get("events", []) if "b" in e and
            (name is None or e["ev"] == name)]


def event_checks(run: dict[str, Any]) -> tuple[list[tuple[str, bool, Any]], dict[str, Any]]:
    """Slot state after every observed loader event + the loader's stats derived
    from the events (``b``/``a`` = the gdb-side summaries before/after the call)."""
    host = run["host"]
    st = run["stats"] or {}
    items: list[tuple[str, bool, Any]] = []
    evs = loader_events(run)
    derived = {"disarm_tlb_events": 0, "disarm_tlb_links": 0, "flush_resets": 0,
               "disarm_flush_links": 0, "slots_unlinked": 0, "variant_slots_unlinked": 0,
               "unlink_all": 0, "disarm_forbid_events": 0, "disarm_forbid_links": 0}
    counts: dict[str, int] = {}
    bad_after: dict[str, list[Any]] = {}
    missing_after = []
    for e in evs:
        b, a = e["b"], e.get("a")
        counts[e["ev"]] = counts.get(e["ev"], 0) + 1
        if a is None:
            missing_after.append((e["ev"], e["seg"], e.get("after_error")))
            continue
        if e["ev"] in ("tcg_supercache_mapping_changed", "tcg_supercache_v6_forbid",
                       "sc6_disarm_all_work"):
            if b.get("any_live"):
                pre = "disarm_tlb" if e["ev"] == "tcg_supercache_mapping_changed" \
                    else "disarm_forbid"
                derived[pre + "_events"] += 1
                derived[pre + "_links"] += b["armed"]
                derived["slots_unlinked"] += b["live_slots"]
                derived["variant_slots_unlinked"] += b["variant_slots"]
                derived["unlink_all"] += 1
                if a.get("disarm_bad"):
                    bad_after.setdefault(e["ev"], []).append((e["seg"], a["disarm_bad"]))
            if e["ev"] != "tcg_supercache_mapping_changed" and a.get("forbidden") is not True:
                bad_after.setdefault(e["ev"], []).append((e["seg"], "not forbidden"))
        elif e["ev"] == "tcg_supercache_runtime_reset":
            if b.get("loaded"):
                derived["flush_resets"] += 1
                derived["disarm_flush_links"] += b["armed"]
                if a.get("armed") or a.get("anything_live"):
                    bad_after.setdefault(e["ev"], []).append((e["seg"], a))
    items.append(("every loader event's after-state was read", not missing_after,
                  missing_after[:4]))
    items.append(("after every TLB-flush / forbid disarm: every guarded slot of a live R at its "
                  "reset path with jmp_dest 0, every built C' slot unlinked, no link armed; "
                  "after every tb_flush: nothing live, nothing armed",
                  not bad_after, {k: v[:3] for k, v in bad_after.items()}))
    items.append(("no gdb event record dropped", not host.get("events_dropped"),
                  host.get("events_dropped")))
    if st:
        for k in ("disarm_tlb_events", "disarm_tlb_links", "flush_resets", "disarm_flush_links",
                  "slots_unlinked", "variant_slots_unlinked", "disarm_forbid_events",
                  "disarm_forbid_links"):
            items.append((f"stats {k} == derived from the observed events",
                          st.get(k) == derived[k], (st.get(k), derived[k])))
        items.append(("stats invariant_checks == unlink_all calls (stats on)",
                      st.get("invariant_checks") == derived["unlink_all"],
                      (st.get("invariant_checks"), derived["unlink_all"])))
        items.append(("stats invariant_violations == 0", st.get("invariant_violations") == 0,
                      st.get("invariant_violations")))
    summary = [{"ev": e["ev"], "seg": e["seg"], "thread": e.get("thread"),
                "b": e["b"], "a": e.get("a")} for e in evs[:40]]
    return items, {"counts": counts, "derived": derived, "first_events": summary,
                   "rereports": host.get("rereports", 0)}


def hits_after(run: dict[str, Any], seg_from: int) -> int:
    n = 0
    for v in run["host"].get("variant_hits", {}).values():
        for s, h in v["segments"].items():
            if int(s) >= seg_from:
                n += h["hits"]
    return n


def hits_total(run: dict[str, Any], vid: int | None = None) -> int:
    return sum(v["total"] for k, v in run["host"].get("variant_hits", {}).items()
               if vid is None or int(k) == vid)


def first_event_seg(run: dict[str, Any], name: str, *, need_live: bool = False,
                    need_armed: bool = False) -> int | None:
    for e in loader_events(run, name):
        if (not need_live or e["b"].get("any_live")) and (not need_armed or e["b"].get("armed")):
            return e["seg"]
    return None


def variant_linked(st: dict[str, Any], vid: int) -> bool:
    v = st["variants"][vid]
    vs = v.get("state")
    return bool(vs) and not vs["invalid"] and any(sl and (sl["dest"] & ~1) for sl in vs["slots"])


def variant_unlinked(st: dict[str, Any], vid: int) -> bool:
    v = st["variants"][vid]
    vs = v.get("state")
    return bool(vs) and all(not sl or not (sl["dest"] & ~1) for sl in vs["slots"])


def verdict(items: list[tuple[str, bool, Any]]) -> str:
    return "PASS" if items and all(ok for _, ok, _ in items) else "FAIL"


def pack(items: list[tuple[str, bool, Any]]) -> list[dict[str, Any]]:
    return [{"check": c, "ok": bool(ok), "detail": d} for c, ok, d in items]


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

def art_specs() -> list[ArtSpec]:
    S = nm()
    res14 = call_returns("phase_b3_14", "b3_res")
    assert len(res14) == 2, res14
    # Since WP24 review 3 b3_res masks interrupts around log_pair (push; mrs
    # PRIMASK; cpsid i | orr; bl log_pair | msr PRIMASK | pop {r4, pc}): log_pair
    # returns to the `msr PRIMASK` TB, which ends with exit_tb (no lookup tail:
    # it can be a C but neither an R nor a grown C), and the `pop` is a TB of its
    # own, reached through the main loop.  The return sites in phase_b3_14 each
    # run once, so a link into them arms only after their single execution.
    # Hence (a) b3-14 keeps the earlier shape where it still exists (log_pair's
    # return -> SPEC -> b3_res; b3_res's pop grown with two GENERAL slots to the
    # return sites) and (b) the variant of a grown C moves to the hot loop of
    # the phase in b3-14g.
    # log_pair's store-and-return block: the fall-through after its bounds check
    lp_ret = max(a for a in call_block_starts("log_pair")
                 if a <= insn_addrs("log_pair", "bx")[0])
    msr = insn_addrs("b3_res", "msr")
    pop = insn_addrs("b3_res", "pop")
    assert len(msr) == 1 and len(pop) == 1, (msr, pop)
    call14 = call_sites("phase_b3_14", "f_b3_14")
    assert len(call14) == 1, call14
    return [
        ArtSpec("b3-1", "1", (Grow(S["f_b3_1"], (("SPEC", S["vc_b3_1"]),)),),
                notes="F-B3-1: R = f_b3_1 (adds; bx lr), C = vc_b3_1 (cbz)"),
        ArtSpec("b3-1-general", "1", (Grow(S["f_b3_1"], (("GENERAL", S["vc_b3_1"]),)),),
                notes="F-B3-1 with a GENERAL slot (main loop chains R|n)"),
        ArtSpec("b3-1-identity", "1", identity=True, notes="identity-semantic written by WP18"),
        ArtSpec("b3-6r", "6r", (
            Grow(0x10078, (("SPEC", S["rec_b3_cont"]), ("SPEC", S["vc_b3_6r"]))),
            Grow(S["rec_b3_ret"], (("SPEC", S["rec_b3_cont"]),))),
            notes="F-B3-6 recursion: the z-path epilogue TB (0x10078..pop) returns to "
                  "rec_b3_cont (inner) or vc_b3_6r (top); the base-case pop TB returns to "
                  "rec_b3_cont; rec_b3_cont has two variants (one per R)"),
        ArtSpec("b3-12", "12", (Grow(S["f_b3_12"], (("SPEC", S["vc_b3_12"]),)),)),
        ArtSpec("b3-13", "13", (Grow(S["f_b3_13"], (("SPEC", S["vc_b3_13"]),)),)),
        ArtSpec("b3-14", "14", (
            Grow(S["f_b3_14"], (("SPEC", S["vc_b3_14"]),)),
            Grow(lp_ret, (("SPEC", msr[0]),)),
            Grow(pop[0], (("GENERAL", res14[0]), ("GENERAL", res14[1])))),
            notes="F-B3-14: f_b3_14 -> SPEC -> vc_b3_14; log_pair's return TB -> SPEC -> "
                  "b3_res's `msr PRIMASK` TB (exit_tb; run twice per phase, so C' is "
                  "entered on the second b3_res call); b3_res's pop TB grown with two "
                  "GENERAL slots to the phase's b3_res return sites"),
        ArtSpec("b3-14g", "14", (
            Grow(call14[0], (("SPEC", S["f_b3_14"]),)),
            Grow(S["f_b3_14"], (("GENERAL", S["vc_b3_14"]),))),
            notes="F-B3-14 with a variant of a grown C on the hot loop: the call TB "
                  "(`mov r0, r4; bl f_b3_14`, cross-page, lookup tail) -> SPEC -> f_b3_14, "
                  "itself grown with a GENERAL slot to vc_b3_14; C' carries a cloned "
                  "guarded stub whose GENERAL slot the main loop chains, and the guest "
                  "MPU write at iteration 500 flushes the TLB while it is linked"),
    ]


def run_all(qemu: Path, out: Path, only: list[str], timeout: float) -> dict[str, Any]:
    bases = Bases(qemu, out)
    exe_sha = bases.exe_sha
    doc: dict[str, Any] = {"schema": "wp22.stress/1", "qemu": str(qemu),
                           "qemu_sha256": file_sha256(qemu), "started": time.strftime(
                               "%Y-%m-%dT%H:%M:%S%z"), "envlayout_dir": bases.env[
                               "SCOPT_ENVLAYOUT_PATH"], "artifacts": {}, "scenarios": {}}
    arts: dict[str, Artifact] = {}
    for spec in art_specs():
        log(f"base {spec.cfg} / artifact {spec.name}")
        b = bases.get(spec.cfg)
        a = build_artifact(spec, b, out, exe_sha)
        arts[spec.name] = a
        doc["artifacts"][spec.name] = {"cfg": spec.cfg, "notes": spec.notes, "base": b.info,
                                       "dir": str(a.dir) if a.dir else None, **a.layout}
        log(f"  {spec.name}: {a.status} {a.reason or ''} version={a.layout.get('version')} "
            f"variants={len(a.layout.get('variants', []))} slots={len(a.layout.get('slots', []))}")
    (out / "wp22-stress.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")

    def want(name: str) -> bool:
        return not only or name in only

    def record(name: str, items, run, extra=None):
        v = verdict(items)
        doc["scenarios"][name] = {"verdict": v, "checks": pack(items),
                                  "run": {k: x for k, x in run.items() if k != "host"}
                                  if run else None, **(extra or {})}
        log(f"[{v}] {name}" + "".join(f"\n    FAIL {c}: {str(d)[:300]}" for c, ok, d in items
                                      if not ok))
        (out / "wp22-stress.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")

    published = {k: a for k, a in arts.items() if a.status == "published"}
    for k, a in arts.items():
        items = [("package_b3 published (WP18 writer, static V-rows)", a.status == "published",
                  a.reason)]
        if a.status == "published":
            items.append(("semantic table in SuperCache", a.layout.get("semantic_table") is True,
                          a.layout.get("version")))
        record(f"package:{k}", items, None)

    # -- 1. plain warm runs ------------------------------------------------------
    for k, a in published.items():
        name = f"plain:{k}"
        if not want(name):
            continue
        r = plain_run(a, out, name.replace(":", "-"), qemu, timeout)
        st = r["stats"] or {}
        nl = len(a.layout["links"])
        items = [("exit 0", r["exit_code"] == 0, r["exit_code"]),
                 ("post-run proof PASS", r["proof"] == "PASS", r["proof"]),
                 ("appended 0", r["appended"] == 0, r["appended"]),
                 ("fixture oracle (icount .norm + invariants)", not r["oracle_failures"],
                  r["oracle_failures"][:4]),
                 ("semantic table loaded", st.get("loaded") is True and st.get("file_version") == a.layout["version"],
                  (st.get("loaded"), st.get("file_version"))),
                 ("no 'invalid v6 extension' / 'discarding v6 file'", not r["v6_log"],
                  r["v6_log"]),
                 ("invariant_violations == 0", st.get("invariant_violations") == 0,
                  st.get("invariant_violations"))]
        if nl:
            items.append((f"links armed >= {nl} (every link armed at least once)",
                          (st.get("links_armed") or 0) >= nl, st.get("links_armed")))
            items.append(("no refusal", sum((st.get("refused") or {}).values()) == 0,
                          st.get("refused")))
        record(name, items, r)

    # -- 2. arm (host gdb, no perturbation) ------------------------------------
    for k in ("b3-1", "b3-1-general", "b3-6r", "b3-12", "b3-13", "b3-14", "b3-14g"):
        name = f"arm:{k}"
        if k not in published or not want(name):
            continue
        a = published[k]
        r = gdb_run(a, out, name.replace(":", "-"), qemu)
        items, ev = event_checks(r)
        fails = EXP.compare(r["guest_norm"], a.base.cfg, "icount")
        items += [("QEMU exit 0 under gdb", r["qemu_exit"] == 0, r["qemu_exit"]),
                  ("fixture oracle (icount)", not fails, fails[:3])]
        st = r["stats"] or {}
        if k in ("b3-1", "b3-12", "b3-13"):
            items.append(("C' entered on every iteration after arming (999 of 1000)",
                          hits_total(r, 0) == 999, hits_total(r, 0)))
        if k in ("b3-1", "b3-6r", "b3-12", "b3-13", "b3-14", "b3-14g"):
            fe = r["host"].get("first_entry", {})
            okv, det = True, {}
            for lk in a.layout["links"]:
                vid, si = lk["variant"], lk["slot"]
                stv = fe.get(str(vid))
                if stv is None:
                    okv = False
                    det[vid] = "not entered"
                    continue
                s = stv["slots"][si]
                v = stv["variants"][vid]
                good = (s.get("target_variant") == vid and s.get("dest") == v.get("general_live")
                        and bool(v.get("general_live")) and v.get("built") == v["tc"] - 192)
                okv &= good
                det[vid] = {"slot": si, "target_variant": s.get("target_variant"),
                            "dest_is_C": s.get("dest") == v.get("general_live"),
                            "built_at_entry_tb": v.get("built") == v["tc"] - 192}
            items.append(("at the first entry of every C': R's SPEC slot n targets C'.tc.ptr, "
                          "jmp_dest[n] == the live general C, C' struct built at tb_offset",
                          okv, det))
        if k == "b3-1-general":
            items.append(("GENERAL only: no variant, stats loaded", st.get("n_variants") == 0
                          and st.get("n_slots") == 1, (st.get("n_variants"), st.get("n_slots"))))
        if k == "b3-6r":
            items.append(("every variant entered", all(
                v["total"] > 0 for v in r["host"]["variant_hits"].values()),
                {i: v["total"] for i, v in r["host"]["variant_hits"].items()}))
        if k == "b3-14":
            seg = first_event_seg(r, "tcg_supercache_mapping_changed", need_armed=True)
            vh = r["host"]["variant_hits"]
            # variant 0 = C' of vc_b3_14 (link order = plan order)
            v0 = vh.get("0", {"segments": {}})
            before = sum(h["hits"] for s, h in v0["segments"].items() if seg and int(s) < seg)
            last_r4 = max((h["last_r4"] for s, h in v0["segments"].items()
                           if seg and int(s) < seg), default=None)
            mpu = [e for e in loader_events(r, "tcg_supercache_mapping_changed")
                   if e["b"].get("armed")]
            items += [("guest MPU write flushed the TLB while the link was armed",
                       bool(mpu), ev["first_events"][:4]),
                      ("a variant slot C'|n was linked by the main loop at that flush",
                       bool(mpu) and mpu[0]["b"]["linked_variant_slots"] > 0,
                       mpu[0]["b"] if mpu else None),
                      ("C'(vc_b3_14) entered 499 times before the flush, last at iteration 499",
                       before == 499 and last_r4 == 499, (before, last_r4)),
                      ("no C'(vc_b3_14) entry after the MPU flush (epoch disarm)",
                       seg is not None and sum(h["hits"] for s, h in v0["segments"].items()
                                               if int(s) >= seg) == 0, v0["segments"]),
                      ("C' of b3_res's `msr PRIMASK` TB (log_pair's return) entered on the "
                       "second b3_res call", vh.get("1", {}).get("total", 0) >= 1, vh.get("1")),
                      ("stats disarm_tlb_events >= 1", (st.get("disarm_tlb_events") or 0) >= 1,
                       st.get("disarm_tlb_events"))]
        if k == "b3-14g":
            # variant 0 = C' of the grown f_b3_14 (cloned GENERAL slot to vc_b3_14)
            seg = first_event_seg(r, "tcg_supercache_mapping_changed", need_armed=True)
            v0 = r["host"]["variant_hits"].get("0", {"segments": {}})
            before = sum(h["hits"] for s, h in v0["segments"].items() if seg and int(s) < seg)
            last_r4 = max((h["last_r4"] for s, h in v0["segments"].items()
                           if seg and int(s) < seg), default=None)
            mpu = [e for e in loader_events(r, "tcg_supercache_mapping_changed")
                   if e["b"].get("armed") and "before" in e and "after" in e]
            items += [("guest MPU write flushed the TLB while the link was armed",
                       bool(mpu), ev["first_events"][:4]),
                      ("at that flush the cloned GENERAL slot C'|n of the grown C was linked "
                       "by the main loop, and the flush unlinked it",
                       bool(mpu) and variant_linked(mpu[0]["before"], 0)
                       and variant_unlinked(mpu[0]["after"], 0),
                       (mpu[0]["b"], mpu[0]["a"]) if mpu else None),
                      ("C'(f_b3_14) entered 499 times before the flush, last at iteration 499",
                       before == 499 and last_r4 == 499, (before, last_r4)),
                      ("no C'(f_b3_14) entry after the MPU flush (epoch disarm)",
                       seg is not None and sum(h["hits"] for s, h in v0["segments"].items()
                                               if int(s) >= seg) == 0, v0["segments"]),
                      ("stats disarm_tlb_events >= 1", (st.get("disarm_tlb_events") or 0) >= 1,
                       st.get("disarm_tlb_events"))]
        record(name, items, r, {"events": ev})

    # -- 3. tlb_flush with C'|n linked (6r) ---------------------------------------
    name = "tlb:b3-6r"
    if "b3-6r" in published and want(name):
        a = published["b3-6r"]
        r = gdb_run(a, out, name.replace(":", "-"), qemu,
                    actions=[{"variant": 0, "hit": 50, "do": "tlb_flush"}])
        items, ev = event_checks(r)
        fails = EXP.compare(r["guest_norm"], a.base.cfg, "icount")
        act = (r["host"].get("actions_done") or [{}])[0]
        b, af = act.get("before", {}), act.get("after", {})
        seg = first_event_seg(r, "tcg_supercache_mapping_changed", need_armed=True)
        items += [("QEMU exit 0", r["qemu_exit"] == 0, r["qemu_exit"]),
                  ("fixture oracle (icount)", not fails, fails[:3]),
                  ("action ran at variant 0 hit 50", act.get("at_hit") == 50, act.get("at_hit")),
                  ("links armed and a variant slot C'|n linked before the flush",
                   _armed(b) > 0 and _linked_variant_slots(b) > 0,
                   (_armed(b), _linked_variant_slots(b))),
                  ("after: all slots reset, variant slots unlinked, nothing armed",
                   not _disarmed_ok(af), _disarmed_ok(af)[:4]),
                  ("disarmed links stay off for the epoch (disarmed_epoch)",
                   all(lk["disarmed_epoch"] for lk in af.get("links", []) if lk),
                   af.get("links")),
                  ("no variant entry after the flush", seg is not None and hits_after(r, seg) == 0,
                   hits_after(r, seg or 0))]
        record(name, items, r, {"events": ev})

    # -- 4. one-insn-per-tb via HMP (b3-1) ----------------------------------------
    name = "hmp:b3-1"
    if "b3-1" in published and want(name):
        a = published["b3-1"]
        r = gdb_run(a, out, name.replace(":", "-"), qemu, hmp=True,
                    actions=[{"variant": 0, "hit": 100, "do": "hmp"}])
        items, ev = event_checks(r)
        fails = EXP.compare(r["guest_norm"], a.base.cfg, "icount")
        st = r["stats"] or {}
        act = (r["host"].get("actions_done") or [{}])[0]
        dis = loader_events(r, "sc6_disarm_all_work")
        vt = r["host"].get("vcpu_thread")
        items += [("QEMU exit 0", r["qemu_exit"] == 0, r["qemu_exit"]),
                  ("fixture oracle (icount)", not fails, fails[:3]),
                  ("setter ran on a non-vCPU thread", act.get("setter_thread") not in (None, vt),
                   (act.get("setter_thread"), vt)),
                  ("setter only queued: link still armed right after it",
                   _armed(act.get("after_setter", {})) == 1, _armed(act.get("after_setter", {}))),
                  ("disarm ran on the vCPU thread in the exclusive context",
                   len(dis) == 1 and dis[0]["thread"] == vt and dis[0].get("exclusive") == 1,
                   [(d["thread"], d.get("exclusive")) for d in dis]),
                  ("stats forbidden one_insn_per_tb, 1 forbid disarm of 1 link",
                   st.get("forbidden") is True and st.get("forbid_reason") == "one_insn_per_tb"
                   and st.get("disarm_forbid_events") == 1 and st.get("disarm_forbid_links") == 1,
                   {x: st.get(x) for x in ("forbidden", "forbid_reason", "disarm_forbid_events",
                                           "disarm_forbid_links")}),
                  ("no variant entry after the disarm", bool(dis) and
                   hits_after(r, dis[0]["seg"]) == 0, hits_after(r, dis[0]["seg"] if dis else 0))]
        record(name, items, r, {"events": ev})

    # -- 5. plugin reset (b3-1) ------------------------------------------------------
    name = "plugin-reset:b3-1"
    if "b3-1" in published and want(name):
        a = published["b3-1"]
        r = gdb_run(a, out, name.replace(":", "-"), qemu,
                    actions=[{"variant": 0, "hit": 100, "do": "plugin_reset"}])
        items, ev = event_checks(r)
        st = r["stats"] or {}
        rs = [e for e in loader_events(r, "tcg_supercache_runtime_reset") if e["b"].get("armed")]
        fb = loader_events(r, "tcg_supercache_v6_forbid")
        fails = EXP.compare(r["guest_norm"], a.base.cfg, "icount")
        items += [("QEMU exit 0", r["qemu_exit"] == 0, r["qemu_exit"]),
                  ("plugin_flush_destroy: tb_flush with the link armed, then the Q11 forbid",
                   bool(rs) and bool(fb) and fb[0]["seg"] > rs[0]["seg"],
                   ([e["seg"] for e in rs], [e["seg"] for e in fb])),
                  ("forbid ran in the exclusive context on the vCPU thread",
                   bool(fb) and fb[0]["thread"] == r["host"].get("vcpu_thread"),
                   [e["thread"] for e in fb]),
                  ("stats forbidden plugin_reset; armed only in the first epoch",
                   st.get("forbidden") is True and st.get("forbid_reason") == "plugin_reset"
                   and st.get("links_armed") == 1 and (st.get("refused") or {}).get(
                       "forbidden", 0) >= 1,
                   {x: st.get(x) for x in ("forbidden", "forbid_reason", "links_armed",
                                           "refused")}),
                  ("no variant entry after the reset", bool(rs) and hits_after(r, rs[0]["seg"])
                   == 0, hits_after(r, rs[0]["seg"] if rs else 0))]
        record(name, items, r, {"events": ev, "guest_oracle_failures_reported": fails,
                                "note": "qemu_plugin_reset(id, NULL) unregisters FastDyn's "
                                        "callbacks; guest output after it is reported, not "
                                        "required to equal the oracle"})

    # -- 6/7. invalidate C / invalidate R at a PAUSED stop (b3-13) -----------------------
    diag13 = FIX / "inputs/diag-13d.bin"
    for nm_, script in (("invalidate-C:b3-13", WP22GDB / "wp22-13-invc.gdb"),
                        ("invalidate-R:b3-13", WP22GDB / "wp22-13-invr.gdb")):
        if "b3-13" not in published or not want(nm_):
            continue
        a = published["b3-13"]
        r = gdb_run(a, out, nm_.replace(":", "-"), qemu, guest=script, sigint_after=2.0,
                    inp=diag13)
        items, ev = event_checks(r)
        st = r["stats"] or {}
        inv = [e for e in loader_events(r, "tb_invalidate_phys_range")
               if e["b"].get("armed") and "before" in e]
        g = r.get("guest") or {}
        items += [("guest scenario checks (B3DIAG + results, QEMU exit 0)", g.get("ok") is True,
                   [c for c in g.get("checks", []) if not c["ok"]])]
        if inv:
            e = inv[0]
            b, af = e["before"], e.get("after") or {}
            s_b, s_a = b["slots"][0], af.get("slots", [{}])[0]
            items.append(("before the write: R's SPEC slot jumps into C'",
                          s_b.get("target_variant") == 0 and bool(s_b.get("dest")),
                          {k: s_b.get(k) for k in ("dest", "target_variant", "at_reset")}))
            if nm_.startswith("invalidate-C"):
                items += [("after: C invalid, R's slot back at its reset path (jmp F), "
                           "jmp_dest cleared",
                           af["variants"][0].get("general_invalid") is True and
                           s_a.get("at_reset") is True and not s_a.get("dest"),
                           {"general_invalid": af["variants"][0].get("general_invalid"),
                            **{k: s_a.get(k) for k in ("dest", "at_reset")}})]
            else:
                items += [("after: R invalid and removed from C's jump list",
                           s_a.get("R_invalid") is True and
                           af["variants"][0].get("general_jmp_list_head") == 0,
                           {"R_invalid": s_a.get("R_invalid"),
                            "C_jmp_list_head": af["variants"][0].get("general_jmp_list_head")})]
            items.append(("no variant entry after the write", hits_after(r, e["seg"]) == 0,
                          hits_after(r, e["seg"])))
            items.append(("C' entered before the write (iterations 1..299)",
                          hits_total(r) - hits_after(r, e["seg"]) == 299,
                          hits_total(r) - hits_after(r, e["seg"])))
        else:
            items.append(("debug write observed while the link was armed", False,
                          [x["ev"] for x in r["host"].get("events", [])][:10]))
        items.append(("stats links_armed == 1 (never re-armed)", st.get("links_armed") == 1,
                      st.get("links_armed")))
        record(nm_, items, r, {"events": ev})

    # -- 8. guest debugger on the gdbstub (WP24 scenarios, unchanged) ----------------------
    # f-b3-12 / f-b3-13a set their first breakpoint before the guest runs, on the
    # page of Vc: C is then looked up with CF_BP_PAGE, never activated from the
    # cache, and no link is armed (a finding for WP28, see the report); f-b3-13b
    # keeps a breakpoint (b3_done) and a watchpoint: arming is refused; f-b3-13d
    # pauses (no flush) with the link armed and then hits a breakpoint at Vc
    # (RUN_STATE_DEBUG stop = tb_flush with the link armed); since WP24 review 3
    # it also writes r0 := 7 at that first Vc stop, and its EXPECT lines (checked
    # through gdb_guest's result) require z 999 / nz 1.
    for nm_, art, script, inp, sig in (
            ("bp-stop-flush:b3-12", "b3-12", FIX / "diag/f-b3-12.gdb", None, None),
            ("bp-kept:b3-13", "b3-13", FIX / "diag/f-b3-13a.gdb", None, None),
            ("watchpoint:b3-13", "b3-13", FIX / "diag/f-b3-13b.gdb", None, None),
            ("paused-bp:b3-13", "b3-13", FIX / "diag/f-b3-13d.gdb", diag13, 2.0)):
        if art not in published or not want(nm_):
            continue
        a = published[art]
        r = gdb_run(a, out, nm_.replace(":", "-"), qemu, guest=script, sigint_after=sig, inp=inp)
        items, ev = event_checks(r)
        st = r["stats"] or {}
        g = r.get("guest") or {}
        refused = st.get("refused") or {}
        flushes = loader_events(r, "tcg_supercache_runtime_reset")
        items.append(("guest scenario checks (WP24 EXPECT lines, QEMU exit 0)",
                      g.get("ok") is True, [c for c in g.get("checks", []) if not c["ok"]]))
        items.append(("the debugger's stops flushed (Q6 reset observed)",
                      (st.get("flush_resets") or 0) >= 1 and len(flushes) == st.get(
                          "flush_resets"), (st.get("flush_resets"), len(flushes))))
        if nm_ in ("bp-stop-flush:b3-12", "bp-kept:b3-13"):
            items += [("breakpoint on Vc's page before the run: no link armed, C' never "
                       "entered", st.get("links_armed") == 0 and hits_total(r) == 0,
                       (st.get("links_armed"), hits_total(r))),
                      ("no refusal needed: C (CF_BP_PAGE lookup) is never activated from the "
                       "cache", sum(refused.values()) == 0, refused)]
        if nm_ == "watchpoint:b3-13":
            items += [("arming refused while the debugger's breakpoint/watchpoint lists are "
                       "non-empty", refused.get("breakpoints", 0) + refused.get("watchpoints", 0)
                       > 0 and st.get("links_armed") == 0, (refused, st.get("links_armed"))),
                      ("watchpoint inserts flushed the TLB (disarm_tlb_events > 0)",
                       (st.get("disarm_tlb_events") or 0) > 0, st.get("disarm_tlb_events")),
                      ("C' never entered", hits_total(r) == 0, hits_total(r))]
        if nm_ == "paused-bp:b3-13":
            armed_flush = [e for e in flushes if e["b"].get("armed")]
            vh = r["host"]["variant_hits"].get("0", {})
            items += [("the first tb_flush (the breakpoint hit at Vc) found the link still armed: "
                       "the PAUSED stop and the breakpoint insert did not flush (G11)",
                       bool(flushes) and flushes[0]["b"].get("armed") == 1,
                       flushes[0]["b"] if flushes else None),
                      ("that flush cleared it (nothing live or armed after)",
                       bool(armed_flush) and not armed_flush[0]["a"].get("armed")
                       and not armed_flush[0]["a"].get("anything_live"),
                       armed_flush[0].get("a") if armed_flush else None),
                      ("C' entered 299 times (iterations 1..299) and not after the pause: "
                       "the inline g_bp guard sent R to F while the link was armed",
                       vh.get("total") == 299 and max(
                           (h["last_r4"] for h in vh.get("segments", {}).values()),
                           default=None) == 299, vh),
                      ("never re-armed (links_armed == 1)", st.get("links_armed") == 1,
                       st.get("links_armed"))]
        record(nm_, items, r, {"events": ev})

    doc["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    sc = doc["scenarios"]
    doc["summary"] = {k: v["verdict"] for k, v in sc.items()}
    doc["verdict"] = "PASS" if sc and all(v["verdict"] == "PASS" for v in sc.values()) else "FAIL"
    (out / "wp22-stress.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    doc = run_all(a.qemu.resolve(), out, a.only, a.timeout)
    log(f"verdict {doc['verdict']} -> {out / 'wp22-stress.json'}")
    return 0 if doc["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
