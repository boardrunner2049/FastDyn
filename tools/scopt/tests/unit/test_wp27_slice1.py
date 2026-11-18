"""WP27 integration stage (slice 1 on the gate binary): offline tests of the driver
``tests/qemu/wp27_slice1.py`` (plan ``## WP27`` steps 6-7, T-B3-8; design §3.5, §9.4).

The dynamic evidence is in ``runs/wp27/slice1/`` (``wp27-slice1.json``, ``verdict.json``).
These tests pin the pieces that turn host bytes into the O4 mutant and the X-REHY
variant (site finder, jcc rewrite, nop fill) and the verdict logic, without QEMU; the
last test re-reads the recorded evidence when it exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

HERE = Path(__file__).resolve().parent
QEMU_TESTS = HERE.parent / "qemu"
ROOT = fastdyn_root()
EVIDENCE = ROOT / "runs/wp27/slice1/wp27-slice1.json"

# C = vc_b3_1's TB `cbz r0` on the gate binary (icount base), first 0x30 bytes:
# icount entry check, then the r0 load / test / jne of §3.5 at 0x15 / 0x18 / 0x1a
C_PLAIN = bytes.fromhex(
    "8b5df8" "ffcb" "85db" "0f8c4b000000" "66895df8" "c645fc01"
    "8b5d00" "85db" "0f851c000000" "8b5d3c" "83c306" "895d3c" "6690" "e900000000")
# the same C under F-B3-23 (coverage): FastDyn's inline LOG_REG ring store (with its
# spin loop `je 0x15`) precedes the r0 load / test / jne at 0x56 / 0x59 / 0x5b
C_COV = bytes.fromhex(
    "8b5df8" "ffcb" "85db" "0f8c8b000000" "66895df8" "c645fc01"
    "488d1d549ff6f7" "440fb723" "458d6c2404" "450fb7ed" "4c8d352c4edad7" "450fb736"
    "453bee" "74dc" "4d63ec" "4c8d352dc5fffb" "4d03ee" "448b753c" "45897500" "4183c404"
    "66448923" "8b5d00" "85db" "0f851b000000" "418d5e06")


@pytest.fixture(scope="module")
def D():
    pytest.importorskip("capstone")
    sys.path.insert(0, str(QEMU_TESTS))
    import wp27_slice1

    return wp27_slice1


def _dis(code: bytes, addr: int = 0):
    import capstone

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    return list(md.disasm(code, addr))


def test_find_sites_plain_and_coverage(D):
    s = D.find_sites(C_PLAIN, 0)
    assert {k: v[:2] for k, v in s.items()} == {"L": (0x15, 3), "T": (0x18, 2), "J": (0x1A, 6)}
    assert s["J"][2].startswith("jne")
    # the entry-check load `mov ebx,[rbp-8]` + `test ebx,ebx` + `jl` is not the r0 site
    c = D.find_sites(C_COV, 0)
    assert {k: v[:2] for k, v in c.items()} == {"L": (0x56, 3), "T": (0x59, 2), "J": (0x5B, 6)}


def test_find_sites_needs_test_of_the_loaded_register(D):
    # r0 loaded into ebx but the test is of ecx: no site
    code = bytes.fromhex("8b5d00" "85c9" "0f851c000000")
    assert D.find_sites(code, 0) == {}
    # another env offset for regs[0]
    code = bytes.fromhex("8b5d10" "85db" "0f851c000000")
    assert D.find_sites(code, 0) == {}
    assert {k: v[:2] for k, v in D.find_sites(code, 0x10).items()} == {
        "L": (0, 3), "T": (3, 2), "J": (5, 6)}


def test_jcc_to_jmp_keeps_the_target(D):
    for code, off in ((C_PLAIN, 0x1A), (C_COV, 0x5B)):
        new = D.jcc_to_jmp(code, off, 6)
        assert len(new) == 6 and new[0] == 0xE9 and new[5] == 0x90
        old_i = _dis(code[off:off + 6], off)[0]
        new_i = _dis(new, off)
        assert new_i[0].mnemonic == "jmp" and new_i[1].mnemonic == "nop"
        assert int(new_i[0].op_str, 16) == int(old_i.op_str, 16)
    short = bytes.fromhex("7410")
    assert D.jcc_to_jmp(short, 0, 2) == bytes.fromhex("eb10")
    with pytest.raises(ValueError):
        D.jcc_to_jmp(bytes.fromhex("85db"), 0, 2)


@pytest.mark.parametrize("n,want", [(2, "6690"), (3, "669090"), (6, "669066906690"),
                                    (1, "90")])
def test_nopfill(D, n, want):
    b = D.nopfill(n)
    assert b.hex() == want
    ins = _dis(b)
    assert sum(i.size for i in ins) == n and all(i.mnemonic == "nop" for i in ins)
    # register-free: no memory operand (B3 pass encodings, WP21 note 12)
    assert all("ptr" not in i.op_str for i in ins)


def _doc(ok: bool = True, o4_ok: bool = True, xrehy_ok: bool = True) -> dict:
    m = {"icount": None, "noicount": None}
    dyn_items = {k: ok for k in ("o1_equal_warm", "o2a_equal_warm", "o2b_equal_warm",
                                 "z_n_nz_0_both", "o3_pass", "armed_eq_1",
                                 "c_prime_hits_about_n_minus_warmup", "f_taken_only_in_warmup",
                                 "c_prime_plus_f_eq_stub", "no_invariant_violation",
                                 "proofs_pass", "gate_binary")}
    return {"env": {}, "bases": {k: {"ok": True} for k in m},
            "artifact": {k: {"ok": True} for k in m},
            "dynamic": {k: {"ok": ok, "items": dyn_items} for k in m},
            "o4": {k: {"ok": o4_ok} for k in m}, "xrehy": {k: {"ok": xrehy_ok} for k in m}}


def test_verdict_rows(D):
    modes = ["icount", "noicount"]
    v = D.verdict(_doc(), modes)
    assert v["verdict"] == "pass" and v["timed"] is False
    crit = [r["criterion"] for r in v["rows"]]
    assert crit[-1].startswith("T-B3-8") and v["rows"][-1]["result"] == "pass"
    v = D.verdict(_doc(o4_ok=False), modes)
    assert v["verdict"] == "fail" and v["rows"][-1]["result"] == "fail"
    # X-REHY is not part of T-B3-8, but it is a WP27 acceptance row
    v = D.verdict(_doc(xrehy_ok=False), modes)
    assert v["verdict"] == "fail" and v["rows"][-1]["result"] == "pass"
    v = D.verdict(_doc(ok=False), modes)
    assert v["rows"][-1]["result"] == "fail"


def test_recorded_evidence():
    """The recorded slice-1 run (if present): one link armed, C' entered N - 1 times
    with one warm-up fallback, both O4 mutants took the nz path, X-REHY ring equal."""
    if not EVIDENCE.is_file():
        pytest.skip("runs/wp27/slice1 not recorded")
    d = json.loads(EVIDENCE.read_text())
    assert d["env"]["gate_sha256"].startswith("56d8bfb3") and d["env"]["gate_unchanged"]
    assert d["env"]["timed"] is False
    for m in ("icount", "noicount"):
        a = d["artifact"][m]
        assert a["ext"]["version"] == 6 and a["ext"]["variants"] == 1
        assert [s["kind"] for s in a["ext"]["slots"]] == ["SPEC"]
        assert a["ext"]["links"] == [{"slot": 0, "variant": 0}]
        dy = d["dynamic"][m]
        assert dy["verdict"] == "pass" and dy["stats_view"]["links_armed"] == 1
        lab = dy["probe_labels"]
        assert (dy["hits"][lab["C'"]], dy["hits"][lab["F"]], dy["hits"][lab["S"]]) == \
            (999, 1, 1000)
        for mu in d["o4"][m]["mutants"].values():
            assert mu["items"]["o4_detected"]
            assert mu["results"]["mutant"] == {"0": 1, "1": 999}
        x = d["xrehy"][m]
        assert x["dynamic"]["oracles"]["O6"] == "pass"
        ring = x["dynamic"]["data"]["O6"]
        assert ring["artifact"]["ring"] == ring["reference"]["ring"]
