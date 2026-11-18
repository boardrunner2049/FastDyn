#!/usr/bin/env python3
"""WP26 integration stage (plan ``## WP26``, Acceptance (integration); design
§9.4, §9.5 F-B3-18 / F-B4-3). Nothing is timed; no timing lock is taken.

    wp26_integ.py --out OUT [--only f_b3_18 o2b harness] [--timeout S]

Every QEMU run: a fresh run directory made by the scopt harness
(``deploy_new``: FastDyn oracle, fingerprint, sparse copy), ``setarch x86_64
-R``, a timeout, ``supercache_flat=false``; gdb always ``-nx``, never with
cwd inside a QEMU tree.

1. **F-B3-18** (``f_b3_18``). Base snapshot of WP24 phase F-B3-1 prepared
   and imported **on the gate binary** (``wp22_stress.Bases``: icount render
   of ``fixtures/b3/b3-1.toml.in``, ``scopt prepare``/``bundle``/``import``/
   ``admission identity``). Three v6 artifacts written by WP18
   (``scopt.validate.package_b3``):

   * ``identity-v6``: ``B3Plan(force_v6=True)`` (empty extension table);
   * ``b3-1-spec``: the F-B3-1 v6 artifact of the design (R = ``f_b3_1``,
     SPEC slot to ``vc_b3_1``, an identity-clone variant);
   * ``wp18-synthetic-v6``: the synthetic plan of WP18's unit tests
     (``tests/synth/b3.py``: ``r_hosts``/``spec_pairs``, one grown R with
     two SPEC slots and two variants) applied to the same snapshot.

   Per artifact: the artifact **directory** on the gate binary (full
   RuntimeIdentity checks; expect launch verdict and proof PASS with the v6
   proof rows ``v6_header`` and ``ext`` true, fixture oracle clean, O1 equal
   to the v5 warm reference on the gate binary); the artifact directory on
   ``qint`` (expect a deploy refusal: its RuntimeIdentity names the gate
   exe); the bare ``cache.sc`` on ``qint`` (no identity check; expect proof
   INVALID with ``v6_header`` failed and launch verdict
   ``INVALID_artifact_not_used``). Controls: the v5 base cache on the gate
   binary (the O1 reference) and bare on ``qint`` (expect PASS, so the qint
   INVALID is caused by the v6 format, not by the cache being foreign).

2. **O2b on F-B4-3** (``o2b``). Base snapshot of phase F-B4-3 on the gate
   binary, its identity-v6 artifact. Runs on the gate binary: v5 warm
   reference (``launch``); identity-v6 (``launch``); identity-v6 under a
   guest ``arm-none-eabi-gdb -nx`` on the gdbstub with a **control**
   script (watch ``g_sweep``, change nothing) and with the **shift**
   script (when ``systick_start`` stores the sweep pointer, advance it by
   one entry: the first reload is unchanged, every later reload comes from
   the shifted table, so SysTick lands at other instructions). The gdb runs
   start QEMU from the deploy oracle's argv (``qemu_argv.json``) with
   ``-S -gdb``; after the run the §4.8 post-run proof (``prove_run``) is
   taken and a launch-style verdict derived by the launcher's rules. O2b
   (``o2b_records``, icount, exact) of each run against the reference:
   expect pass, pass, pass, **fail** with record differences and a sound
   reference log; O4's rule (``o4_detected``) applied to the shift run as
   if it were a control counts it as detected.

3. **Harness on the gate binary** (``harness``, supplementary). ``scopt
   validate --dynamic --oracles`` on ``b3-1-spec``: on ``qint`` it is
   refused before any launch (exit 2, ``v6_loader_required``); on the gate
   binary O1, O2a, O2b, O3 (``links_armed > 0`` and C' hits > 0) and O5.

Writes ``OUT/wp26-integ.json`` (verdict per item and overall).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
sys.path.insert(0, str(WT))
sys.path.insert(0, str(HERE))
ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FIX = ROOT / "fixtures/b3"

import wp22_stress as S  # noqa: E402  (Bases, ArtSpec, build_artifact, nm)

EXP = S.EXP
GG = S.GG

from scopt.run import oracles as O  # noqa: E402
from scopt.run.deploy import DeployError, deploy_new  # noqa: E402
from scopt.run.launcher import (FASTDYN_SRC, file_sha256, launch, load_inputs,  # noqa: E402
                                norm_guest, reset_ram, scrub_env, stray_capture_files)
from scopt.run.proof import prove_run  # noqa: E402

PY = sys.executable
GATE_JSON = ROOT / "runs/wp22/gate-binary.json"
QINT = ROOT / "wt/qemu-int/build/qemu-system-arm"
QINT_SHA = "8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
V6_PROOF_ROWS = ("header", "records", "v6_header", "ext")
S_WARNING = "qemu-system-arm: warning: icount sleep disabled and no active timers"

CONTROL_GDB = """\
# WP26 O2b integration (control): stop once when systick_start stores the
# SysTick sweep pointer (g_sweep), change nothing.
# EXPECT stopped=1
watch g_sweep
continue
printf "B3DIAG stopped=%d\\n", 1
printf "B3DIAG sweep=0x%x\\n", g_sweep
delete
detach
"""

SHIFT_GDB = """\
# WP26 O2b integration (shifted SysTick reload): when systick_start stores the
# sweep pointer of F-B4-3 (SWEEP_SLOW = {997, 1009, 1013, ...}), advance it by
# one entry. The first reload (997, written right after) is unchanged; every
# later reload comes from the shifted table, so SysTick lands at other
# instructions and other instruction counts.
# EXPECT stopped=1
watch g_sweep
continue
set var g_sweep = g_sweep + 4
set var g_sweep_n = g_sweep_n - 1
printf "B3DIAG stopped=%d\\n", 1
printf "B3DIAG sweep=0x%x\\n", g_sweep
delete
detach
"""


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# pure helpers (unit-tested in tests/unit/test_wp26_integ.py)
# ---------------------------------------------------------------------------

def launch_style_verdict(proof: dict[str, Any] | None, rc: int | None,
                         stray: list[str]) -> str:
    """The verdict rule of :func:`scopt.run.launcher.launch` for a run that
    was started outside the launcher (QEMU under a guest debugger)."""
    v = (proof or {}).get("verdict") or "NOT_PROVED"
    if stray:
        v = "INVALID_stray_capture_files"
    if rc != 0:
        v = "INVALID_exit_code" if v in ("PASS", "NOT_PROVED") else v
    return v


def proof_rows(proof: dict[str, Any] | None) -> dict[str, Any]:
    checks = {c.get("check"): c.get("ok") for c in (proof or {}).get("checks", [])}
    return {k: checks.get(k) for k in V6_PROOF_ROWS}


def judge_gate(run: dict[str, Any]) -> list[str]:
    """F-B3-18, gate side: why the run is not a proved v6 run ([] = pass)."""
    why = []
    if run.get("qemu_ok") is not True:
        why.append(f"ran {run.get('qemu')}, not the gate binary")
    if run.get("verdict") != "PASS":
        why.append(f"launch verdict {run.get('verdict')}")
    if run.get("proof") != "PASS":
        why.append(f"proof {run.get('proof')}")
    rows = run.get("proof_rows") or {}
    for k in ("v6_header", "ext"):
        if rows.get(k) is not True:
            why.append(f"proof row {k} = {rows.get(k)}")
    if run.get("exit_code") != 0:
        why.append(f"exit {run.get('exit_code')}")
    return why


def judge_qint(run: dict[str, Any]) -> list[str]:
    """F-B3-18, qint side: why the run does not show the old loader refusing
    the v6 file ([] = pass: proof INVALID with ``v6_header`` failed)."""
    why = []
    if "deploy_refused" in run:
        return [f"deploy refused {run['deploy_refused']} (no run)"]
    if run.get("qemu_ok") is not True:
        why.append(f"ran {run.get('qemu')}, not qint")
    if run.get("proof") == "PASS" or not str(run.get("proof", "")).startswith("INVALID"):
        why.append(f"proof {run.get('proof')}")
    if "v6_header" not in (run.get("failed") or []):
        why.append(f"v6_header not in failed {run.get('failed')}")
    if run.get("exit_code") != 0:
        why.append(f"exit {run.get('exit_code')} (the guest should still run, uncached)")
    return why


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def toml_for(base: S.Base, qemu: Path) -> Path:
    """The base's fixture TOML rendered for ``qemu``: FastDyn takes the QEMU
    from the TOML's ``qemu_path`` (it wins over ``FASTDYN_QEMU``), so a run
    on another binary needs its own rendering (same template, same icount
    arm; only ``qemu_path`` differs)."""
    if file_sha256(qemu) == file_sha256(Path(tomllib.loads(base.toml.read_text())
                                             ["Machine"]["qemu_path"])):
        return base.toml
    text, _ = S.fdrun.render(str(FIX / f"b3-{base.cfg}.toml.in"), "sc", f"b3-{base.cfg}.sc",
                             str(qemu), "none")
    p = base.toml.with_name(f"b3-{base.cfg}.{file_sha256(qemu)[7:15]}.toml")
    p.write_text(text)
    return p


def plain(src: Path, base: S.Base, rd_path: Path, qemu: Path, timeout: float,
          *, mode: str = "icount") -> dict[str, Any]:
    """deploy_new + launch; never raises on a deploy refusal (recorded).
    ``qemu_ok``: the launch ran ``qemu`` (sha256 compared)."""
    try:
        rd, dep = deploy_new(src, toml_for(base, qemu), rd_path, qemu=str(qemu))
    except DeployError as e:
        return {"run_dir": str(rd_path), "deploy_refused": e.code, "detail": e.detail[:400]}
    stats = rd.root / "v6-stats.json"
    doc = launch(rd, timeout_s=timeout, inputs={"input": str(base.inp)},
                 extra_env={"QEMU_SC_V6_STATS": str(stats)})
    pr = doc.get("proof") or {}
    norm = O.fixture_norm(doc.get("guest_norm") or [], doc.get("exit_code"))
    return {"run_dir": str(rd.root), "qemu": doc.get("qemu"),
            "qemu_sha256": doc.get("qemu_sha256"),
            "qemu_ok": doc.get("qemu_sha256") == file_sha256(qemu),
            "toml": str(rd.toml), "verdict": doc.get("verdict"),
            "exit_code": doc.get("exit_code"), "timed_out": doc.get("timed_out"),
            "proof": pr.get("verdict"), "failed": pr.get("failed"),
            "failed_detail": {c.get("check"): c.get("detail") for c in pr.get("checks", [])
                              if not c.get("ok")},
            "appended": pr.get("appended"), "proof_rows": proof_rows(pr),
            "fixture_oracle_failures": EXP.compare(norm, base.cfg, mode),
            "v6_stats": json.loads(stats.read_text()) if stats.is_file() else None,
            "v6_log": [ln for ln in Path(doc["stdout_log"]).read_text(
                errors="replace").splitlines() if "supercache" in ln][:6],
            "doc": doc}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def guest_gdb_run(src: Path, base: S.Base, rd_path: Path, qemu: Path, script_text: str,
                  timeout: float) -> dict[str, Any]:
    """Deploy ``src`` like :func:`plain`, then run QEMU from the deploy
    oracle's argv with ``-S -gdb`` and drive the guest with
    ``arm-none-eabi-gdb -nx -batch`` running ``script_text``; afterwards the
    post-run proof and a launch-style document."""
    rd, _ = deploy_new(src, toml_for(base, qemu), rd_path, qemu=str(qemu))
    ram = reset_ram(rd)
    loaded = load_inputs(rd, {"input": str(base.inp)})
    qa = json.loads((rd.root / "qemu_argv.json").read_text())
    if Path(qa["argv"][0]).resolve() != qemu.resolve():
        raise SystemExit(f"oracle argv uses {qa['argv'][0]}, not {qemu}")
    port = _free_port()
    argv = list(qa["argv"]) + ["-S", "-gdb", f"tcp:127.0.0.1:{port}"]
    stats = rd.root / "v6-stats.json"
    env, env_diff = scrub_env(qemu=qemu, extra={**qa.get("env_set", {}),
                                                "QEMU_SC_V6_STATS": str(stats)})
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10",
           f"{timeout:g}", *argv]
    script = rd.root / "guest.gdb"
    script.write_text(script_text)
    (rd.root / "cmd.txt").write_text(" ".join(cmd) + "\n")
    out_log = rd.root / "qemu.stdout"
    t0 = time.monotonic()
    with open(out_log, "wb") as qf:
        q = subprocess.Popen(cmd, cwd=str(FASTDYN_SRC), env=env, stdout=qf,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        for _ in range(600):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if q.poll() is not None:
                    break
                time.sleep(0.1)
        gcmd = ["timeout", "--kill-after=10", f"{timeout:g}", GG.GDB, "-nx", "-batch",
                "-ex", "set confirm off", "-ex", "set pagination off",
                "-ex", f"target remote 127.0.0.1:{port}", "-x", str(script), str(FIX / "b3.elf")]
        with open(rd.root / "gdb-guest.log", "wb") as gl:
            grc = subprocess.run(gcmd, cwd=str(rd.root), stdout=gl, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL).returncode
        try:
            rc = q.wait(timeout=timeout + 30)
        except subprocess.TimeoutExpired:
            q.kill()
            rc = q.wait()
    text = out_log.read_text(errors="replace")
    diag: dict[str, list[str]] = {}
    for ln in (rd.root / "gdb-guest.log").read_text(errors="replace").splitlines():
        m = re.match(r"B3DIAG (\w+)=(\S+)", ln)
        if m:
            diag.setdefault(m.group(1), []).append(m.group(2))
    pr = prove_run(rd)
    stray = stray_capture_files(rd.root)
    verdict = launch_style_verdict(pr, rc, stray)
    ng = norm_guest(text)
    doc = {"schema": "wp26.integ.guest_gdb_run/1", "run_dir": str(rd.root), "argv": cmd,
           "launcher": "QEMU from qemu_argv.json under a guest gdb (not scopt launch)",
           "qemu": str(qemu), "qemu_sha256": file_sha256(qemu), "exit_code": rc,
           "gdb_exit": grc, "seconds": round(time.monotonic() - t0, 1), "ram_reset": ram,
           "inputs": loaded, "env_diff": env_diff, "guest_norm": ng,
           "stray_capture_files": stray, "proof": pr, "appended": pr.get("appended"),
           "verdict": verdict, "gdb_diag": diag}
    (rd.root / "run.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    # QEMU started with -S prints this warning once (the vCPU is stopped while
    # gdb connects); a FastDyn launch never prints it. Only this exact line
    # is dropped, for the fixture oracle; guest_norm (O1/O2b) is unchanged.
    fx = [ln for ln in ng if ln != S_WARNING]
    norm = O.fixture_norm(fx, rc)
    return {"run_dir": str(rd.root), "qemu": argv[0],
            "fixture_oracle_dropped_lines": len(ng) - len(fx),
            "qemu_ok": file_sha256(argv[0]) == file_sha256(qemu),
            "verdict": verdict, "exit_code": rc, "gdb_exit": grc,
            "proof": pr.get("verdict"), "failed": pr.get("failed"),
            "appended": pr.get("appended"), "proof_rows": proof_rows(pr), "gdb_diag": diag,
            "fixture_oracle_failures": EXP.compare(norm, base.cfg, "icount"),
            "v6_stats": json.loads(stats.read_text()) if stats.is_file() else None,
            "doc": doc}


def field_differences(ref_lines: list[str], art_lines: list[str]) -> dict[str, int]:
    """Per SysTick record field: in how many of the paired records (index
    by index, up to the shorter log) the field differs; plus the counts."""
    pr, pa = O.parse_handler_records(ref_lines), O.parse_handler_records(art_lines)
    out = {f: 0 for f in O.SREC}
    for x, y in zip(pr["systick"], pa["systick"]):
        for f in O.SREC:
            if x.get(f) != y.get(f):
                out[f] += 1
    out["paired"] = min(len(pr["systick"]), len(pa["systick"]))
    out["reference_records"] = len(pr["systick"])
    out["artifact_records"] = len(pa["systick"])
    return out


def strip(r: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if k != "doc"}


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

def synthetic_artifact(base: S.Base, out: Path, exe_sha: str) -> S.Artifact:
    """WP18's unit-test synthetic v6 plan (tests/synth/b3.py) on ``base``."""
    from scopt.link import B3Plan
    from scopt.validate import package_b3
    from tests.synth import b3 as H

    s = base.snap
    hosts = H.r_hosts(s)
    if not hosts:
        raise SystemExit("synthetic: no unit can host two SPEC slots")
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:1]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs[:2])
    plan = B3Plan(grow=grow, variants=tuple(vs))
    spec = S.ArtSpec("wp18-synthetic-v6", base.cfg,
                     notes="tests/synth/b3.py spec_pairs: one grown R, two SPEC slots, two "
                           "identity-clone variants")
    store = out / "artifacts" / spec.name
    r = package_b3(s, plan, store, candidate_id="sha256:" + "26" * 32, runtime=base.ri,
                   allow_uncertified=True, v6_exes=[exe_sha])
    a = S.Artifact(spec, base, Path(r.artifact_dir) if r.artifact_dir else None, r.status,
                   r.reason)
    a.layout = {"status": r.status, "reason": r.reason,
                "r_uid": [str(k) for k in grow], "variants": len(vs),
                "r_vaddrs": [hex(u.vaddrs[0]) for u in hosts[:1]],
                "c_vaddrs": [hex(u.vaddrs[0]) for u in cs[:2]],
                "fails": None if r.validation is None else
                [x["code"] + ":" + x["check"] for x in r.validation.rows
                 if x["result"] == "fail"]}
    return a


def item_f_b3_18(bases: S.Bases, out: Path, gate: Path, exe_sha: str,
                 timeout: float) -> dict[str, Any]:
    base = bases.get("1")
    res: dict[str, Any] = {"base": base.info, "toml": str(base.toml), "artifacts": {},
                           "controls": {}}
    arts = [S.build_artifact(S.ArtSpec("identity-v6", "1", identity=True,
                                       notes="identity-v6 written by WP18"), base, out, exe_sha),
            S.build_artifact(S.ArtSpec("b3-1-spec", "1",
                                       (S.Grow(S.nm()["f_b3_1"],
                                               (("SPEC", S.nm()["vc_b3_1"]),)),),
                                       notes="F-B3-1: R = f_b3_1, SPEC slot -> vc_b3_1"),
                             base, out, exe_sha),
            synthetic_artifact(base, out, exe_sha)]
    snapdir = next((out / "base/1/store/snapshots").iterdir())
    log("F-B3-18: v5 base on the gate binary (O1 reference)")
    ref = plain(snapdir, base, out / "runs/f18-ref-gate-v5", gate, timeout)
    res["controls"]["v5_gate"] = strip(ref)
    # The runtime_signature in a .sc header is bound to the exe: qint's v5
    # loader rejects a gate-made v5 cache too (reported, not a check).
    foreign = plain(Path(base.info["sc"]), base, out / "runs/f18-info-qint-gate-v5-bare", QINT,
                    timeout)
    res["controls"]["gate_v5_bare_on_qint_info"] = strip(foreign)
    # Discriminating control: a base prepared on qint (qint's runtime_signature).
    # Its v5 cache must PASS on qint, and an identity-v6 written by WP18 from
    # it (v6_exes = [qint sha]: a test-only override of V7 v6_capable_runtime,
    # this directory is never published or run on the gate) must be INVALID on
    # qint with v6_header failed and no runtime_signature difference, so the
    # qint INVALID of the gate artifacts is caused by the v6 format.
    qbases = S.Bases(QINT, out / "qint")
    paths = [bases.env["SCOPT_ENVLAYOUT_PATH"], qbases.env["SCOPT_ENVLAYOUT_PATH"]]
    os.environ["SCOPT_ENVLAYOUT_PATH"] = ":".join(dict.fromkeys(paths))
    bases.env["SCOPT_ENVLAYOUT_PATH"] = qbases.env["SCOPT_ENVLAYOUT_PATH"] = \
        os.environ["SCOPT_ENVLAYOUT_PATH"]
    qbase = qbases.get("1")
    qsnap = next((out / "qint/base/1/store/snapshots").iterdir())
    ctl = plain(qsnap, qbase, out / "runs/f18-ctl-qint-v5", QINT, timeout)
    res["controls"]["qint_v5_on_qint"] = strip(ctl)
    qa = S.build_artifact(S.ArtSpec("identity-v6-qintsig", "1", identity=True,
                                    notes="identity-v6 of the qint-prepared base (control)"),
                          qbase, out / "qint", QINT_SHA)
    res["controls"]["qintsig_identity_v6"] = {"package": qa.layout,
                                              "dir": str(qa.dir) if qa.dir else None}
    qs = None
    if qa.status == "published":
        qs = plain(qa.dir / "cache.sc", qbase, out / "runs/f18-ctl-qint-qintsig-id6-bare",
                   QINT, timeout)
        res["controls"]["qintsig_identity_v6"]["qint_bare"] = strip(qs)
    sig_diff = None if qs is None else any(
        "runtime_signature" in str(d) for d in (qs.get("failed_detail") or {}).values())
    cw = []
    if not (ref.get("verdict") == "PASS" and ref.get("qemu_ok") is True):
        cw.append(f"gate v5 reference {ref.get('verdict')}")
    if not (ctl.get("verdict") == "PASS" and ctl.get("qemu_ok") is True):
        cw.append(f"qint v5 control {ctl.get('verdict')}")
    if qs is None:
        cw.append(f"qint-signature identity-v6 not packaged: {qa.status} {qa.reason}")
    else:
        cw += [f"qint-signature identity-v6: {w}" for w in judge_qint(qs)]
        if sig_diff is not False:
            cw.append(f"qint-signature identity-v6: runtime_signature differs ({sig_diff})")
    res["controls"]["why"] = cw
    res["controls"]["ok"] = ctl_ok = not cw
    log(f"  controls: {'ok' if ctl_ok else cw}; qint-signature identity-v6 on qint "
        f"{None if qs is None else (qs.get('verdict'), qs.get('failed_detail'))}")
    for a in arts:
        name = a.spec.name
        ent: dict[str, Any] = {"package": a.layout, "dir": str(a.dir) if a.dir else None}
        res["artifacts"][name] = ent
        log(f"  {name}: {a.status} {a.reason or ''}")
        if a.status != "published":
            ent["f_b3_18"] = {"pass": False, "why": [f"package {a.status} {a.reason}"]}
            continue
        g = plain(a.dir, base, out / f"runs/f18-{name}-gate", gate, timeout)
        qd = plain(a.dir, base, out / f"runs/f18-{name}-qint-dir", QINT, timeout)
        qb = plain(a.dir / "cache.sc", base, out / f"runs/f18-{name}-qint-bare", QINT, timeout)
        o1 = O.o1_outputs(ref["doc"], g["doc"]) if "doc" in g and "doc" in ref else None
        ent["gate"] = strip(g)
        ent["qint_dir"] = strip(qd)
        ent["qint_bare"] = strip(qb)
        ent["o1_gate_vs_v5_gate"] = None if o1 is None else o1.to_json()
        wg, wq = judge_gate(g), judge_qint(qb)
        if g.get("fixture_oracle_failures"):
            wg.append(f"fixture oracle {g['fixture_oracle_failures'][:3]}")
        if o1 is None or o1.verdict != "pass":
            wg.append(f"O1 vs v5 reference {None if o1 is None else o1.verdict}")
        ent["f_b3_18"] = {"gate_pass": not wg, "gate_why": wg, "qint_invalid": not wq,
                          "qint_why": wq, "qint_dir_refused": qd.get("deploy_refused"),
                          "pass": not wg and not wq}
        log(f"  {name}: gate {g.get('verdict')} rows {g.get('proof_rows')} O1 "
            f"{None if o1 is None else o1.verdict}; qint bare {qb.get('verdict')} "
            f"failed {qb.get('failed')}; qint dir {qd.get('deploy_refused') or qd.get('verdict')}")
    res["pass"] = ctl_ok and len(res["artifacts"]) == 3 and all(
        e["f_b3_18"]["pass"] for e in res["artifacts"].values())
    return res


def item_o2b(bases: S.Bases, out: Path, gate: Path, exe_sha: str,
             timeout: float) -> dict[str, Any]:
    base = bases.get("b4-3")
    res: dict[str, Any] = {"base": base.info, "toml": str(base.toml), "runs": {}, "o2b": {}}
    a = S.build_artifact(S.ArtSpec("b4-3-identity-v6", "b4-3", identity=True,
                                   notes="identity-v6 of F-B4-3 (WP18)"), base, out, exe_sha)
    res["artifact"] = {"dir": str(a.dir) if a.dir else None, "status": a.status,
                       "reason": a.reason}
    if a.status != "published":
        res["pass"] = False
        return res
    snapdir = next((out / "base/b4-3/store/snapshots").iterdir())
    log("O2b: F-B4-3 v5 reference, identity-v6, gdb control, gdb shift (gate binary)")
    runs = {"reference_v5": plain(snapdir, base, out / "runs/o2b-ref-v5", gate, timeout),
            "identity_v6": plain(a.dir, base, out / "runs/o2b-id6", gate, timeout),
            "gdb_control": guest_gdb_run(a.dir, base, out / "runs/o2b-gdb-control", gate,
                                         CONTROL_GDB, timeout),
            "gdb_shift": guest_gdb_run(a.dir, base, out / "runs/o2b-gdb-shift", gate,
                                       SHIFT_GDB, timeout)}
    for k, r in runs.items():
        res["runs"][k] = strip(r)
        log(f"  {k}: verdict {r.get('verdict')} proof {r.get('proof')} rows "
            f"{r.get('proof_rows')} exit {r.get('exit_code')} diag {r.get('gdb_diag')}")
    ref = runs["reference_v5"]["doc"]
    expect = {"identity_v6": "pass", "gdb_control": "pass", "gdb_shift": "fail"}
    ok = runs["reference_v5"].get("verdict") == "PASS" and \
        all(r.get("qemu_ok") is True for r in runs.values())
    for k, want in expect.items():
        r = runs[k]
        o = O.o2b_records(ref["guest_norm"], r["doc"]["guest_norm"], "icount")
        j = o.to_json()
        res["o2b"][k] = j
        good = o.verdict == want and r.get("verdict") == "PASS" and \
            r["proof_rows"].get("v6_header") is True and r["proof_rows"].get("ext") is True
        j["field_differences"] = field_differences(ref["guest_norm"], r["doc"]["guest_norm"])
        if k == "gdb_shift":
            fd = j["field_differences"]
            good = good and fd["pc"] > 0 and fd["iter"] > 0 and fd["cvr"] + fd["pc"] > 0
            good = good and not o.data.get("reference_problems") and \
                bool(o.data.get("differences")) and r["gdb_diag"].get("stopped") == ["1"]
            o4 = O.o4_detected([o], artifact_run=r["doc"], reference_run=ref)
            res["o4_rule_on_shift"] = o4.to_json()
            good = good and o4.verdict == "pass"
        if k in ("identity_v6", "gdb_control"):
            good = good and not r.get("fixture_oracle_failures")
        if k == "gdb_control":
            good = good and r["gdb_diag"].get("stopped") == ["1"]
        res["o2b"][k]["expected"] = want
        res["o2b"][k]["ok"] = good
        ok = ok and good
        log(f"  O2b {k}: {o.verdict} (want {want}) {o.detail[:160]}")
    res["pass"] = ok
    return res


def _cli(args: list[str], logf: Path, env: dict[str, str]) -> int:
    with open(logf, "w") as f:
        return subprocess.run([PY, "-m", "scopt", *args], stdout=f, stderr=subprocess.STDOUT,
                              cwd=str(WT), env=env).returncode


def item_harness(bases: S.Bases, out: Path, gate: Path, exe_sha: str,
                 timeout: float) -> dict[str, Any]:
    base = bases.get("1")
    art = out / "artifacts" / "b3-1-spec"
    dirs = [p for p in art.glob("artifacts/*") if p.is_dir()]
    if len(dirs) != 1:
        return {"pass": False, "why": f"b3-1-spec artifact not found under {art} (run f_b3_18)"}
    adir = dirs[0]
    env = dict(bases.env)
    res: dict[str, Any] = {"artifact": str(adir)}
    p = subprocess.run([PY, "-m", "scopt", "diag", "probes", str(adir)], capture_output=True,
                       text=True, cwd=str(WT), env=env)
    (out / "harness-probes.txt").write_text(p.stdout + p.stderr)
    res["probes"] = p.stdout.strip().splitlines()[:12]
    snapdir = next((out / "base/1/store/snapshots").iterdir())
    common = [str(adir), "--dynamic", "-c", str(base.toml), "--baseline", str(snapdir),
              "--inputs", str(base.inp),
              "--fixture-input", "1", "--fixture-expected", str(FIX / "expected.py"),
              "--timeout", str(timeout), "--json"]
    rc_q = _cli(["validate", *common, "--out", str(out / "harness-qint"), "--qemu", str(QINT),
                 "--oracles", "O1,O5"], out / "harness-qint.log", env)
    qtxt = (out / "harness-qint.log").read_text(errors="replace")
    res["qint"] = {"exit": rc_q, "v6_loader_required": "v6_loader_required" in qtxt,
                   "launched": (out / "harness-qint").is_dir() and
                   any((out / "harness-qint").rglob("run.json"))}
    o3 = json.dumps({"stats": {"links_armed": ">0"}, "hits": {"variant 0 C'": ">0"}})
    rc_g = _cli(["validate", *common, "--out", str(out / "harness-gate"), "--qemu", str(gate),
                 "--oracles", "O1,O2a,O2b,O3,O5", "--o3", o3, "--hit-target", "variant 0 C'"],
                out / "harness-gate.log", env)
    dj = out / "harness-gate" / "dynamic_oracles.json"
    doc = json.loads(dj.read_text()) if dj.is_file() else None
    res["gate"] = {"exit": rc_g, "verdict": None if doc is None else doc.get("verdict"),
                   "file": str(dj)}
    if doc is not None:
        rows = []
        for c in doc.get("cases", []):
            for r in c.get("oracles", []):
                rows.append({"oracle": r.get("oracle"), "verdict": r.get("verdict"),
                             "detail": str(r.get("detail"))[:200]})
        res["gate"]["results"] = rows
    got = {r["oracle"]: r["verdict"] for r in res["gate"].get("results", [])}
    res["pass"] = rc_q == 2 and res["qint"]["v6_loader_required"] and \
        not res["qint"]["launched"] and rc_g == 0 and res["gate"]["verdict"] == "pass" and \
        all(got.get(o) in ("pass", "reported") for o in ("O1", "O2a", "O2b", "O3", "O5"))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    gj = json.loads(GATE_JSON.read_text())
    gate = Path(gj["exe_path"])
    gsha = file_sha256(gate).removeprefix("sha256:")
    qsha = file_sha256(QINT).removeprefix("sha256:")
    if gsha != gj["exe_sha256"]:
        raise SystemExit(f"gate exe sha {gsha} != frozen {gj['exe_sha256']}")
    if qsha != QINT_SHA:
        raise SystemExit(f"qint exe sha {qsha} != {QINT_SHA}")
    tag = subprocess.run(["git", "-C", gj["worktree"], "rev-parse", gj["tag"] + "^{commit}"],
                         capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "-C", gj["worktree"], "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", gj["worktree"], "status", "--porcelain",
                            "--untracked-files=no"], capture_output=True, text=True).stdout
    doc: dict[str, Any] = {
        "schema": "wp26.integ/1", "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "timing": "none (no timing lock; nothing timed)",
        "gate": {"exe": str(gate), "sha256": gsha, "tag": gj["tag"], "tag_commit": tag,
                 "head": head, "frozen_commit": gj["commit"],
                 "worktree_clean": not dirty.strip()},
        "qint": {"exe": str(QINT), "sha256": qsha},
        "fixture_elf_sha256": file_sha256(FIX / "b3.elf"), "items": {}}
    if tag != gj["commit"] or head != gj["commit"] or dirty.strip():
        raise SystemExit(f"gate tree moved: tag {tag} head {head} dirty {dirty!r}")
    bases = S.Bases(gate, out)
    doc["envlayout_dir"] = bases.env["SCOPT_ENVLAYOUT_PATH"]
    items = {"f_b3_18": item_f_b3_18, "o2b": item_o2b, "harness": item_harness}
    for name, fn in items.items():
        if a.only and name not in a.only:
            continue
        log(f"item {name}")
        try:
            doc["items"][name] = fn(bases, out, gate, gsha, a.timeout)
        except SystemExit as e:
            doc["items"][name] = {"pass": False, "error": str(e)}
        (out / "wp26-integ.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
        log(f"item {name}: pass={doc['items'][name].get('pass')}")
    doc["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    doc["verdict"] = "PASS" if doc["items"] and all(
        v.get("pass") for v in doc["items"].values()) else "FAIL"
    (out / "wp26-integ.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    log(f"verdict {doc['verdict']} -> {out / 'wp26-integ.json'}")
    return 0 if doc["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
