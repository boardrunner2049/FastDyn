"""WP2: MAPS parser, ELF symbolization and the guest Thumb length check."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scopt.decode.symbols import (SETARCH_R_PIE_BASE, Maps, Module, Symbolizer,
                                  load_elf)
from scopt.decode.thumb import (check_lengths, lengths_from_words, thumb2_halfword_len,
                                thumb_lengths)
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
QEMU = ROOT / "src/qemu/build/qemu-system-arm"
PLUGIN = ROOT / "src/fastdyn/build/libfastdyn.so"

MAPS_TEXT = """\
555555554000-5555558bc000 r--p 00000000 103:02 1234   /opt/q/qemu-system-arm
5555558bc000-55555601d000 r-xp 00368000 103:02 1234   /opt/q/qemu-system-arm
555556fd0000-555557000000 rw-p 00000000 00:00 0      [heap]
7fff1f000000-7fff1f00a000 r--p 00000000 103:02 99     /opt/p/libfastdyn.so
7fff6c000000-7fffec000000 rwxp 00000000 00:00 0
7ffffffde000-7ffffffff000 rw-p 00000000 00:00 0      [stack]
"""


def test_maps_parse_find_and_roundtrip():
    m = Maps.parse(MAPS_TEXT)
    assert len(m) == 6
    e = m.find(0x5555558BC010)
    assert e is not None and e.perms == "r-xp" and e.offset == 0x368000 and e.executable
    assert e.path == "/opt/q/qemu-system-arm" and e.inode == 1234
    assert m.find(0x7FFF6C000000).path == ""
    assert m.find(0x555556FCFFFF) is None and not m.contains(0x1000)
    assert m.find(0x7FFFEC000000) is None  # end is exclusive
    assert m.paths() == ["/opt/q/qemu-system-arm", "/opt/p/libfastdyn.so"]
    assert m.load_bias("/opt/q/qemu-system-arm") == 0x555555554000
    assert m.load_bias("/nope") is None
    assert Maps.parse("\n".join(x.to_line() for x in m)).entries == m.entries
    with pytest.raises(ValueError):
        Maps.parse("garbage line")


def test_symbolizer_anonymous_and_heap_mappings():
    s = Symbolizer([], Maps.parse(MAPS_TEXT))
    assert s.symbolize(0x555556FD0010) == ("[heap]", None, 0x10)
    assert str(s.symbolize(0x7FFF6C000018)) == "[anon]+0x18"
    assert s.symbolize(0x10) is None


@pytest.mark.real
def test_elf_symbols_match_nm():
    if not QEMU.exists():
        pytest.skip("qemu binary missing")
    img = load_elf(QEMU)
    assert img.is_pie
    nm = subprocess.run(["nm", "--defined-only", str(QEMU)], capture_output=True, text=True,
                        check=True).stdout
    want = {}
    for line in nm.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] in ("helper_lookup_tb_ptr", "helper_ldul_mmu",
                                            "helper_v7m_msr", "tb_gen_code"):
            want[parts[2]] = int(parts[0], 16)
    assert len(want) == 4
    for name, v in want.items():
        assert img.symbol(name).value == v
        assert img.lookup(v) == (name, 0)
        assert img.is_func_start(v)
    v = want["helper_ldul_mmu"]
    assert img.lookup(v + 3) == ("helper_ldul_mmu", 3)
    q = Module.from_file(QEMU, SETARCH_R_PIE_BASE)
    sym = Symbolizer([q])
    assert sym.symbolize(SETARCH_R_PIE_BASE + v) == (q.name, "helper_ldul_mmu", 0)
    assert q.contains(SETARCH_R_PIE_BASE + v) and not q.contains(0x1000)
    assert q.address_of("helper_ldul_mmu") == SETARCH_R_PIE_BASE + v
    # synthesized maps cover the PT_LOAD segments at the bias
    mm = Maps.from_modules([q])
    assert mm.find(SETARCH_R_PIE_BASE + v).executable
    assert Maps.from_modules([q]).load_bias(q.path, q.image) == SETARCH_R_PIE_BASE


@pytest.mark.real
def test_symbolizer_from_real_maps_text():
    if not (QEMU.exists() and PLUGIN.exists()):
        pytest.skip("binaries missing")
    text = (f"555555554000-5555558bc000 r--p 00000000 00:00 1 {QEMU}\n"
            f"5555558bc000-55555601d000 r-xp 00368000 00:00 1 {QEMU}\n"
            f"7fff1f000000-7fff1f00a000 r--p 00000000 00:00 2 {PLUGIN}\n"
            f"7fff1f00a000-7fff1f02c000 r-xp 0000a000 00:00 2 {PLUGIN}\n")
    s = Symbolizer.from_maps(Maps.parse(text))
    assert {m.name for m in s.modules} == {"qemu-system-arm", "libfastdyn.so"}
    plug = [m for m in s.modules if m.name == "libfastdyn.so"][0]
    assert plug.base == 0x7FFF1F000000
    bs = plug.address_of("benchmark_start")
    assert s.symbolize(bs) == ("libfastdyn.so", "benchmark_start", 0)


# --------------------------------------------------------------------------
# Thumb
# --------------------------------------------------------------------------

def test_thumb_halfword_rule():
    assert thumb2_halfword_len(0xB580) == 2  # push {r7, lr}
    assert thumb2_halfword_len(0xF000) == 4  # bl prefix
    assert thumb2_halfword_len(0xE92D) == 4  # push.w
    assert thumb2_halfword_len(0xE7FE) == 2  # b . (0b11100 is 16-bit)


def test_thumb_lengths_and_check():
    # push {r7,lr}; bl +0; movs r0,#1; ldr.w r0,[r1,#4]; bx lr
    code = bytes.fromhex("80b5" "00f000f8" "0120" "d1f80400" "7047")
    assert thumb_lengths(code, 0x08000100) == [2, 4, 2, 4, 2]
    assert thumb_lengths(code, 0x08000100, count=2) == [2, 4]
    assert check_lengths(code, [2, 4, 2, 4, 2], 0x08000100) == []
    probs = check_lengths(code, [2, 2, 4, 4, 2], 0x08000100)
    assert probs and "insn 1" in probs[0]
    assert check_lengths(code[:-2], [2, 4, 2, 4, 2])  # wrong guest size
    # an undefined encoding still gets its architectural length
    assert thumb_lengths(bytes.fromhex("ffffffff")) == [4]


def test_lengths_from_words_with_page_wrap():
    assert lengths_from_words([0x100, 0x102, 0x106], 8) == [2, 4, 2]
    # PCREL word0 is the in-page offset; crossing a 1 KiB page wraps to 0
    assert lengths_from_words([0x3FC, 0x3FE, 0x002], 8, page_bits=10) == [2, 4, 2]
    assert lengths_from_words([0x3FC, 0x000], 6, page_bits=10) == [4, 2]
    assert lengths_from_words([], 0) == []
