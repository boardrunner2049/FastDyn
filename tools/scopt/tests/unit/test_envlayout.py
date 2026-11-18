"""WP17: EnvLayout v1 (design §0 G21) — producer parser, packaged layout, binding."""

from __future__ import annotations

import copy
import os
import re
import shutil
from pathlib import Path

import pytest

from scopt.analysis.envlayout import envlayout_for_runtime, parse_ptype_o, produce_envlayout
from scopt.formats import schemas
from scopt.formats.envlayout_v1 import (G21_NAMES, EnvField, EnvLayout, EnvLayoutError,
                                        check_binding, find_envlayout, read_envlayout)

QINT_SHA = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
QINT_EXE = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn")) / \
    "wt/qemu-int/build/qemu-system-arm"
OFFSETS_TXT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn")) / \
    "runs/b3-explore/judge-soundness/offsets.txt"
OTHER_SHA = "sha256:" + "ab" * 32


@pytest.fixture(scope="module")
def qint() -> EnvLayout:
    lay = find_envlayout(QINT_SHA)
    assert lay is not None, "packaged EnvLayout for the qint binary is missing"
    return lay


def _off(lay: EnvLayout, name: str) -> int:
    return lay.field(name).off


# -- packaged layout = DWARF facts of the qint binary -----------------------------------------

def test_packaged_layout_validates_and_binds(qint):
    doc = qint.to_json()
    schemas.validate(doc, "envlayout.v1")
    assert doc["envlayout_id"] == qint.envlayout_id
    assert qint.exe_sha256 == QINT_SHA
    assert check_binding(qint, {"qemu_sha256": QINT_SHA}) is None
    assert check_binding(qint, {"qemu_sha256": QINT_SHA}, qint.envlayout_id) is None


def test_g21_offsets(qint):
    """Design §0 G21 table (env-relative)."""
    want = {"regs[15]": 0x3C, "thumb": 0x14D, "hflags.flags": 0x150, "hflags.flags2": 0x158,
            "condexec_bits": 0x228, "mpu_ctrl[0]": 0xC40,
            "CPUState.breakpoints": -0x2980, "CPUState.watchpoints": -0x2970,
            "CPUState.singlestep_enabled": -0x2AEC, "plugin_mem_cbs": -0x20,
            "plugin_mem_value_low": -0x18, "plugin_mem_value_high": -0x10,
            "icount_decr": -0x8, "can_do_io": -0x4}
    got = {k: _off(qint, G21_NAMES[k]) for k in want}
    assert got == want


def _parse_offsets_txt(text: str) -> tuple[dict[int, int], dict[str, int]]:
    vals = {}
    for m in re.finditer(r"^\$(\d+) = .*?(0x[0-9a-f]+|\d+)\s*$", text, re.M):
        vals[int(m.group(1))] = int(m.group(2), 0)
    neg = {}
    for m in re.finditer(r"^/\*\s+(\d+)\s+\|\s+\d+ \*/\s+.*?(\w+);", text, re.M):
        neg[m.group(2)] = int(m.group(1))
    return vals, neg


def test_offsets_equal_offsets_txt(qint):
    """WP17 acceptance: EnvLayout offsets equal runs/b3-explore/judge-soundness/offsets.txt."""
    if not OFFSETS_TXT.is_file():
        pytest.skip(f"{OFFSETS_TXT} not present")
    vals, neg = _parse_offsets_txt(OFFSETS_TXT.read_text())
    cs = vals[1]  # sizeof(CPUState)
    assert qint.sizes["CPUState"] == cs
    assert qint.env_offset_in_cpu == vals[9]  # (CPUARMState *) 0x2bc0 = &ArchCPU.env
    assert qint.neg_offset_in_cpustate == vals[10]  # (CPUNegativeOffsetState *) 0x300
    # CPUState fields (offsets inside CPUState) -> env-relative
    assert _off(qint, "cpu.breakpoints.tqh_first") == vals[2] - cs
    assert _off(qint, "cpu.watchpoints.tqh_first") == vals[3] - cs
    assert _off(qint, "cpu.singlestep_enabled") == vals[4] - cs
    # CPUARMState fields (env-relative already)
    assert _off(qint, "v7m.mpu_ctrl[0]") == vals[5]
    assert _off(qint, "thumb") == vals[6]
    assert _off(qint, "hflags.flags") == vals[7]
    assert _off(qint, "condexec_bits") == vals[8]
    # CPUNegativeOffsetState (ptype/o in the same file), relative to its end (= env)
    nsize = qint.sizes["CPUNegativeOffsetState"]
    assert nsize == 10432
    for f in ("plugin_mem_cbs", "plugin_mem_value_low", "plugin_mem_value_high", "icount_decr",
              "can_do_io"):
        name = "neg.icount_decr.u32" if f == "icount_decr" else f"neg.{f}"
        assert _off(qint, name) == neg[f] - nsize, f


def test_classify(qint):
    c = qint.classify
    assert c(-8, 4).name == "neg.icount_decr.u32" and c(-8, 4).exact
    assert c(-8, 2).name == "neg.icount_decr.u16.low" and c(-8, 2).exact
    assert c(-6, 2).name == "neg.icount_decr.u16.high"
    assert c(-4, 1).name == "neg.can_do_io"
    assert c(0x3C, 4).name == "regs[15]" and c(0x3C, 4).exact
    assert c(0, 4).name == "regs[0]"
    r = c(-0x18, 4)  # 32-bit store into the 64-bit plugin_mem_value_low
    assert r.name == "neg.plugin_mem_value_low" and not r.exact and r.width == 4
    assert c(0x218, 4).name == "NF"
    assert c(-0x110, 4).name == "neg.tlb.f[1].mask"
    assert c(-0x108, 8).name == "neg.tlb.f[1].table"
    # the 2-byte hole after thumb (0x14e) is no field: unclassified
    assert c(0x14E, 1) is None
    # an access straddling two fields is unclassified
    assert c(0x3A, 4) is None
    # beyond the end of CPUARMState
    assert c(qint.sizes["CPUArchState"] + 16, 4) is None


def test_env_bounds(qint):
    lo, hi = qint.env_bounds()
    assert lo == -11200 and hi == qint.sizes["CPUArchState"]


# -- binding -------------------------------------------------------------------------------------

def test_different_exe_is_unbound(qint):
    """WP17 acceptance: a different exe sha256 yields guard_layout_unbound."""
    assert find_envlayout(OTHER_SHA) is None
    assert check_binding(qint, {"qemu_sha256": OTHER_SHA}) == "guard_layout_unbound"
    assert check_binding(None, {"qemu_sha256": QINT_SHA}) == "guard_layout_unbound"
    assert check_binding(qint, {"qemu_sha256": QINT_SHA}, "sha256:" + "0" * 64) == \
        "guard_layout_unbound"
    v = envlayout_for_runtime({"qemu_sha256": OTHER_SHA})
    assert not v["bound"] and v["reason"] == "guard_layout_unbound" and v["layout"] is None
    v = envlayout_for_runtime({"qemu_sha256": QINT_SHA})
    assert v["bound"] and v["layout"].envlayout_id == qint.envlayout_id
    # v2 identity whose admission names another layout id
    v = envlayout_for_runtime({"qemu_sha256": QINT_SHA, "admission": {"envlayout_id": OTHER_SHA}})
    assert not v["bound"]


def test_search_path_and_tamper(tmp_path, qint, monkeypatch):
    doc = qint.to_json()
    bad = copy.deepcopy(doc)
    bad["fields"][0]["off"] += 4
    with pytest.raises(EnvLayoutError) as e:
        read_envlayout(bad)
    assert e.value.code == "envlayout_id_mismatch"
    with pytest.raises(EnvLayoutError):
        read_envlayout({**doc, "schema": "scopt.envlayout/9"})
    # a layout for another exe on SCOPT_ENVLAYOUT_PATH is found only for that exe
    other = EnvLayout.build(exe_sha256=OTHER_SHA, fields=[EnvField("regs", "env", 0, 64, 4, (16,))],
                            sizes=dict(qint.sizes), env_offset_in_cpu=11200,
                            neg_offset_in_cpustate=768, producer={"tool": "test", "method": "x"})
    (tmp_path / (OTHER_SHA.split(":")[1] + ".json")).write_bytes(other.canonical_bytes())
    monkeypatch.setenv("SCOPT_ENVLAYOUT_PATH", str(tmp_path))
    got = find_envlayout(OTHER_SHA)
    assert got is not None and got.envlayout_id == other.envlayout_id
    assert check_binding(got, {"qemu_sha256": QINT_SHA}) == "guard_layout_unbound"


# -- ptype/o parser --------------------------------------------------------------------------------

PTYPE = """\
/* offset      |    size */  type = struct Demo {
/*      0      |      16 */    uint32_t regs[4];
/*     16      |       1 */    _Bool thumb;
/* XXX  3-byte hole      */
/*     20      |      16 */    struct {
/*     20      |       4 */        uint32_t a;
/*     24      |      12 */        uint32_t b[3];

                                   /* total size (bytes):   16 */
                               } inner;
/*     36      |       8 */    union {
/*                     4 */        uint32_t u32;
/*                     4 */        struct {
/*     36      |       2 */            uint16_t low;
/*     38      |       2 */            uint16_t high;

                                       /* total size (bytes):    4 */
                                   } u16;

                                   /* total size (bytes):    8 */
                               } dec;
/*     44      |      16 */    struct {
/*     44      |       4 */        int x;
/*     48      |       4 */        int y;

                                   /* total size (bytes):    8 */
                               } pair[2];
/*     60      |       8 */    void (*fn)(int);
/*     68      |       8 */    CPUTLB tlb;

                               /* total size (bytes):   76 */
                             }
type = unsigned long
"""


def test_parse_ptype_o():
    got = parse_ptype_o(PTYPE)
    assert got["unsigned long"] == (0, [])
    size, leaves = got["Demo"]
    assert size == 76
    flat = {".".join(lf.path): (lf.off, lf.size, lf.dims) for lf in leaves}
    assert flat["regs"] == (0, 16, (4,))
    assert flat["thumb"] == (16, 1, ())
    assert flat["inner.a"] == (20, 4, ())
    assert flat["inner.b"] == (24, 12, (3,))
    assert flat["dec.u32"] == (36, 4, ())
    assert flat["dec.u16.low"] == (36, 2, ())
    assert flat["dec.u16.high"] == (38, 2, ())
    assert flat["pair[0].x"] == (44, 4, ()) and flat["pair[1].y"] == (56, 4, ())
    assert flat["fn"] == (60, 8, ())
    assert flat["tlb"] == (68, 8, ())
    lf = next(x for x in leaves if x.path == ("tlb",))
    assert lf.type == "CPUTLB"


@pytest.mark.real
@pytest.mark.slow
def test_produce_matches_packaged(qint, tmp_path):
    """Re-run the gdb producer on the qint binary: same EnvLayout id as the packaged one."""
    if shutil.which("gdb") is None or not QINT_EXE.is_file():
        pytest.skip("gdb or the qint binary is not available")
    lay = produce_envlayout(QINT_EXE, exe_sha256=QINT_SHA)
    assert lay.envlayout_id == qint.envlayout_id
