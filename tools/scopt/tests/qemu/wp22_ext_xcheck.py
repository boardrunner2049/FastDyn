#!/usr/bin/env python3
"""WP22 integration: the loader against WP18's model of it, on WP18-written files.

    wp22_ext_xcheck.py --qemu QEMU --artifact ART_DIR --config TOML --input BIN --out OUT

``ART_DIR`` is a v6 artifact published by WP18's ``package_b3`` (e.g. the
``b3-14`` artifact of ``wp22_stress.py``: SPEC and GENERAL slots, a variant of
a grown unit). Each mutant below changes one field of its header or extension
table (decoded with ``scopt.formats.sc_v6.ExtTable`` and re-encoded, so the
ext checksum is recomputed unless the mutant is about the checksum; header
mutants recompute the header checksum). For every mutant:

* WP18's prediction: ``read_sc6(mutant).problem`` = ``(code, loader message)``
  (``scopt.formats.sc_v6.ext_problem``, the Python statement of the loader's
  checks in order);
* the loader: the mutant is deployed with the scopt harness and run on QEMU;
  it must exit non-zero before any guest output with
  ``supercache: invalid v6 extension: <message>``, and the deployed file must
  be unchanged (sparse sha256, size, inode: no truncate, R2);
* the two messages must be equal (V11 statement vs the C loader).

The unmodified artifact is the control (exit 0, ext loaded). Writes
``OUT/ext-xcheck.json``; nothing is timed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from scopt.formats.sc_v5 import ScHeader, sha256_sparse  # noqa: E402
from scopt.formats.sc_v6 import (EXT_OFFSET, KIND_GENERAL, ExtTable, read_sc6)  # noqa: E402
from scopt.run.deploy import deploy_new  # noqa: E402
from scopt.run.launcher import file_sha256, launch  # noqa: E402

GAP_END = 0x10000
GUEST_RE = re.compile(r"^(Reg [0-9]+:|\[BENCH\])")
MSG_RE = re.compile(r"supercache: invalid v6 extension: (.*)$")


def read_ext(path: Path) -> ExtTable:
    f = read_sc6(path)
    if f.ext is None or f.problem is not None:
        raise SystemExit(f"{path}: not a loadable v6 file ({f.problem})")
    # decode() keeps the stored counts, max_end and checksum; a mutant must have
    # them recomputed from its entries (else every mutant is a checksum mismatch)
    ext = replace(f.ext, n_variants=None, n_slots=None, n_links=None, variants_max_end=None,
                  checksum=None)
    if ext.encode() != f.ext.encode():
        raise SystemExit(f"{path}: the normalized table does not re-encode to the file's bytes")
    return ext


def write_ext(path: Path, ext: ExtTable) -> None:
    enc = ext.encode()
    with open(path, "r+b") as fh:
        fh.seek(EXT_OFFSET)
        fh.write(bytes(GAP_END - EXT_OFFSET))
        fh.seek(EXT_OFFSET)
        fh.write(enc)


def write_features(path: Path, fn: Callable[[int], int]) -> None:
    with open(path, "r+b") as fh:
        h = ScHeader.from_bytes(fh.read(80))
        h = h.replace(reserved=fn(h.reserved)).with_checksum()
        fh.seek(0)
        fh.write(h.to_bytes())


def _v(ext: ExtTable, i: int, **kw: Any) -> ExtTable:
    vs = list(ext.variants)
    vs[i] = replace(vs[i], **kw)
    return replace(ext, variants=tuple(vs))


def _s(ext: ExtTable, i: int, **kw: Any) -> ExtTable:
    ss = list(ext.slots)
    ss[i] = replace(ss[i], **kw)
    return replace(ext, slots=tuple(ss))


def _l(ext: ExtTable, i: int, **kw: Any) -> ExtTable:
    ls = list(ext.links)
    ls[i] = replace(ls[i], **kw)
    return replace(ext, links=tuple(ls))


def mutants(ext: ExtTable, records: list[Any]) -> dict[str, Any]:
    """name -> ("hdr", fn) | ("ext", ExtTable)."""
    spec = [i for i, s in enumerate(ext.slots) if s.kind != KIND_GENERAL]
    gen = [i for i, s in enumerate(ext.slots) if s.kind == KIND_GENERAL]
    rec0 = records[0]
    v0 = ext.variants[0]
    m: dict[str, Any] = {
        "hdr-unknown-feature-bit": ("hdr", lambda f: f | 0x10),
        "hdr-ext-table-clear": ("hdr", lambda f: f & ~1),
        "hdr-variants-bit-clear": ("hdr", lambda f: f & ~2),
        "hdr-slots-bit-clear": ("hdr", lambda f: f & ~4),
        "ext-magic": ("ext", replace(ext, magic=b"QSCX6EXX")),
        "ext-version": ("ext", replace(ext, ext_version=2)),
        "ext-header-size": ("ext", replace(ext, ext_header_size=72)),
        "ext-entry-sizes": ("ext", replace(ext, variant_entry_size=72)),
        "ext-reserved": ("ext", replace(ext, reserved1=1)),
        "ext-checksum": ("ext", replace(ext, checksum=(ext.header_bytes()[56:] and
                                                       int.from_bytes(ext.header_bytes()[56:64],
                                                                      "little") ^ 1))),
        "ext-artifact-id-zero": ("ext", replace(ext, artifact_id8=b"\0" * 8)),
        "ext-max-end": ("ext", replace(ext, variants_max_end=ext.computed_max_end + 64)),
        "ext-n-links-short": ("ext", replace(ext, links=ext.links[:-1])),
        "variant-tb-misaligned": ("ext", _v(ext, 0, tb_offset=v0.tb_offset + 8)),
        "variant-tc-size": ("ext", _v(ext, 0, tc_size=v0.tc_size + 1)),
        "variant-end": ("ext", _v(ext, 0, end_offset=v0.end_offset + 4)),
        "variant-general-not-record": ("ext", _v(ext, 0, general_tb_offset=v0.general_tb_offset
                                                 + 64)),
        "variant-overlaps-record": ("ext", _v(ext, 0, tb_offset=rec0.tb_offset,
                                              tc_offset=rec0.tb_offset + (v0.tc_offset -
                                                                          v0.tb_offset),
                                              end_offset=rec0.tb_offset + (v0.end_offset -
                                                                           v0.tb_offset))),
        "variant-code-hash": ("ext", _v(ext, 0, code_fnv64=v0.code_fnv64 ^ 1)),
        "variant-id": ("ext", _v(ext, 0, variant_id=7)),
        "variant-flags": ("ext", _v(ext, 0, flags=2)),
        "slot-src-not-record": ("ext", _s(ext, spec[0], src_tb_offset=ext.slots[spec[0]]
                                          .src_tb_offset + 64)),
        "slot-kind": ("ext", _s(ext, spec[0], kind=3)),
        "slot-exit-class": ("ext", _s(ext, spec[0], exit_class=0)),
        "slot-guard-hash": ("ext", _s(ext, spec[0], guard_fnv64=ext.slots[spec[0]].guard_fnv64
                                      ^ 1)),
        "slot-guard-off": ("ext", _s(ext, spec[0], guard_off=ext.slots[spec[0]].guard_off + 1)),
        "slot-vc-page-offset": ("ext", _s(ext, spec[0], target_vaddr=ext.slots[spec[0]]
                                          .target_vaddr + 2)),
        "link-variant-range": ("ext", _l(ext, 0, variant_index=len(ext.variants))),
        "link-slot-range": ("ext", _l(ext, 0, slot_index=len(ext.slots))),
    }
    if gen:
        m["link-to-general-slot"] = ("ext", _l(ext, 0, slot_index=gen[0]))
    if len(ext.links) >= 2:
        m["link-duplicate-variant"] = ("ext", _l(ext, 1, variant_index=ext.links[0]
                                                 .variant_index))
    if len(ext.slots) >= 2:
        m["slot-duplicate"] = ("ext", _s(ext, 1, src_tb_offset=ext.slots[0].src_tb_offset,
                                         slot_n=ext.slots[0].slot_n))
    return m


def run_one(sc: Path, toml: Path, inp: Path, rd_path: Path, qemu: Path,
            timeout: float) -> dict[str, Any]:
    rd, _ = deploy_new(sc, toml, rd_path, qemu=qemu)
    before = {"sha256": sha256_sparse(rd.cache), "size": rd.cache.stat().st_size,
              "inode": rd.cache.stat().st_ino}
    r = launch(rd, timeout_s=timeout, inputs={"input": str(inp)}, prove=False,
               extra_env={"QEMU_SC_V6_STATS": str(rd.root / "v6-stats.json")})
    text = Path(r["stdout_log"]).read_text(errors="replace")
    after = {"sha256": sha256_sparse(rd.cache) if rd.cache.exists() else None,
             "size": rd.cache.stat().st_size if rd.cache.exists() else None,
             "inode": rd.cache.stat().st_ino if rd.cache.exists() else None}
    msgs = [m.group(1).strip() for m in map(MSG_RE.search, text.splitlines()) if m]
    return {"run_dir": str(rd.root), "exit_code": r["exit_code"], "messages": msgs,
            "guest_lines": sum(1 for ln in text.splitlines() if GUEST_RE.match(ln)),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--artifact", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args()
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    (out / "files").mkdir(parents=True)
    src = a.artifact / "cache.sc"
    f0 = read_sc6(src)
    ext = read_ext(src)
    records = [r for r in f0.sc.records if r is not None]
    if not records:
        raise SystemExit("no records")
    doc: dict[str, Any] = {"schema": "wp22.ext-xcheck/1", "qemu": str(a.qemu),
                           "qemu_sha256": file_sha256(a.qemu), "artifact": str(a.artifact),
                           "source_sha256": sha256_sparse(src), "cases": {}}
    ctl = run_one(src, a.config, a.input, out / "runs/control", a.qemu, a.timeout)
    st = out / "runs/control/v6-stats.json"
    ctl["stats_loaded"] = json.loads(st.read_text()).get("loaded") if st.is_file() else None
    ctl_ok = ctl["exit_code"] == 0 and not ctl["messages"] and ctl["stats_loaded"] is True
    doc["control"] = {"ok": ctl_ok, **ctl}
    print(f"[{'PASS' if ctl_ok else 'FAIL'}] control exit={ctl['exit_code']}", flush=True)
    ok_all = ctl_ok
    for name, (kind, what) in mutants(ext, records).items():
        p = out / "files" / f"{name}.sc"
        subprocess.run(["cp", "--sparse=always", str(src), str(p)], check=True)
        p.chmod(0o600)
        if kind == "hdr":
            write_features(p, what)
        else:
            write_ext(p, what)
        pred = read_sc6(p).problem
        r = run_one(p, a.config, a.input, out / "runs" / name, a.qemu, a.timeout)
        items = [("WP18 model refuses the mutant", pred is not None, pred),
                 ("the mutant reaches its own check (not a checksum mismatch)",
                  pred is not None and (pred[0] != "ext_checksum") == (name != "ext-checksum"),
                  pred and pred[0]),
                 ("QEMU exits non-zero", r["exit_code"] not in (0, None), r["exit_code"]),
                 ("exactly one 'invalid v6 extension' message", len(r["messages"]) == 1,
                  r["messages"]),
                 ("loader message == WP18 model message", pred is not None and
                  r["messages"][:1] == [pred[1]], (r["messages"][:1], pred and pred[1])),
                 ("no guest output (refused before the machine runs)", r["guest_lines"] == 0,
                  r["guest_lines"]),
                 ("file unchanged (no truncate)", r["before"] == r["after"],
                  (r["before"], r["after"]))]
        ok = all(x for _, x, _ in items)
        ok_all &= ok
        doc["cases"][name] = {"ok": ok, "predicted": pred,
                              "checks": [{"check": c, "ok": bool(x), "detail": d}
                                         for c, x, d in items], "run": r}
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: model={pred and pred[0]} "
              f"loader={r['messages'][:1]}", flush=True)
        if r["run_dir"]:
            for ram in Path(r["run_dir"]).rglob("*.ram"):
                ram.unlink()
    doc["verdict"] = "PASS" if ok_all else "FAIL"
    doc["n_mutants"] = len(doc["cases"])
    (out / "ext-xcheck.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    print(f"verdict {doc['verdict']} ({doc['n_mutants']} mutants) -> {out / 'ext-xcheck.json'}")
    shutil.rmtree(out / "files", ignore_errors=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
