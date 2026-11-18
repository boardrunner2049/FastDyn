#!/usr/bin/env python3
"""WP5 smoke test of QEMU profile mode (``scp``) on the minimal fixture.

    qprof_smoke.py --qemu QEMU --out OUT_ROOT [--phases steady@0x08003972]
                   [--profile-runs 2] [--nochain-runs 2] [--skip-nochain]
    qprof_smoke.py --check FILE.qprof [--sc STAGED.sc]   # validate one file

Runs, each in its own fresh run directory under OUT_ROOT (own cache/, work/,
RAM files and QMP socket; QEMU under ``setarch x86_64 -R`` with a timeout;
``supercache_flat=false``):

1. ``cold``: empty cache, profile off -> the snapshot (records > 0).
2. ``warm``: sparse copy of the cold cache, every ``QEMU_SC_*`` unset ->
   appends 0 records, leaves the ``.sc`` untouched, normalized output equals
   B0, and no ``*.qprof`` appears.
3. ``profile-K``: sparse copy of the cold cache, ``QEMU_SC_PROFILE`` and
   ``QEMU_SC_PROFILE_PHASES`` set -> a valid ``.qprof`` (header, phase rows,
   TB rows, edge rows, trailer), ``clean_exit=1``, no overflow, the
   flow-conservation identities of design §3.4, rows joined to the staged
   ``.sc`` (§3.6), same normalized guest output.
4. ``nochain-K``: a direct ``-d exec,nochain`` run of the same binary
   (``log_options`` changed, so its own TOML and fresh cache) -> the number
   of ``Trace`` lines, compared with ``total_dispatches`` (exactly when the
   repeats show the workload is deterministic, otherwise reported with the
   relative difference), and per ``(cs_base, pc, flags)`` key.
5. ``mutex``: both ``QEMU_SC_CAPTURE`` and ``QEMU_SC_PROFILE`` set -> QEMU
   exits non-zero with the mutual-exclusion error and no ``.qprof``/``.scm``
   is created.
6. ``config-errors/*``: direct QEMU (``-M none``) for every rejection of
   ``scp_configure_from_env`` (no supercache, supercache-flat, relative path,
   phases without profile, bad phase specs, existing output file, capture
   and profile together) -> non-zero exit, the message, no file created.

The ``.qprof`` reader here is deliberately self-contained (it uses only the
frozen ``scopt.formats.layouts`` structs); the product reader is WP6's
``scopt.formats.qprof_v1``.  Writes ``OUT_ROOT/summary.json`` and exits 0
only if every check passed.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scopt.canon import fnv1a64  # noqa: E402
from scopt.formats import layouts as L  # noqa: E402
from scopt.formats.enums import QprofEdgeKind, QprofHeaderFlag  # noqa: E402

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FASTDYN_SRC = ROOT / "src/fastdyn"
FASTDYN_BIN = FASTDYN_SRC / "fastdyn-env/bin/fastdyn"
TEMPLATE = FASTDYN_SRC / "configs/b0-sc.toml"
EXPECTED_LOG = ROOT / "runs/b0/warm1.log"
U64 = 0xFFFF_FFFF_FFFF_FFFF
QSC_ENV = ("QEMU_SC_CAPTURE", "QEMU_SC_CAPTURE_LEVEL", "QEMU_SC_PROFILE", "QEMU_SC_PROFILE_PHASES")


# --------------------------------------------------------------------------
# .qprof v1 reader and checks
# --------------------------------------------------------------------------

class QprofError(Exception):
    pass


@dataclass
class Qprof:
    header: dict
    phases: list[dict]
    tbs: list[dict]
    edges: list[dict]
    trailer_ok: bool
    size: int

    @property
    def flags(self) -> QprofHeaderFlag:
        return QprofHeaderFlag(self.header["flags"])


_HDR_FIELDS = ("magic", "version", "header_size", "code_address", "runtime_signature",
               "loaded_record_count", "loaded_max_end", "loaded_header_checksum", "n_phases",
               "flags", "n_tb_rows", "n_edge_rows", "total_dispatches", "created_realtime_ns",
               "r0", "r1", "r2", "checksum")


def parse_qprof(data: bytes) -> Qprof:
    if len(data) < L.QPROF_HEADER.size:
        raise QprofError(f"short file ({len(data)} bytes)")
    h = dict(zip(_HDR_FIELDS, L.QPROF_HEADER.unpack_from(data, 0)))
    if h["magic"] != L.QPROF_MAGIC:
        raise QprofError(f"bad magic {h['magic']!r}")
    if h["version"] != L.QPROF_VERSION or h["header_size"] != L.QPROF_HEADER.size:
        raise QprofError(f"version/header_size {h['version']}/{h['header_size']}")
    if fnv1a64(data[: L.QPROF_HEADER_CHECKSUM_LEN]) != h["checksum"]:
        raise QprofError("header checksum mismatch")
    n_ph, n_tb, n_ed = h["n_phases"], h["n_tb_rows"], h["n_edge_rows"]
    row = L.qprof_tb_row_size(n_ph)
    body = L.QPROF_PHASE.size * n_ph + row * n_tb + L.QPROF_EDGE.size * n_ed
    expect = L.QPROF_HEADER.size + body + L.QPROF_TRAILER.size
    if len(data) != expect:
        raise QprofError(f"size {len(data)} != expected {expect} (provisional header or truncated?)")
    off = L.QPROF_HEADER.size
    phases = []
    for _ in range(n_ph):
        pid, reached, marker, fdi, name = L.QPROF_PHASE.unpack_from(data, off)
        off += L.QPROF_PHASE.size
        phases.append(dict(id=pid, reached=reached, marker=marker, first_dispatch_index=fdi,
                           name=name.split(b"\0", 1)[0].decode()))
    tbs = []
    for _ in range(n_tb):
        (tb_off, vaddr, pa0, pa1, cs_base, flags, cflags, size, icount, pad,
         r0, r1) = L.QPROF_TB_ROW.unpack_from(data, off)
        off += L.QPROF_TB_ROW.size
        per = []
        for _p in range(n_ph):
            e, a, lj, lh = L.QPROF_TB_PHASE.unpack_from(data, off)
            off += L.QPROF_TB_PHASE.size
            per.append(dict(entries=e, aborted=a, longjmp=lj, lookup_hits=lh))
        tbs.append(dict(tb_offset=tb_off, first_vaddr=vaddr, page_addr0=pa0, page_addr1=pa1,
                        cs_base=cs_base, flags=flags, cflags=cflags, size=size, icount=icount,
                        pad=pad, reserved=(r0, r1), phase=per))
    edges = []
    for _ in range(n_ed):
        src, dst, kind, flags, phase, count = L.QPROF_EDGE.unpack_from(data, off)
        off += L.QPROF_EDGE.size
        edges.append(dict(src=src, dst=dst, kind=kind, flags=flags, phase=phase, count=count))
    (trailer,) = L.QPROF_TRAILER.unpack_from(data, off)
    trailer_ok = fnv1a64(data[L.QPROF_HEADER.size: off]) == trailer
    return Qprof(h, phases, tbs, edges, trailer_ok, len(data))


@dataclass
class Checks:
    rows: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    def report(self, prefix: str = "") -> str:
        return "\n".join(f"{prefix}{'PASS' if ok else 'FAIL'} {n}: {d}" for n, ok, d in self.rows)


def check_qprof(q: Qprof, require_clean: bool = True) -> tuple[Checks, dict]:
    """Structural and flow-conservation checks (§3.4, §3.5)."""
    c = Checks()
    h = q.header
    c.add("trailer", q.trailer_ok, "FNV-1a-64 over the body")
    flags = q.flags
    if require_clean:
        c.add("clean_exit", QprofHeaderFlag.CLEAN_EXIT in flags, f"flags=0x{h['flags']:x}")
        c.add("no_overflow", not (flags & (QprofHeaderFlag.TB_OVERFLOW | QprofHeaderFlag.EDGE_OVERFLOW)),
              f"flags=0x{h['flags']:x}")
    c.add("reserved_zero", h["r0"] == h["r1"] == h["r2"] == 0 and all(
        t["pad"] == 0 and t["reserved"] == (0, 0) for t in q.tbs), "header and TB-row reserved")

    # phases
    ph = q.phases
    ok = bool(ph) and ph[0]["name"] == "boot" and ph[0]["marker"] == 0 and \
        all(p["id"] == i for i, p in enumerate(ph))
    c.add("phase_table", ok, f"{[(p['name'], hex(p['marker']), p['reached']) for p in ph]}")
    c.add("phase_reached_values", all(p["reached"] in (0, 1, 2) for p in ph), "0/1 exact_start/2 contained")
    reached = [p["reached"] != 0 for p in ph]
    c.add("phase_reached_prefix", reached == sorted(reached, reverse=True), "reached phases form a prefix")
    fdi = [p["first_dispatch_index"] for p in ph if p["reached"]]
    c.add("phase_first_dispatch_monotone", fdi == sorted(fdi) and (not fdi or fdi[0] == 0), f"{fdi}")

    # rows
    offs = [t["tb_offset"] for t in q.tbs]
    c.add("tb_offsets_unique", len(set(offs)) == len(offs), f"{len(offs)} rows")
    n_ph = len(ph)
    entries = {t["tb_offset"]: [p["entries"] for p in t["phase"]] for t in q.tbs}
    total = sum(sum(v) for v in entries.values())
    c.add("total_dispatches", total == h["total_dispatches"], f"sum(entries)={total} header={h['total_dispatches']}")
    c.add("sub_counts_le_entries", all(
        p["aborted"] <= p["entries"] and p["lookup_hits"] <= p["entries"] and p["longjmp"] <= p["entries"]
        for t in q.tbs for p in t["phase"]), "aborted, longjmp, lookup_hits <= entries")
    c.add("unreached_phase_zero", all(
        t["phase"][i]["entries"] == 0 for t in q.tbs for i in range(n_ph) if not ph[i]["reached"]),
        "no entries in unreached phases")

    # edges
    keyset = set()
    dup = 0
    bad_ref = 0
    first = [e for e in q.edges if e["kind"] == QprofEdgeKind.FIRST]
    for e in q.edges:
        k = (e["src"], e["dst"], e["kind"], e["flags"], e["phase"])
        dup += k in keyset
        keyset.add(k)
        if e["dst"] not in entries or (e["src"] != U64 and e["src"] not in entries):
            bad_ref += 1
    c.add("edge_keys_unique", dup == 0, f"{dup} duplicate keys")
    c.add("edge_refs", bad_ref == 0, f"{bad_ref} edges reference unknown rows")
    c.add("edge_kinds", all(e["kind"] in set(int(k) for k in QprofEdgeKind) for e in q.edges)
          and all(e["flags"] & ~0x7 == 0 for e in q.edges) and all(e["phase"] < n_ph for e in q.edges),
          "kind 1..7, flags bit0..2, phase < n_phases")
    if total:
        c.add("first_edge", len(first) == 1 and first[0]["count"] == 1 and first[0]["src"] == U64,
              f"{first}")
    c.add("none_src_only_first", all((e["src"] == U64) == (e["kind"] == QprofEdgeKind.FIRST) for e in q.edges),
          "src NONE <=> kind FIRST")

    # flow conservation (§3.4)
    inn: dict[tuple[int, int], int] = collections.Counter()
    out: dict[int, int] = collections.Counter()
    for e in q.edges:
        inn[(e["dst"], e["phase"])] += e["count"]
        if e["src"] != U64:
            out[e["src"]] += e["count"]
    in_bad = [(hex(t), p, inn[(t, p)], entries[t][p]) for t in entries for p in range(n_ph)
              if inn[(t, p)] != entries[t][p]]
    in_bad += [(hex(t), p, n, None) for (t, p), n in inn.items() if t not in entries]
    c.add("flow_in", not in_bad, f"sum in-edges(t,p) == entries(t,p) for {len(entries)}x{n_ph}; bad={in_bad[:5]}")
    deficit = {t: sum(entries[t]) - out[t] for t in entries}
    nonzero = {hex(t): d for t, d in deficit.items() if d != 0}
    last = [t for t, d in deficit.items() if d == 1]
    ok = (total == 0 and not nonzero) or (len(nonzero) == 1 and len(last) == 1)
    c.add("flow_out", ok, "sum out-edges(t) == entries(t) - [t is last] (summed over phases; "
          f"an edge carries its destination's phase); deficits={nonzero}")
    stats = dict(
        total_dispatches=h["total_dispatches"], n_tb_rows=len(q.tbs), n_edge_rows=len(q.edges),
        flags=h["flags"], phases=[dict(p) for p in ph],
        last_tb_offset=hex(last[0]) if last else None,
        edge_kind_counts={QprofEdgeKind(k).name: n for k, n in sorted(_sum_by(q.edges, "kind").items())},
        edge_flag_counts={hex(k): n for k, n in sorted(_sum_by(q.edges, "flags").items())},
        aborted=sum(p["aborted"] for t in q.tbs for p in t["phase"]),
        longjmp=sum(p["longjmp"] for t in q.tbs for p in t["phase"]),
        lookup_hits=sum(p["lookup_hits"] for t in q.tbs for p in t["phase"]),
        entries_per_phase=[sum(t["phase"][i]["entries"] for t in q.tbs) for i in range(n_ph)],
    )
    return c, stats


def _sum_by(edges, key):
    out = collections.Counter()
    for e in edges:
        out[e[key]] += e["count"]
    return out


# --------------------------------------------------------------------------
# .sc v5 (staged copy) and the §3.6 join
# --------------------------------------------------------------------------

def read_sc(path: Path) -> tuple[dict, list[dict]]:
    data = path.read_bytes()
    f = L.SC_HEADER.unpack_from(data, 0)
    names = ("magic", "version", "header_size", "code_size", "code_address", "prologue_end",
             "record_count", "runtime_signature", "host_page_size", "target_page_bits", "tb_size",
             "reserved", "checksum")
    h = dict(zip(names, f))
    base = L.SC_CODE_OFFSET + h["code_size"]
    recs = []
    for i in range(h["record_count"]):
        o = base + i * L.SC_RECORD.size
        if o + L.SC_RECORD.size > len(data):
            break
        v = L.SC_RECORD.unpack_from(data, o)
        rec = dict(slot=i, tb_offset=v[0], end_offset=v[1], tc_offset=v[2], tc_size=v[3], pc=v[4],
                   cs_base=v[5], page_addr0=v[6], page_addr1=v[7], guest_hash=v[8], flags=v[9],
                   cflags=v[10], guest_size=v[11], icount=v[12], checksum=v[17], committed=v[18])
        raw = data[o: o + 92]
        hh = fnv1a64(raw)
        rec["valid"] = (rec["committed"] == L.SC_RECORD_COMMIT and
                        rec["checksum"] == ((hh ^ (hh >> 32)) & 0xFFFFFFFF))
        recs.append(rec)
    return h, recs


def check_join(q: Qprof, sc_before: tuple[dict, list[dict]], sc_after: tuple[dict, list[dict]]) -> tuple[Checks, dict]:
    c = Checks()
    hb, rb = sc_before
    ha, ra = sc_after
    h = q.header
    c.add("hdr_code_address", h["code_address"] == hb["code_address"], f"{h['code_address']:#x} vs .sc {hb['code_address']:#x}")
    c.add("hdr_runtime_signature", h["runtime_signature"] == hb["runtime_signature"],
          f"{h['runtime_signature']:#x} vs .sc {hb['runtime_signature']:#x}")
    c.add("hdr_loaded_record_count", h["loaded_record_count"] == hb["record_count"],
          f"{h['loaded_record_count']} vs .sc before run {hb['record_count']}")
    c.add("hdr_loaded_header_checksum", h["loaded_header_checksum"] == hb["checksum"],
          f"{h['loaded_header_checksum']:#x} vs .sc before run {hb['checksum']:#x}")
    max_end = max((r["end_offset"] for r in rb if r["valid"]), default=0)
    c.add("hdr_loaded_max_end", h["loaded_max_end"] == max_end, f"{h['loaded_max_end']:#x} vs {max_end:#x}")

    loaded = {r["tb_offset"]: r for r in ra[: h["loaded_record_count"]] if r["valid"]}
    appended = {r["tb_offset"]: r for r in ra[h["loaded_record_count"]:] if r["valid"]}
    origin = collections.Counter()
    bad = []
    for t in q.tbs:
        key = (t["page_addr0"], t["page_addr1"], t["flags"], t["cs_base"], t["cflags"], t["size"], t["icount"])
        if t["page_addr0"] == U64:
            origin["TRANSIENT"] += 1
            continue
        r = loaded.get(t["tb_offset"])
        if r is not None:
            rk = (r["page_addr0"], r["page_addr1"], r["flags"], r["cs_base"], r["cflags"], r["guest_size"], r["icount"])
            if rk == key:
                origin["SNAPSHOT"] += 1
            else:
                bad.append((hex(t["tb_offset"]), "key mismatch vs slot", r["slot"]))
            continue
        r = appended.get(t["tb_offset"])
        if r is not None and t["tb_offset"] >= h["loaded_max_end"]:
            origin["PROFILE_ONLY"] += 1
            continue
        bad.append((hex(t["tb_offset"]), "no record", None))
    c.add("rows_join", not bad, f"origins={dict(origin)} unjoined={bad[:5]}")
    return c, dict(origins=dict(origin), appended_records=ha["record_count"] - hb["record_count"])


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def norm(text: str) -> str:
    pat = re.compile(r"^(Current CPU type|Reg [0-9]+:|\[BENCH\]|qemu-system-arm:|"
                     r"fastdyn\.targets\.qemu_target\|INFO\|  QEMU exited|fastdyn\.core\|INFO\|  FastDyn Exit)")
    out = []
    for line in text.splitlines():
        if pat.match(line):
            out.append(re.sub(r"^(\[BENCH\]\[[^]]*\]) elapsed_ns=.*", r"\1 elapsed=<host-time>", line))
    return "\n".join(out)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def file_id(p: Path) -> tuple | None:
    if not p.exists():
        return None
    st = p.stat()
    return (st.st_ino, st.st_mtime_ns, sha256(p))


class Runner:
    def __init__(self, qemu: Path, out: Path, timeout: int):
        self.qemu = qemu
        self.out = out
        self.timeout = timeout
        self.cur = out / "current"
        self.toml = out / "b0-sc.toml"
        self.toml_nochain = out / "b0-sc-nochain.toml"
        self.expected = norm(EXPECTED_LOG.read_text())
        tpl = TEMPLATE.read_text()
        old = re.search(r'^supercache\s*=\s*"(.*)/cache/minimal\.sc"', tpl, re.M).group(1)  # the B0 run dir the template names
        assert tpl.count(old) == 4, tpl.count(old)
        s = tpl.replace(old + "/cache/minimal.sc", f"{self.cur}/cache/minimal.sc").replace(old + "/", f"{self.cur}/")
        assert old not in s and "supercache_flat = false" in s
        self.toml.write_text(s)
        assert s.count('log_options = "none"') == 1
        self.toml_nochain.write_text(s.replace('log_options = "none"', 'log_options = "exec,nochain"'))

    def run(self, name: str, *, seed: Path | None = None, env_extra: dict | None = None,
            toml: Path | None = None) -> dict:
        rd = self.out / name
        if rd.exists():
            raise SystemExit(f"refusing to reuse {rd}")
        (rd / "cache").mkdir(parents=True)
        sc = rd / "cache/minimal.sc"
        if seed is not None:
            subprocess.run(["cp", "--sparse=always", str(seed / "minimal.sc"), str(sc)], check=True)
            shutil.copy(seed / "minimal.sc.fastdyn.json", rd / "cache/minimal.sc.fastdyn.json")
        if self.cur.is_symlink() or self.cur.exists():
            self.cur.unlink()
        self.cur.symlink_to(rd)
        env = {k: v for k, v in os.environ.items() if k not in QSC_ENV}
        env["FASTDYN_QEMU"] = str(self.qemu)
        env.update({k: str(v) for k, v in (env_extra or {}).items()})
        pre_sc = read_sc(sc) if sc.exists() else None
        pre_id = file_id(sc)
        cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10", str(self.timeout),
               str(FASTDYN_BIN), "run", "-c", str(toml or self.toml), "-o", str(rd / "work")]
        with open(rd / "stdout.log", "wb") as f:
            rc = subprocess.run(cmd, cwd=FASTDYN_SRC, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
        self.cur.unlink()
        log = (rd / "stdout.log").read_text(errors="replace")
        m = re.search(r"QEMU exited with code (-?\d+)", log)
        res = dict(name=name, dir=str(rd), rc=rc, qemu_rc=int(m.group(1)) if m else None,
                   env={k: str(v) for k, v in (env_extra or {}).items()},
                   norm_matches_b0=norm(log) == self.expected,
                   records_pre=pre_sc[0]["record_count"] if pre_sc else None,
                   records_post=read_sc(sc)[0]["record_count"] if sc.exists() else None,
                   sc_untouched=(pre_id is not None and pre_id == file_id(sc)),
                   stray=sorted(str(p.relative_to(rd)) for p in rd.rglob("*") if p.suffix in (".qprof", ".scm")),
                   bench=(re.search(r"elapsed_us=[0-9.]+", log) or [None])[0])
        res["_pre_sc"] = pre_sc
        (rd / "guest.norm").write_text(norm(log) + "\n")
        return res


def trace_counts(log: Path) -> tuple[int, collections.Counter]:
    per = collections.Counter()
    n = 0
    rx = re.compile(r"^Trace \d+: 0x[0-9a-f]+ \[([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)\]")
    with open(log, errors="replace") as f:
        for line in f:
            if line.startswith("Trace "):
                n += 1
                m = rx.match(line)
                if m:
                    per[(int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16))] += 1
    return n, per


def profile_key_counts(q: Qprof) -> collections.Counter:
    per = collections.Counter()
    for t in q.tbs:
        per[(t["cs_base"], t["first_vaddr"], t["flags"])] += sum(p["entries"] for p in t["phase"])
    return per


def config_error_cases(qemu: Path, root: Path, timeout: int):
    """scp_configure_from_env() rejections: QEMU must exit non-zero with the
    message and create neither the .qprof nor the cache."""
    root.mkdir(parents=True)
    existing = root / "existing.qprof"
    existing.write_bytes(b"")
    cases = [
        # name, accel options (cache placeholder {sc}), env, expected message
        ("no_supercache", "", {"QEMU_SC_PROFILE": "{d}/p.qprof"}, "no supercache is configured"),
        ("flat", ",supercache={sc},supercache-flat=on", {"QEMU_SC_PROFILE": "{d}/p.qprof"},
         "does not support supercache-flat"),
        ("relative_path", ",supercache={sc}", {"QEMU_SC_PROFILE": "p.qprof"}, "must be an absolute path"),
        ("phases_without_profile", ",supercache={sc}", {"QEMU_SC_PROFILE_PHASES": "steady@0x08003972"},
         "QEMU_SC_PROFILE_PHASES is set without QEMU_SC_PROFILE"),
        ("phase_marker_not_hex", ",supercache={sc}",
         {"QEMU_SC_PROFILE": "{d}/p.qprof", "QEMU_SC_PROFILE_PHASES": "steady@123"}, "0x-prefixed hex"),
        ("phase_name_too_long", ",supercache={sc}",
         {"QEMU_SC_PROFILE": "{d}/p.qprof", "QEMU_SC_PROFILE_PHASES": "a234567890123456@0x10"},
         "1..15 bytes"),
        ("too_many_phases", ",supercache={sc}",
         {"QEMU_SC_PROFILE": "{d}/p.qprof",
          "QEMU_SC_PROFILE_PHASES": ",".join(f"p{i}@0x{i:x}" for i in range(8))}, "too many phases"),
        ("existing_file", ",supercache={sc}", {"QEMU_SC_PROFILE": str(existing)}, "cannot create"),
        ("mutual_exclusion", ",supercache={sc}",
         {"QEMU_SC_PROFILE": "{d}/p.qprof", "QEMU_SC_CAPTURE": "{d}/c.scm"}, "mutually exclusive"),
    ]
    for name, accel, env_extra, expect in cases:
        d = root / name
        d.mkdir()
        sc = d / "cache.sc"
        env = {k: v for k, v in os.environ.items() if k not in QSC_ENV}
        env.update({k: v.format(d=d) for k, v in env_extra.items()})
        cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=KILL", str(timeout), str(qemu),
               "-M", "none", "-display", "none", "-nodefaults", "-S",
               "-accel", "tcg" + accel.format(sc=sc)]
        p = subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True)
        (d / "stderr.log").write_text(p.stderr)
        created = sorted(x.name for x in d.iterdir() if x.name != "stderr.log")
        ok = p.returncode not in (0, -9, 124, 137) and expect in p.stderr and not created
        yield name, ok, f"rc={p.returncode} created={created} stderr={p.stderr.strip()[:160]!r}"


def strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--qemu", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--phases", default="steady@0x08003972")
    ap.add_argument("--profile-runs", type=int, default=2)
    ap.add_argument("--nochain-runs", type=int, default=2)
    ap.add_argument("--skip-nochain", action="store_true")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("SMOKE_TIMEOUT", "120")))
    ap.add_argument("--check", type=Path, help="only validate this .qprof file")
    ap.add_argument("--sc", type=Path, help="with --check: the staged .sc after the run")
    a = ap.parse_args()

    if a.check:
        q = parse_qprof(a.check.read_bytes())
        c, stats = check_qprof(q)
        print(c.report())
        print(json.dumps(stats, indent=1, default=str))
        return 0 if c.ok else 1

    if not a.qemu or not a.out:
        ap.error("--qemu and --out are required")
    qemu = a.qemu.resolve()
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; pick a fresh OUT_ROOT")
    out.mkdir(parents=True, exist_ok=True)
    r = Runner(qemu, out, a.timeout)
    summary: dict = dict(qemu=str(qemu), qemu_sha256=sha256(qemu), phases=a.phases, runs=[], checks=[])
    top = Checks()

    def record(res: dict) -> dict:
        summary["runs"].append(strip(res))
        print(f"{res['name']}: rc={res['rc']} qemu_rc={res['qemu_rc']} records {res['records_pre']}->"
              f"{res['records_post']} untouched={res['sc_untouched']} norm_b0={res['norm_matches_b0']} "
              f"stray={res['stray']} {res['bench']}", flush=True)
        return res

    cold = record(r.run("cold"))
    top.add("cold", cold["rc"] == 0 and cold["records_pre"] is None and (cold["records_post"] or 0) > 0
            and cold["norm_matches_b0"], f"records={cold['records_post']}")
    seed = out / "cold/cache"

    warm = record(r.run("warm", seed=seed))
    top.add("warm_profile_unset", warm["rc"] == 0 and warm["records_post"] == warm["records_pre"]
            and warm["sc_untouched"] and warm["norm_matches_b0"] and not warm["stray"],
            f"appended={warm['records_post'] - warm['records_pre']} untouched={warm['sc_untouched']} "
            f"norm_b0={warm['norm_matches_b0']} stray={warm['stray']}")

    profiles = []
    for k in range(a.profile_runs):
        name = f"profile-{k}"
        qp = out / name / "run.qprof"
        res = record(r.run(name, seed=seed, env_extra={"QEMU_SC_PROFILE": qp, "QEMU_SC_PROFILE_PHASES": a.phases}))
        log = (Path(res["dir"]) / "stdout.log").read_text(errors="replace")
        top.add(f"{name}_run", res["rc"] == 0 and res["qemu_rc"] == 0 and res["norm_matches_b0"]
                and "supercache-profile: path=" in log,
                f"rc={res['rc']} qemu_rc={res['qemu_rc']} norm_b0={res['norm_matches_b0']}")
        if not qp.exists():
            top.add(f"{name}_qprof_exists", False, str(qp))
            continue
        try:
            q = parse_qprof(qp.read_bytes())
        except QprofError as e:
            top.add(f"{name}_qprof_parse", False, str(e))
            continue
        c, stats = check_qprof(q)
        sc_after = read_sc(Path(res["dir"]) / "cache/minimal.sc")
        cj, jstats = check_join(q, res["_pre_sc"], sc_after)
        stats.update(jstats)
        for n, ok, d in c.rows + cj.rows:
            top.add(f"{name}.{n}", ok, d)
        # phase 'steady' reached at the marker (T-B1-6 informative)
        hot = [t for t in q.tbs if t["first_vaddr"] <= 0x08018766 < t["first_vaddr"] + t["size"]]
        stats["tb_0x08018766"] = [dict(first_vaddr=hex(t["first_vaddr"]),
                                       entries=[p["entries"] for p in t["phase"]]) for t in hot]
        stats["qprof_sha256"] = sha256(qp)
        profiles.append((name, q, stats))
        summary.setdefault("profiles", {})[name] = stats
        print(f"  {name}: total_dispatches={stats['total_dispatches']} rows={stats['n_tb_rows']} "
              f"edges={stats['n_edge_rows']} origins={stats['origins']} appended={stats['appended_records']} "
              f"per_phase={stats['entries_per_phase']} kinds={stats['edge_kind_counts']}", flush=True)

    if profiles:
        totals = {n: s["total_dispatches"] for n, _, s in profiles}
        keyc = [profile_key_counts(q) for _, q, _ in profiles]
        prof_det = len(set(totals.values())) == 1 and all(k == keyc[0] for k in keyc)
        summary["profile_deterministic"] = prof_det
        top.add("profile_appended_zero", all(s["appended_records"] == 0 for _, _, s in profiles),
                f"{[s['appended_records'] for _, _, s in profiles]}")

    if not a.skip_nochain and profiles:
        traces = []
        for k in range(a.nochain_runs):
            name = f"nochain-{k}"
            res = record(r.run(name, toml=r.toml_nochain))
            n, per = trace_counts(Path(res["dir"]) / "work/qemu.log")
            traces.append((n, per))
            summary.setdefault("nochain", {})[name] = dict(trace_lines=n, rc=res["rc"],
                                                           norm_b0=res["norm_matches_b0"])
            print(f"  {name}: Trace lines={n}", flush=True)
        nc_det = len({n for n, _ in traces}) == 1 and all(p == traces[0][1] for _, p in traces)
        det = nc_det and summary["profile_deterministic"]
        summary["nochain_deterministic"] = nc_det
        summary["determinism_mode"] = "exact" if det else "tolerance"
        q0 = profiles[0][1]
        n0, per0 = traces[0]
        total = q0.header["total_dispatches"]
        pk = profile_key_counts(q0)
        key_diff = {f"{k[1]:#x}/{k[2]:#x}": (pk.get(k, 0), per0.get(k, 0))
                    for k in set(pk) | set(per0) if pk.get(k, 0) != per0.get(k, 0)}
        summary["trace_vs_profile"] = dict(trace_lines=n0, total_dispatches=total,
                                           key_mismatches=len(key_diff), examples=dict(list(key_diff.items())[:10]))
        if det:
            top.add("total_dispatches_eq_trace", total == n0 and all(n == n0 for n, _ in traces),
                    f"total_dispatches={total} Trace lines={[n for n, _ in traces]} (deterministic: exact)")
            top.add("per_key_eq_trace", not key_diff, f"{len(key_diff)} (cs_base,pc,flags) keys differ")
        else:
            rel = abs(total - n0) / max(n0, 1)
            top.add("total_dispatches_eq_trace", rel <= 0.01,
                    f"total_dispatches={total} Trace lines={[n for n, _ in traces]} (non-deterministic: "
                    f"rel diff {rel:.4%}, tolerance 1%)")

    # mutual exclusion
    res = record(r.run("mutex", seed=seed, env_extra={
        "QEMU_SC_PROFILE": out / "mutex/run.qprof", "QEMU_SC_CAPTURE": out / "mutex/run.scm"}))
    log = (Path(res["dir"]) / "stdout.log").read_text(errors="replace")
    msg = [ln for ln in log.splitlines() if "mutually exclusive" in ln]
    top.add("mutual_exclusion", res["qemu_rc"] not in (None, 0) and bool(msg) and not res["stray"]
            and not (out / "mutex/run.qprof").exists() and not (out / "mutex/run.scm").exists(),
            f"qemu_rc={res['qemu_rc']} msg={msg[:1]} stray={res['stray']}")

    # configuration errors, direct QEMU (-M none), each in its own directory
    for name, ok, detail in config_error_cases(qemu, out / "config-errors", a.timeout):
        top.add(f"config_error.{name}", ok, detail)

    summary["checks"] = [dict(name=n, ok=ok, detail=d) for n, ok, d in top.rows]
    summary["verdict"] = "PASS" if top.ok else "FAIL"
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    print(top.report("  "))
    print(f"verdict={summary['verdict']}")
    return 0 if top.ok else 1


if __name__ == "__main__":
    sys.exit(main())
