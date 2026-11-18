"""F-B2-3 fault attribution acceptance (design §7, WP15B).

Two kinds of tests:

* synthetic tests of the driver's pure helpers (``tests/qemu/b2_fault.py``):
  config rendering, faulting-unit lookup, the largest-feasible-negative
  placement, the ``-d exec,int`` log parser and the printed-value parser;
* ``test_evidence_*``: read what a driver run wrote
  (``b2_fault.py --out $B2_FAULT_DIR``; default ``runs/wp15b/fault``) and re-check
  the F-B2-3 facts from the raw files (``runs.jsonl``, the artifacts' sidecars and
  ``validation.json``, the diag ``qemu.log``, the ELF symbol table), not only from
  ``fault.json``. Skipped when that directory is absent.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
FIX = ROOT / "fixtures/fault"
OUT = Path(os.environ.get("B2_FAULT_DIR", ROOT / "runs/wp15b/fault"))
WT = Path(__file__).resolve().parents[2]
INT_QEMU_SHA = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
MIB = 1 << 20

_spec = importlib.util.spec_from_file_location("b2_fault", WT / "tests/qemu/b2_fault.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


# ---------------------------------------------------------------------------
# synthetic
# ---------------------------------------------------------------------------

def test_render_config_matches_fdrun():
    if not (FIX / "fault-a.toml.in").is_file():
        pytest.skip("fault fixture not present")
    t = (FIX / "fault-a.toml.in").read_text()
    q = "/q/qemu-system-arm"
    no = bf.render_config(t, "a", q, "noicount")
    ic = bf.render_config(t, "a", q, "icount")
    assert 'supercache = "cache/fault-a.sc"\nsupercache_flat = false\n' in no
    assert f'qemu_path = "{q}"' in no and "@" not in no.split("[Device]")[0]
    assert bf.ICOUNT_LINE not in no and bf.ICOUNT_LINE in ic
    # byte-equal to the rendering WP12's fdrun.py produced for the integrated binary
    ref = ROOT / "runs/wp12-rev2/fault-qual-qint/noicount/a/cold/fastdyn.toml"
    if ref.is_file():
        assert bf.render_config(t, "a", str(bf.INT_QEMU), "noicount") == ref.read_text()


def test_units_containing():
    us = [{"uid": "x", "pc": 0xc4, "guest_size": 38}, {"uid": "y", "pc": 0xd4, "guest_size": 22},
          {"uid": "z", "pc": 0xda + 2, "guest_size": 4}]
    assert [u["uid"] for u in bf.units_containing(us, 0xda)] == ["x", "y"]
    assert bf.units_containing(us, 0xc4 + 38 + 100) == []


def test_minusmax_moves_packs_from_prologue_and_evicts_occupants():
    layout = [("u0", 0x40, 0x280), ("u1", 0x280, 0x400), ("u2", 0x400, 0x570),
              ("f1", 0x2940, 0x2b60), ("f2", 0x2b80, 0x2d60), ("u3", 0x2d80, 0x3000)]
    fm, ev = bf.minusmax_moves(layout, ["f1", "f2"], prologue_end=45)
    assert fm == {"f1": 0x40, "f2": 0x280}           # ROUND_UP(45, 64) = 0x40, then packed
    assert ev == {"u0": 0x3000, "u1": 0x3240, "u2": 0x33c0}  # occupants -> ROUND_UP(max_end)
    new = {u: (tb, end) for u, tb, end in layout}
    for u, o in {**fm, **ev}.items():
        tb, end = new[u]
        new[u] = (o, o + end - tb)
    iv = sorted(new.values())
    assert all(a[1] <= b[0] for a, b in zip(iv, iv[1:])), iv   # disjoint
    assert all(tb % 64 == 0 and tb >= 0x40 for tb, _ in iv)


def test_parse_diag_log_attributes_exception_to_last_trace():
    log = "\n".join([
        "Trace 0: 0x7fff6c000100 [00800400/0000000000000040/00000110/ff020000] Reset_Handler",
        "cpu_io_recompile: rewound execution of TB to 0000000000000122",
        "Trace 0: 0x7fff70002c40 [00800400/0000000000000122/00000510/ff031001] variant_d",
        "Trace 0: 0x7fff70002c40 [00800400/0000000000000122/00000510/ff031001] variant_d",
        "Linking TBs 0x7fff70002c40 index 0 -> 0x7fff70002c40",
        "Taking exception 4 [Data Abort] on CPU 0",
        "...at fault address 0x20000001",
        "...really UsageFault with UFSR.UNALIGNED",
        "Linking TBs 0x1 index 0 -> 0x2",
        "Taking exception 8 [QEMU v7M exception exit] on CPU 0",
    ])
    p = bf.parse_diag_log(log)
    assert [e["excp"] for e in p["exceptions"]] == [4, 8]
    e = p["exceptions"][0]
    assert e["lines"] == ["Taking exception 4 [Data Abort] on CPU 0",
                          "...at fault address 0x20000001",
                          "...really UsageFault with UFSR.UNALIGNED"]
    assert e["trace_before"]["tc"] == 0x7fff70002c40 and e["trace_before"]["pc"] == 0x122
    assert p["rewinds"] == {"0x122": 1}
    k = "pc=0x122/flags=0x510/cs_base=0x800400/cflags=0xff031001"
    assert p["keys"][k] == 2 and p["tcs"][k] == [0x7fff70002c40]


def test_fault_reports_on_expected_norms():
    if not (FIX / "expected/fixture.norm").is_file():
        pytest.skip("fault fixture not present")
    regs = bf.reg_lines((FIX / "expected/fixture.norm").read_text().splitlines())
    r = bf.fault_reports(regs)
    assert [(x["variant"], x["pc"], x["ipsr"]) for x in r] == [(0xA, 0xDA, 6), (0xB, 0xFA, 6),
                                                                (0xC, 0x10C, 5)]
    s = bf.compact_values(regs)
    assert "(a) pc=0xda" in s and "(c) pc=0x10c" in s and "(d) cpuid=0x410fc231 n=64" in s
    d = bf.reg_lines((FIX / "expected/d.norm").read_text().splitlines())
    assert bf.fault_reports(d) == [] and bf.compact_values(d).startswith("(d) ")


# ---------------------------------------------------------------------------
# evidence of a driver run
# ---------------------------------------------------------------------------

def _summary() -> dict:
    p = OUT / "b2-fault.json"
    if not p.is_file():
        pytest.skip(f"{p} not present (run tests/qemu/b2_fault.py --out {OUT})")
    return json.loads(p.read_text())


def _workloads() -> list[str]:
    p = OUT / "b2-fault.json"
    if not p.is_file():
        return ["noicount/a"]
    return sorted(json.loads(p.read_text())["workloads"])


@pytest.mark.real
def test_evidence_summary():
    s = _summary()
    assert s["qemu_sha256"] == INT_QEMU_SHA
    gated = [k for k in s["workloads"] if k.startswith("noicount/")]
    assert sorted(gated) == [f"noicount/{v}" for v in ("a", "b", "c", "d", "fixture")]
    for k in gated:
        assert s["workloads"][k]["verdict"] == "pass", (k, s["workloads"][k]["failed"])


@pytest.mark.real
@pytest.mark.parametrize("wl", _workloads())
def test_evidence_workload(wl):
    _summary()
    d = OUT / wl
    doc = json.loads((d / "fault.json").read_text())
    image = doc["image"]
    syms = bf.elf_symbols(FIX / f"fault_{image}.elf")
    variants = bf.IMAGES[image]
    # printed values: identical in every run of every arm and equal to the expected .norm
    rows = [json.loads(x) for x in (d / "runs/runs.jsonl").read_text().splitlines()]
    assert {r["arm"] for r in rows} == set(bf.ARMS)
    exp = bf.reg_lines((FIX / "expected" / f"{image}.norm").read_text().splitlines())
    for r in rows:
        assert r["valid"] and r["exit_code"] == 0, r["run_dir"]
        assert bf.reg_lines(r["guest_norm"]) == exp, (r["arm"], r["run_dir"])
        if r["arm"] != "nocache":
            assert r["verdict"] == "PASS" and r["appended"] == 0, r["run_dir"]
    # stacked PC == the ELF address of the faulting insn (symbol table read here)
    got = {x["variant"]: x["pc"] for x in bf.fault_reports(exp)}
    for v in variants:
        sym, vid = bf.VARIANT_SYMS[v]
        if vid:
            assert got[vid] == syms[sym] & ~1, v
    # the faulting units, moved by the artifacts as claimed (read from the sidecars)
    fu = set(doc["faulting_units"])
    assert fu
    arts = doc["artifacts"]
    for name, want in (("plus64", 64 * MIB), ("minus64c", -64 * MIB)):
        A = Path(arts[name]["artifact_dir"])
        side = json.loads((A / "cache.sc.scopt.json").read_text())
        assert set(side["move_map"]) == fu, name
        assert all(m["delta"] == want for m in side["move_map"].values()), name
    side = json.loads((Path(arts["minusmax"]["artifact_dir"]) / "cache.sc.scopt.json").read_text())
    assert all(side["move_map"][u]["delta"] < 0 for u in fu)
    assert doc["minus64_direct"]["feasible_units"] == [] and doc["minus64_direct"]["moved"] == 0
    for name in ("identity", "plus64", "minus64c", "minusmax"):
        A = Path(arts[name]["artifact_dir"])
        val = json.loads((A / "validation.json").read_text())
        assert all(val["codes"][f"V{i}"] == "pass" for i in range(1, 9)), name
        assert all(r["result"] == "pass" for r in val["rows"] if r["code"] == "V6"), name
        assert all(x["ok"] for x in arts[name]["moved_units_v6"].values()), name
    # diag: re-parse each deploy arm's qemu.log
    parsed = {a: bf.parse_diag_log(Path(doc["diag"][a]["qemu_log"]).read_text())
              for a in bf.DEPLOY_ARMS}
    faults = {a: [e for e in p["exceptions"] if e["excp"] != bf.EXC_RETURN]
              for a, p in parsed.items()}
    nf = sum(1 for v in variants if bf.VARIANT_SYMS[v][1])
    for a in bf.DEPLOY_ARMS:
        assert len(faults[a]) == nf, a
        assert [e["lines"] for e in faults[a]] == [e["lines"] for e in faults["warm"]], a
        assert [bf.key_str(e["trace_before"]) for e in faults[a]] == \
            [bf.key_str(e["trace_before"]) for e in faults["warm"]], a
        assert parsed[a]["rewinds"] == parsed["warm"]["rewinds"], a
    for a in bf.DEPLOY_ARMS[1:]:
        tcs = {int(x["new_tc_ptr"], 16) for x in arts[a]["moved_units_v6"].values()}
        for e in faults[a]:
            assert e["trace_before"]["tc"] in tcs, (a, hex(e["trace_before"]["tc"]))
    # (c)/(d): the one-insn CF_NOIRQ TB with the same key (and count) in every deploy arm
    for v in variants:
        if v not in ("c", "d"):
            continue
        addr = syms[bf.VARIANT_SYMS[v][0]] & ~1
        keys = {a: {k: n for k, n in p["keys"].items()
                    if k.startswith(f"pc=0x{addr:x}/") and int(k.split("cflags=")[1], 16)
                    & bf.CF_NOIRQ} for a, p in parsed.items()}
        assert len(keys["warm"]) == 1, keys
        assert all(keys[a] == keys["warm"] for a in keys), keys
        if v == "d":
            assert list(keys["warm"].values()) == [64]
            assert all(parsed[a]["rewinds"].get(f"0x{addr:x}") == 64 for a in parsed)
