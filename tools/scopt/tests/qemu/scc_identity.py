#!/usr/bin/env python3
"""T-B1-1 (design 2.6), local form: capture must not change any TB byte.

    scc_identity.py QEMU OUT_ROOT [--off 3] [--on 2] [--timeout 120]

Runs the minimal fixture cold, N_off times with capture off (T-B1-1a, the
determinism control) and N_on times with ``QEMU_SC_CAPTURE`` set (T-B1-1b),
all with the **same** QEMU binary and all in **one** run directory
``OUT_ROOT/rd`` that is deleted and recreated before every run (fresh cache,
work and RAM files; the rendered TOML is byte-identical across runs). The
``.scm`` files are written outside the run directory, to ``OUT_ROOT/scm/``.
After each run the sparse ``.sc`` is copied to ``OUT_ROOT/sc/<run>.sc``.

Gate:
  * every run exits 0 and prints the B0 guest-visible lines;
  * if the capture-off runs give one whole-file sha256, every capture-on run
    must have that sha256 (mode ``whole_file``);
  * otherwise (documented nondeterminism) mode ``per_key``: for every
    (TBKey, guest_hash) common to an on run and an off run, the records must
    be equal modulo tb/tc/end offsets and the code bytes equal after
    normalizing only self-relative fields (rel32 into the prologue); every
    absolute host value (external rel32 target, RIP-LEA target, pool data)
    must be equal. The site list comes from the on run's ``.scm``.
  * every capture-on ``.scm`` passes ``scm_smoke.py --sc``;
  * RUN_INFO ``code_address`` equals the ``.sc`` header ``code_address`` of
    every run; the MAPS(first_commit) layouts of the on runs are equal
    (addresses, permissions, offsets, paths; kernel-assigned inode numbers of
    memfd mappings are ignored) and do not contain the ``.scm``. Capture-off
    runs write no MAPS, so on/off layout equality is covered by the ``.sc``
    whole-file comparison (every embedded host address is inside it).

Different binaries are never compared byte for byte (helper addresses move
whenever QEMU text changes); this script uses exactly one binary.
QEMU runs under ``setarch x86_64 -R`` with a timeout; supercache_flat=false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import scm_smoke  # noqa: E402
from scopt.formats import layouts as L  # noqa: E402
from scopt.formats.enums import NONE_U32, ChunkType, Encoding, MapsTag, SiteKind  # noqa: E402

ROOT = Path("/data/extra_copy/fastdyn")
FASTDYN_SRC = ROOT / "src/fastdyn"
FASTDYN_BIN = FASTDYN_SRC / "fastdyn-env/bin/fastdyn"
TEMPLATE = FASTDYN_SRC / "configs/b0-sc.toml"
EXPECTED_LOG = ROOT / "runs/b0/warm1.log"

NORM_RE = re.compile(r"^(Current CPU type|Reg [0-9]+:|\[BENCH\]|qemu-system-arm:|"
                     r"fastdyn\.targets\.qemu_target\|INFO\|  QEMU exited|"
                     r"fastdyn\.core\|INFO\|  FastDyn Exit)")


def norm(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if NORM_RE.match(line):
            out.append(re.sub(r"^(\[BENCH\]\[[^]]*\]) elapsed_ns=.*", r"\1 elapsed=<host-time>", line))
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def render_toml(out: Path, rd: Path) -> None:
    s = TEMPLATE.read_text()
    old = "/data/extra_copy/fastdyn/runs/b0"
    assert s.count(old) == 4, s.count(old)
    s = s.replace(old + "/cache/minimal.sc", f"{rd}/cache/minimal.sc").replace(old + "/", f"{rd}/")
    assert old not in s and "supercache_flat = false" in s
    out.write_text(s)


def read_sc(path: Path) -> dict:
    with open(path, "rb") as f:
        hdr = L.SC_HEADER.unpack(f.read(L.SC_HEADER.size))
        code_size, code_address, record_count = hdr[3], hdr[4], hdr[6]
        rec_base = L.SC_CODE_OFFSET + code_size
        n = min(record_count, max(0, (os.fstat(f.fileno()).st_size - rec_base) // L.SC_RECORD.size))
        f.seek(rec_base)
        raw = f.read(n * L.SC_RECORD.size)
        recs = []
        for i in range(n):
            rb = raw[i * 104:(i + 1) * 104]
            r = L.SC_RECORD.unpack(rb)
            f.seek(L.SC_CODE_OFFSET + r[2])
            recs.append({"slot": i, "raw": rb, "rec": r, "code": f.read(r[1] - r[2])})
    return {"code_address": code_address, "record_count": record_count, "records": recs}


def key_of(r: tuple) -> tuple:
    # TBKey (page_addr0, page_addr1, flags, cs_base, cflags) + pc (0 under PCREL)
    # + guest_hash
    return (r[6], r[7], r[9], r[5], r[10], r[4], r[8])


def scm_sites(path: Path) -> tuple[dict, dict]:
    """slot -> TB dict, plus RUN_INFO and MAPS(first_commit) bytes."""
    data = path.read_bytes()
    off = L.SCM_FILE_HEADER.size
    tbs, info = {}, {}
    sm = scm_smoke.Smoke()
    while off + 16 <= len(data):
        t, pl, _ = L.SCM_CHUNK_HEADER.unpack_from(data, off)
        p = data[off + 16:off + 16 + pl]
        if t == ChunkType.TB:
            tb = scm_smoke.parse_tb(p, sm, "x")
            if tb:
                tbs[tb["slot"]] = tb
        elif t == ChunkType.RUN_INFO:
            info["run_info"] = L.SCM_RUN_INFO.unpack_from(p)
        elif t == ChunkType.MAPS and L.SCM_MAPS_HEADER.unpack_from(p)[0] == MapsTag.FIRST_COMMIT:
            info["maps_first_commit"] = p[16:]
        off += 16 + L.align8(pl) + 8
    return tbs, info


def maps_layout(raw: bytes) -> tuple[str, ...]:
    """/proc/self/maps lines without the dev:inode columns.

    Addresses, permissions, offsets and paths are the layout; the inode of an
    anonymous memfd (e.g. ``/memfd:displaysurface (deleted)``) is assigned by
    the kernel per run and is not."""
    out = []
    for line in raw.decode("latin-1").splitlines():
        f = line.split(None, 5)
        if len(f) >= 5:
            out.append(" ".join(f[:3] + f[5:]))
    return tuple(out)


def per_key_compare(on: dict, off: dict, on_tbs: dict, prologue_end: int) -> dict:
    """Design 2.6 weaker mode. Returns counts and the first mismatches."""
    ca_on, ca_off = on["code_address"], off["code_address"]
    off_by_key = {}
    for r in off["records"]:
        off_by_key.setdefault(key_of(r["rec"]), r)
    common = mism = 0
    examples = []
    for r in on["records"]:
        o = off_by_key.get(key_of(r["rec"]))
        if o is None:
            continue
        common += 1
        a, b = r["rec"], o["rec"]
        # records modulo offsets: everything except tb/end/tc offsets and checksum
        same_rec = a[3:17] == b[3:17] and a[18] == b[18]
        ca, cb = r["code"], o["code"]
        ok = same_rec and len(ca) == len(cb)
        if ok and ca != cb:
            tb = on_tbs.get(r["slot"])
            if tb is None:
                ok = False
            else:
                ba, bb = ca_on + a[2], ca_off + b[2]
                ma, mb = bytearray(ca), bytearray(cb)
                for s in tb["sites"]:
                    fo, io, w, enc, kind, aux, value, ii, ff = s
                    if w != 4 or enc not in (Encoding.REL32, Encoding.RIPREL32, Encoding.POOL_INDIRECT):
                        continue
                    if enc == Encoding.POOL_INDIRECT:
                        continue  # disp to the TB's own pool: self-relative and equal
                    da = struct.unpack_from("<i", ca, fo)[0]
                    db = struct.unpack_from("<i", cb, fo)[0]
                    ta, tb_ = ba + fo + 4 + da, bb + fo + 4 + db
                    if enc == Encoding.RIPREL32:
                        ta, tb_ = ta, tb_  # same end-of-insn correction on both sides
                    in_a = ca_on <= ta < ca_on + (1 << 31)
                    if kind in (SiteKind.EXIT_TB_RET_JMP, SiteKind.EXIT_TB_EPI_JMP):
                        eq = (ta - ca_on) == (tb_ - ca_off) and (ta - ca_on) < prologue_end
                    elif in_a:  # intra/own-struct: relative to the TB itself
                        eq = (ta - ba) == (tb_ - bb)
                    else:       # absolute host value outside the arena
                        eq = ta == tb_
                    if eq:
                        ma[fo:fo + 4] = mb[fo:fo + 4] = b"\0\0\0\0"
                ok = ma == mb
        if not ok:
            mism += 1
            if len(examples) < 10:
                examples.append({"on_slot": r["slot"], "off_slot": o["slot"], "record_equal": same_rec})
    return {"common_keys": common, "mismatches": mism, "examples": examples}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("qemu")
    ap.add_argument("out_root")
    ap.add_argument("--off", type=int, default=3)
    ap.add_argument("--on", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args(argv)
    qemu = Path(a.qemu).resolve()
    out = Path(a.out_root).resolve()
    if out.exists() and any(out.iterdir()):
        print(f"refusing to reuse non-empty {out}", file=sys.stderr)
        return 2
    rd = out / "rd"
    (out / "sc").mkdir(parents=True)
    (out / "scm").mkdir()
    (out / "logs").mkdir()
    toml = out / "b0-sc.toml"
    render_toml(toml, rd)
    expected = norm(EXPECTED_LOG.read_text())
    qemu_sha = sha256_file(qemu)

    plan = [f"off{i + 1}" for i in range(a.off)] + [f"on{i + 1}" for i in range(a.on)]
    runs = []
    for name in plan:
        if rd.exists():
            shutil.rmtree(rd)
        (rd / "cache").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("QEMU_SC_")}
        env["FASTDYN_QEMU"] = str(qemu)
        scm = None
        if name.startswith("on"):
            scm = out / "scm" / f"{name}.scm"
            env["QEMU_SC_CAPTURE"] = str(scm)
        cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10",
               str(a.timeout), str(FASTDYN_BIN), "run", "-c", str(toml), "-o", str(rd / "work")]
        p = subprocess.run(cmd, cwd=FASTDYN_SRC, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors="replace")
        (out / "logs" / f"{name}.log").write_text(p.stdout)
        sc_src = rd / "cache/minimal.sc"
        sc_dst = out / "sc" / f"{name}.sc"
        rec = {"name": name, "exit": p.returncode, "guest_norm_matches_b0": norm(p.stdout) == expected,
               "capture_banner": "supercache-capture: level=reloc" in p.stdout}
        if sc_src.exists():
            subprocess.run(["cp", "--sparse=always", str(sc_src), str(sc_dst)], check=True)
            rec["sc_sha256"] = sha256_file(sc_dst)
            rec["record_count"] = struct.unpack_from("<Q", sc_dst.read_bytes()[:80], 40)[0]
        # nothing capture-related may appear inside the run directory
        rec["stray_scm_in_rd"] = sorted(str(x) for x in rd.rglob("*.scm")) + \
            sorted(str(x) for x in rd.rglob("*.qprof"))
        if scm:
            rec["scm"] = str(scm)
            rec["scm_exists"] = scm.exists()
        runs.append(rec)
        print(json.dumps(rec), flush=True)
    if rd.exists():
        shutil.rmtree(rd)

    fails: list[str] = []
    for r in runs:
        if r["exit"] != 0:
            fails.append(f"{r['name']}: exit {r['exit']}")
        if not r["guest_norm_matches_b0"]:
            fails.append(f"{r['name']}: guest-visible output differs from B0")
        if "sc_sha256" not in r:
            fails.append(f"{r['name']}: no .sc")
        if r["stray_scm_in_rd"]:
            fails.append(f"{r['name']}: stray capture/profile files in the run dir")
        if r["name"].startswith("on") and not (r.get("scm_exists") and r["capture_banner"]):
            fails.append(f"{r['name']}: capture did not run")
        if r["name"].startswith("off") and r["capture_banner"]:
            fails.append(f"{r['name']}: capture ran in a capture-off run")
    off_runs = [r for r in runs if r["name"].startswith("off")]
    on_runs = [r for r in runs if r["name"].startswith("on")]
    off_shas = {r.get("sc_sha256") for r in off_runs}
    result: dict = {"qemu": str(qemu), "qemu_sha256": qemu_sha, "runs": runs}

    smoke = {}
    on_parsed = {}
    for r in on_runs:
        if not r.get("scm_exists"):
            continue
        sm = scm_smoke.run(r["scm"], str(out / "sc" / f"{r['name']}.sc"), False)
        smoke[r["name"]] = {"verdict": "PASS" if not sm.failures else "FAIL",
                            "failures": sm.failures[:20],
                            "tb_chunks": sm.stats.get("tb_chunks"),
                            "binding": sm.stats.get("binding")}
        if sm.failures:
            fails.append(f"{r['name']}: scm_smoke FAIL")
        on_parsed[r["name"]] = scm_sites(Path(r["scm"]))
    result["scm_smoke"] = smoke

    # code_address equality and MAPS(first_commit) equality
    scs = {r["name"]: read_sc(out / "sc" / f"{r['name']}.sc") for r in runs if "sc_sha256" in r}
    cas = {n: s["code_address"] for n, s in scs.items()}
    result["code_address"] = {n: hex(v) for n, v in cas.items()}
    if len(set(cas.values())) != 1:
        fails.append("code_address differs between runs")
    for n, (tbs, info) in on_parsed.items():
        if "run_info" in info and info["run_info"][2] != cas.get(n):
            fails.append(f"{n}: RUN_INFO code_address != .sc header")
    maps = {n: maps_layout(info.get("maps_first_commit") or b"") for n, (_, info) in on_parsed.items()}
    result["maps_first_commit_equal_across_on_runs"] = len(set(maps.values())) == 1
    if len(set(maps.values())) != 1:
        fails.append("MAPS(first_commit) layout differs between capture-on runs")
    if maps:
        result["scm_mapped"] = any(".scm" in line for line in next(iter(maps.values())))
        if result["scm_mapped"]:
            fails.append("the .scm file is mapped into QEMU")

    if len(off_shas) == 1 and None not in off_shas:
        mode = "whole_file"
        ref = next(iter(off_shas))
        for r in on_runs:
            if r.get("sc_sha256") != ref:
                fails.append(f"{r['name']}: sha256 {r.get('sc_sha256')} != capture-off {ref}")
        result["off_sha256"] = ref
    else:
        mode = "per_key"
        result["off_sha256"] = sorted(s for s in off_shas if s)
        cmp = {}
        for r in on_runs:
            n = r["name"]
            if n not in scs or n not in on_parsed:
                continue
            pe = on_parsed[n][1]["run_info"][4] if "run_info" in on_parsed[n][1] else 0
            for o in off_runs:
                if o["name"] in scs:
                    c = per_key_compare(scs[n], scs[o["name"]], on_parsed[n][0], pe)
                    cmp[f"{n}~{o['name']}"] = c
                    if c["mismatches"] or not c["common_keys"]:
                        fails.append(f"{n} vs {o['name']}: {c['mismatches']} mismatches "
                                     f"over {c['common_keys']} common keys")
        result["per_key"] = cmp
    result["mode"] = mode
    result["failures"] = fails
    result["verdict"] = "PASS" if not fails else "FAIL"
    text = json.dumps(result, indent=2, sort_keys=True)
    (out / "identity.json").write_text(text + "\n")
    print(text)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
