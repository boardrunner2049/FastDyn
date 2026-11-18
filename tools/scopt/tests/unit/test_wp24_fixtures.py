"""WP24 B3/B4 fixture consistency checks (static; no QEMU).

The fixture lives outside this repo in $FASTDYN_ROOT/fixtures/b3
(like the WP12 fixtures).  These tests check, independently of the
fixture's own generator where possible (pyelftools + capstone instead of
b3conf's .nm/.lst parsing), that

  * the images match ELF.sha256 and the frozen oracles were made from them;
  * every template's virtual/modifier addresses are the ELF symbols named in
    their comments, every B3 patch sits on a return instruction (or the
    designated `nop` of F-B3-5), and callee and continuation are on
    different 1 KiB pages;
  * the templates are exactly what gen_tomls.py renders (no hand edits);
  * every input encodes its config's phase id and argument, and every phase
    id is in b3.c's phase table;
  * every frozen icount oracle satisfies its named invariants, and the
    oracle machinery rejects corrupted outputs.

The dynamic acceptance (nocache/cold/warm x icount/noicount, per-mode
oracle) is tests/qemu/wp24_fixtures_qualify.sh.
"""
import hashlib
import importlib.util
import json
import os
import re
import struct
import sys
import tomllib

import pytest
from scopt.paths import fastdyn_root

B3 = str(fastdyn_root() / "fixtures/b3")

pytestmark = pytest.mark.skipif(not os.path.isfile(os.path.join(B3, "b3conf.py")),
                                reason="WP24 fixture not present")


def _load(name):
    if B3 not in sys.path:
        sys.path.insert(0, B3)
    spec = importlib.util.spec_from_file_location(name, os.path.join(B3, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def conf():
    return _load("b3conf")


@pytest.fixture(scope="module")
def expected(conf):
    return _load("expected")


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


_ELF_CACHE = {}


def _elf(path):
    if path not in _ELF_CACHE:
        from elftools.elf.elffile import ELFFile
        with open(path, "rb") as f:
            elf = ELFFile(f)
            syms = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols() if s.name}
            segs = [(seg["p_vaddr"], seg.data()) for seg in elf.iter_segments() if seg["p_type"] == "PT_LOAD"]
        _ELF_CACHE[path] = (syms, segs)
    return _ELF_CACHE[path]


def _insn(path, addr):
    import capstone
    _, segs = _elf(path)
    for base, data in segs:
        if base <= addr < base + len(data):
            md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
            return next(md.disasm(data[addr - base:addr - base + 4], addr))
    raise AssertionError(f"0x{addr:x} not in a loadable segment of {path}")


def _template(conf, cfg):
    text = open(os.path.join(B3, f"b3-{cfg.name}.toml.in")).read()
    doc = tomllib.loads(text.replace("@SUPERCACHE@", "").replace("@ICOUNT@", "").replace("@QEMU@", "/q"))
    return text, doc


def _entries_with_symbols(text):
    """[(kind, symbol, at)] from the generated `[[...]]  # <symbol>: ...` comments."""
    out = []
    for m in re.finditer(r"\[\[CPU\.cpu0\.(virtuals|modifiers)\]\]\s+# (ret:[^\s:]+|[^\s:]+):?.*\nat = \"(0x[0-9a-f]+)\"", text):
        out.append((m.group(1), m.group(2), int(m.group(3), 16)))
    return out


def test_images_match_sha256_file():
    lines = open(os.path.join(B3, "ELF.sha256")).read().split("\n")
    # 6 WP24 images + b3_b411.elf (close-out F-B4-11, design/closeout-logreg.md B.2)
    assert len([x for x in lines if x]) == 7
    for line in filter(None, lines):
        digest, path = line.split()
        assert _sha(os.path.join(B3, path)) == digest, path


def test_templates_are_generated(conf):
    gen = _load("gen_tomls")
    for cfg in conf.CONFIGS:
        assert open(os.path.join(B3, f"b3-{cfg.name}.toml.in")).read() == gen.render(cfg), cfg.name


def test_virtual_and_modifier_addresses_are_elf_symbols(conf):
    for cfg in conf.CONFIGS:
        elf = os.path.join(B3, cfg.elf)
        syms, _ = _elf(elf)
        text, doc = _template(conf, cfg)
        assert doc["CPU"]["cpu0"][0]["binary"] == elf
        entries = _entries_with_symbols(text)
        assert len(entries) == len(doc["CPU"]["cpu0"][0].get("virtuals", [])) + \
            len(doc["CPU"]["cpu0"][0].get("modifiers", [])), cfg.name
        for kind, sym, at in entries:
            if sym.startswith("ret:"):
                continue
            assert at == syms[sym] & ~1, (cfg.name, sym)
        names = {(k, s) for k, s, _ in entries}
        for need in ("b3_bench_start", "b3_pp_tag", "b3_pp_val", "b3_fail", "b3_fail_end", "b3_done"):
            assert ("virtuals", need) in names, (cfg.name, need)


def test_patches_sit_on_returns_and_pages_differ(conf):
    for cfg in conf.CONFIGS:
        elf = os.path.join(B3, cfg.elf)
        syms, _ = _elf(elf)
        _, doc = _template(conf, cfg)
        for mod in doc["CPU"]["cpu0"][0].get("modifiers", []):
            at = int(mod["at"], 16)
            ins = _insn(elf, at)
            if cfg.name == "b4-7":
                assert ins.mnemonic.startswith("ldr") and re.fullmatch(r"0x[0-9a-f]{8} <- 5", mod["patch"].replace('"', ""))
                continue
            if cfg.name == "5":
                assert ins.mnemonic == "nop"            # modifier on the insn before bx lr
                continue
            assert (ins.mnemonic == "bx" and ins.op_str == "lr") or \
                   (ins.mnemonic.startswith("pop") and "pc" in ins.op_str), (cfg.name, ins.mnemonic, ins.op_str)
            # the callee containing the patch lives in the callee area, its
            # continuation in .text: different 1 KiB pages (TARGET_PAGE_BITS 10)
            assert at >= 0x10000, (cfg.name, hex(at))
            vcs = [v & ~1 for k, v in syms.items() if k.startswith("vc_")]
            assert all((v >> 10) != (at >> 10) for v in vcs if v < 0x10000), cfg.name


def test_c_twins_differ_by_opt_level(conf):
    """F-B3-24: the -O0 build really changes the C callee (frame, spills)."""
    o1 = conf.func_return("b3.elf", "b3c_f1")
    o0 = conf.func_return("b3_O0.elf", "b3c_f1")
    os_ = conf.func_return("b3_Os.elf", "b3c_f1")
    s1, _ = _elf(os.path.join(B3, "b3.elf"))
    s0, _ = _elf(os.path.join(B3, "b3_O0.elf"))
    assert s1["b3c_f1"] & ~1 == s0["b3c_f1"] & ~1 == 0x100f4
    assert o0 - (s0["b3c_f1"] & ~1) > o1 - (s1["b3c_f1"] & ~1)
    assert o1 == os_
    # the asm phases do not move between builds
    for sym in ("f_b3_1", "vc_b3_1", "b3_pp_tag", "phase_b4_9"):
        assert s1[sym] == s0[sym]


def test_stale_16d_changes_one_instruction(conf):
    a, b = os.path.join(B3, "b3.elf"), os.path.join(B3, "b3_16d.elf")
    sa, _ = _elf(a)
    sb, _ = _elf(b)
    assert sa["f_b3_1"] == sb["f_b3_1"] and sa["f_b3_1_ret"] == sb["f_b3_1_ret"]
    ia, ib = _insn(a, sa["f_b3_1"] & ~1), _insn(b, sb["f_b3_1"] & ~1)
    assert ia.mnemonic == ib.mnemonic == "adds" and ia.size == ib.size
    # b3.elf: adds r0, #1 (overwritten by the r0 <- 0 patch); b3_16d.elf:
    # adds r6, #1 -- the caller's nz accumulator, which the patch does not
    # touch, so a stale R/C' from the b3.elf cache is visible in the output
    assert ia.op_str.replace(" ", "") in ("r0,#1", "r0,r0,#1")
    assert ib.op_str.replace(" ", "") in ("r6,#1", "r6,r6,#1")
    # the rest of the image is unchanged
    _, segs_a = _elf(a)
    _, segs_b = _elf(b)
    fa = sa["f_b3_1"] & ~1
    diff = [base + i for (base, da), (_, db) in zip(segs_a, segs_b) for i in range(min(len(da), len(db)))
            if da[i] != db[i]]
    assert diff and all(fa <= x < fa + 2 for x in diff) and [len(d) for _, d in segs_a] == [len(d) for _, d in segs_b]


def test_stale_16d_oracle_discriminates(conf, expected):
    """16d.norm differs from 1.norm in a result, not only in the BENCH name:
    running the stale b3.elf code under b3_16d.elf would not print 16d.norm."""
    def mask(t):
        return re.sub(r"\[BENCH\]\[[^]]*\]", "[BENCH][x]", t)
    n1 = open(os.path.join(B3, "expected", "1.norm")).read()
    n16 = open(os.path.join(B3, "expected", "16d.norm")).read()
    assert mask(n1) != mask(n16)
    assert expected.parse(n16)["results"] == {0: 1000, 1: 1000}
    assert expected.parse(n1)["results"] == {0: 1000, 1: 0}
    # the F-B3-1 output relabelled as 16d fails the 16d oracle
    relabelled = n1.replace("[BENCH][b3-1]", "[BENCH][b3-16d]")
    assert any("result[1]" in f for f in expected.check(relabelled, "16d", "icount").fails)


def test_inputs_encode_phase_and_argument(conf):
    phases_c = open(os.path.join(B3, "b3.c")).read()
    table = {int(x, 16) for x in re.findall(r"\{(0x[0-9a-f]+),\s*(?:phase_\w+|c\d_entry)", phases_c)}
    for cfg in conf.CONFIGS:
        assert cfg.phase in table, (cfg.name, hex(cfg.phase))
        for sfx, arg in cfg.inputs.items():
            data = open(os.path.join(B3, "inputs", f"{cfg.name}{sfx}.bin"), "rb").read()
            assert struct.unpack("<II", data) == (cfg.phase, arg)


def test_frozen_oracles_satisfy_invariants(conf, expected):
    meta = json.load(open(os.path.join(B3, "expected", "FROZEN.json")))
    for cfg in conf.CONFIGS:
        for inp in cfg.input_names():
            norm = open(os.path.join(B3, "expected", f"{inp}.norm")).read()
            c = expected.check(norm, inp, "icount")
            assert not c.fails, (inp, c.fails[:3])
            assert meta[inp]["elf_sha256"] == _sha(os.path.join(B3, cfg.elf)), inp
            assert meta[inp]["norm_sha256"] == hashlib.sha256(norm.encode()).hexdigest(), inp
            assert expected.compare(norm, inp, "icount") == []
            if not cfg.timing:
                assert expected.compare(norm, inp, "noicount") == []


def test_oracle_rejects_corruptions(conf, expected):
    """Negative controls for the oracle itself."""
    def put(text, tag, value):
        new = re.sub(r"(Reg 0:0x%08x\nReg 1:)0x[0-9a-f]{8}" % tag, r"\g<1>0x%08x" % value, text, count=1)
        assert new != text
        return new

    norm = open(os.path.join(B3, "expected", "8.norm")).read()
    assert any("fault.pc" in f for f in expected.check(put(norm, 0x540000, 0xDEAD), "8", "icount").fails)
    assert any("fault.cfsr" in f for f in expected.check(put(norm, 0x540002, 0x400), "8", "icount").fails)
    z_wrong = put(norm, 0x520000, 499)
    assert expected.check(z_wrong, "8", "icount").fails
    assert expected.compare(z_wrong, "8", "noicount")
    # timer phase: noicount accepts different interrupt positions but not a broken relation
    n7 = open(os.path.join(B3, "expected", "7.norm")).read()
    p = expected.parse(n7)
    assert p["hk"][0] > 0 and p["results"][1] == p["hk"][0]
    broken = put(n7, 0x520001, p["results"][1] + 1)
    assert any("nz ==" in f for f in expected.check(broken, "7", "noicount").fails)
    # the exact icount oracle is not applied to a timer phase in noicount mode
    shifted = put(n7, 0x530000, 0x1234)
    assert expected.compare(shifted, "7", "icount") and not expected.compare(shifted, "7", "noicount")


def test_models(expected):
    # CRC model == zlib's CRC-32 without the final inversion
    import zlib
    data = bytes(expected.buf4()) * 2
    assert expected.crc(512) == zlib.crc32(data) ^ 0xFFFFFFFF
    r = 0
    for i in range(700):
        r ^= i + 1
    assert expected.b4_9_sum() == r


# ------------------------------------------------- diag tools (review rework)
def _diag(name):
    d = os.path.join(B3, "diag")
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(f"wp24_diag_{name}", os.path.join(d, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# `info breakpoints` as gdb 15 prints it (runs/wp24-review/o3-neg/gdb.log):
# the first target was never hit, the second was hit 1000 times.
_INFO_UNHIT_FIRST = """Num     Type           Disp Enb Address            What
2       hw breakpoint  keep y   0x00007fff6c7f0000 
\tignore next 1000000000 hits
3       hw breakpoint  keep y   0x00007fff6c0040c0 
\tbreakpoint already hit 1000 times
\tignore next 999999000 hits
"""


def test_o3_hits_unhit_breakpoint_counts_zero():
    o3 = _diag("o3_hits")
    assert o3.parse_hits(_INFO_UNHIT_FIRST) == {0x7fff6c7f0000: 0, 0x7fff6c0040c0: 1000}
    # unhit breakpoint last, a one-hit ("time") row, and a trailing prompt line
    text = ("Num     Type           Disp Enb Address            What\n"
            "1       hw breakpoint  keep y   0x0000000000001000 \n"
            "\tbreakpoint already hit 1 time\n"
            "\tignore next 999999999 hits\n"
            "2       hw breakpoint  keep y   0x0000000000002000 \n"
            "\tignore next 1000000000 hits\n"
            "[Inferior 1 (process 4242) exited normally]\n"
            "\tbreakpoint already hit 77 times\n")
    assert o3.parse_hits(text) == {0x1000: 1, 0x2000: 0}
    assert 0x3000 not in o3.parse_hits(text)        # a missing target is not reported as 0


def test_o3_flash_offset_does_not_assume_record_0():
    o3 = _diag("o3_hits")
    rh = 0x80
    assert o3.flash_offset([0x110000 + rh, 0x110000 + 0x2f4], rh) == (0x110000, "record 0 (reset TB)")
    off, how = o3.flash_offset([0x110000 + 0x2f4, 0x110000 + 0x10000, 0x110000 + rh], rh)
    assert off == 0x110000 and "record 0 is not" in how
    with pytest.raises(ValueError):
        o3.flash_offset([0x110000 + 0x2f4, 0x110100], rh)                 # no aligned candidate
    with pytest.raises(ValueError):
        o3.flash_offset([0x2f4, 0x100000 + rh, 0x110000 + rh], rh)        # two candidates


def test_o3_flash_offset_on_real_cache_reordered(conf):
    """On a real F-B3-1 warm cache the offset is 0x110000 with the records in
    file order and with them reversed (an optimizer output may reorder)."""
    sc = str(fastdyn_root() / "runs/wp24/qual-1/icount/1/warm-2/cache/b3-1.sc")
    if not os.path.isfile(sc):
        pytest.skip("qualification run not present")
    o3 = _diag("o3_hits")
    rh = conf.symbols("b3.elf")["Reset_Handler"] & ~1
    _, recs = o3.read_records(sc)
    pas = [pa for _, pa in recs]
    assert o3.flash_offset(pas, rh)[0] == 0x110000
    assert o3.flash_offset(pas[::-1], rh)[0] == 0x110000


def test_gdb_scenarios_carry_checked_expectations():
    gg = _diag("gdb_guest")
    d = os.path.join(B3, "diag")
    want = {"f-b3-12": {"stop_iter", "cbz", "cbnz"}, "f-b3-13a": {"vc_hits"}, "f-b3-13b": {"watch_hits"},
            "f-b3-13c": {"step_pc"},
            "f-b3-13d": {"pause_iter", "pause_in_spin", "vc_hits", "write_iter", "r0_after_write"}}
    for name, keys in want.items():
        script = os.path.join(d, f"{name}.gdb")
        diag, res = gg.expectations(script)
        assert keys <= set(diag), name
        assert {0x520000, 0x520001} <= set(res), name             # z and nz results checked
        lines = [ln.strip() for ln in open(script) if ln.strip() and not ln.startswith("#")]
        assert lines[-1] == "detach", name                        # gdb exits 0 instead of losing the remote
    # 13(d): after the PAUSED stop, r0 := 7 at the first breakpoint stop at
    # Vc (design §9.5): the `cbz r0` that the B3 fact specializes reads it
    # next, so exactly one iteration (300) takes the nz path
    s13d = open(os.path.join(d, "f-b3-13d.gdb")).read()
    assert "set var $r0 = 7" in s13d and "set var $r6" not in s13d
    body = s13d[s13d.index("break *&vc_b3_13"):]
    assert body.index("set var $r0 = 7") < body.index("end\ncontinue\nend")   # inside the Vc commands
    dg, res = gg.expectations(os.path.join(d, "f-b3-13d.gdb"))
    assert res == {0x520000: 999, 0x520001: 1} and dg["write_iter"] == [300] and dg["r0_after_write"] == [7]


def test_gdb_guest_evaluate_rejects_wrong_values():
    gg = _diag("gdb_guest")
    norm = ["Reg 0:0x00520000", "Reg 1:0x000003e8", "Reg 0:0x00520001", "Reg 1:0x00000007", "exit_code=0"]
    pairs = gg.guest_pairs(norm)
    assert pairs == {0x520000: 1000, 0x520001: 7}
    ok = gg.evaluate({"vc_hits": ["700"]}, pairs, 0, {"vc_hits": [700]}, {0x520001: 7})
    assert all(c["ok"] for c in ok)
    for diag, rc, wd, wr in [({"vc_hits": ["699"]}, 0, {"vc_hits": [700]}, {}),
                             ({}, 0, {"vc_hits": [700]}, {}),               # key never printed
                             ({"vc_hits": ["700"]}, 0, {}, {0x520001: 0}),  # guest result differs
                             ({"vc_hits": ["700"]}, 1, {"vc_hits": [700]}, {})]:
        assert not all(c["ok"] for c in gg.evaluate(diag, pairs, rc, wd, wr))


def test_noicount_invariant_files_in_sync(conf, expected):
    """expected/<input>.inv.json is exactly what expected.py export-inv writes."""
    for cfg in conf.CONFIGS:
        for inp in cfg.input_names():
            path = os.path.join(B3, "expected", f"{inp}.inv.json")
            doc = json.load(open(path))
            assert doc == json.loads(json.dumps(expected.inv_doc(inp))), inp
            assert doc["mode"] == "noicount" and doc["also_equal_to_norm"] == (not cfg.timing)
            names = {i["name"] for i in doc["invariants"]}
            assert {"exit code", "header.phase", "end.pairs"} <= names, inp


# --------------------------------------------- review 3: log race, noicount ticks
_LOGGERS = {"b3_res", "SysTick_Handler", "FaultHandler", "SpareIrq_Handler", "main"}


def _funcs(path):
    from elftools.elf.elffile import ELFFile
    with open(path, "rb") as f:
        return sorted((s["st_value"] & ~1, s["st_size"], s.name)
                      for s in ELFFile(f).get_section_by_name(".symtab").iter_symbols()
                      if s["st_info"]["type"] == "STT_FUNC" and s["st_size"])


def _disasm(path, start, size):
    import capstone
    _, segs = _elf(path)
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    md.skipdata = True                      # literal pools inside handlers
    for base, data in segs:
        if base <= start < base + len(data):
            return list(md.disasm(data[start - base:start - base + size], start))
    raise AssertionError(hex(start))


@pytest.mark.parametrize("elf", ["b3.elf", "b3_O0.elf", "b3_Os.elf", "b3_16d.elf"])
def test_thread_log_writes_are_masked(elf):
    """log_pair is a non-atomic RMW of g_log_n.  The only thread-side caller
    while SysTick may fire is b3_res (phase results), which must run it with
    PRIMASK set and restore PRIMASK after; every other call site is a handler
    or main (main logs before the phase environment is set up and after it is
    torn down).  Review 3 saw the race lose one SysTick record (20-371)."""
    path = os.path.join(B3, elf)
    syms, _ = _elf(path)
    lp = syms["log_pair"] & ~1
    callers = {}
    for start, size, name in _funcs(path):
        for i in _disasm(path, start, size):
            if i.mnemonic in ("bl", "b.w", "b", "blx") and i.op_str == f"#0x{lp:x}":
                callers.setdefault(name, []).append(i.address)
    assert set(callers) <= _LOGGERS, set(callers) - _LOGGERS
    assert "b3_res" in callers and "SysTick_Handler" in callers
    res = [i for s, z, n in _funcs(path) if n == "b3_res" for i in _disasm(path, s, z)]
    seq = [(i.mnemonic, i.op_str.lower()) for i in res]
    k_cpsid = next(k for k, (m, o) in enumerate(seq) if m == "cpsid" and o == "i")
    k_mrs = next(k for k, (m, o) in enumerate(seq) if m == "mrs" and "primask" in o)
    k_bl = next(k for k, i in enumerate(res) if i.op_str == f"#0x{lp:x}")
    k_msr = next(k for k, (m, o) in enumerate(seq) if m == "msr" and "primask" in o)
    assert k_mrs < k_cpsid < k_bl < k_msr
    assert not any(m == "cpsie" for m, _ in seq)       # restores the caller's PRIMASK, never forces 0


def _norm_without_systicks(expected, text):
    """The frozen output with every SysTick record removed and the counters
    that follow from it (end.systicks, end.pairs, hits) adjusted."""
    p = expected.parse(text)
    pairs = [(t, v) for t, v in p["pairs"] if t >> 16 != 0x53]
    pairs = [(t, 0 if t == 0x5F0003 or t == 0x560000 else v) for t, v in pairs]
    pairs = [(t, len(pairs) if t == 0x5F0000 else v) for t, v in pairs]
    return pairs


def _render(text, pairs):
    head = [ln for ln in text.splitlines() if not ln.startswith("Reg ")][:1]
    tail = [ln for ln in text.splitlines() if ln.startswith("[BENCH]") or ln.startswith("exit_code=")]
    body = [x for t, v in pairs for x in (f"Reg 0:0x{t:08x}", f"Reg 1:0x{v:08x}")]
    return "\n".join(head + body + tail) + "\n"


def test_noicount_accepts_zero_ticks_icount_does_not(conf, expected):
    """Without icount a timer phase may legally take no SysTick (review 3:
    9 of 1200 nocache runs); the noicount oracle must accept that and the
    icount oracle must not (non-vacuity is an icount property)."""
    for inp in ("6h", "7", "20", "b4-3"):
        text = open(os.path.join(B3, "expected", f"{inp}.norm")).read()
        pairs = _norm_without_systicks(expected, text)
        if inp == "7":       # no tick -> no Vc hit -> every iteration z
            pairs = [(t, {0x520000: 12000, 0x520001: 0}.get(t, v)) for t, v in pairs]
        if inp == "20":      # no tick -> f's r0 = 1 always -> every iteration nz
            pairs = [(t, {0x520000: 0, 0x520001: 4000}.get(t, v)) for t, v in pairs]
        zero = _render(text, pairs)
        assert expected.parse(zero)["systicks"] == []
        assert expected.compare(zero, inp, "noicount") == [], inp
        fails = expected.check(zero, inp, "icount").fails
        assert any("SysTick records > 0" in f for f in fails), (inp, fails)


def test_oracle_catches_a_lost_systick_record(conf, expected):
    """The race signature of review 3 (20-371: g_systick_count 48, 47 records
    left) fails in both modes."""
    text = open(os.path.join(B3, "expected", "20.norm")).read()
    p = expected.parse(text)
    pairs, seen, drop = [], 0, None
    for t, v in p["pairs"]:
        if t == 0x530000:
            seen += 1
            drop = seen == len(p["systicks"])         # drop the last record, keep the counter
        if not (drop and t >> 16 == 0x53):
            pairs.append((t, v))
    pairs = [(t, len(pairs) if t == 0x5F0000 else v) for t, v in pairs]
    lost = _render(text, pairs)
    for mode in ("icount", "noicount"):
        assert any("end.systicks == SysTick records" in f for f in expected.check(lost, "20", mode).fails), mode


def test_f_b3_7_non_vacuity_margin(expected):
    """F-B3-7 has its own SysTick sweep so that many interrupts land at Vc in
    icount (review 3: 2 of 214 with the shared sweep)."""
    p = expected.parse(open(os.path.join(B3, "expected", "7.norm")).read())
    assert p["hk"][0] >= 20 and p["results"][1] == p["hk"][0]
    assert "ST_7" in open(os.path.join(B3, "b3.c")).read()


def test_o3_exited_normally_rejects_nonzero_exit():
    o3 = _diag("o3_hits")
    assert o3.exited_normally("x\n[Inferior 1 (process 4242) exited normally]\n")
    assert not o3.exited_normally("[Inferior 1 (process 4242) exited with code 01]\n")
    assert not o3.exited_normally("Program terminated with signal SIGKILL, Killed.\n")


def test_qemu_rerun_rewrites_a_moved_run_dir(tmp_path):
    """A run dir moved after the run: the printed command still names the old
    location; prepare() must rewrite it (review 3: FastDyn could not open
    virtuals.txt and gdb timed out)."""
    qr = _diag("qemu_rerun")
    old = "/nonexistent/runs/nowhere/old-run"
    src = tmp_path / "moved-run"
    (src / "work" / "virtuals").mkdir(parents=True)
    (src / "work" / "virtuals" / "virtuals.txt").write_text("x\n")
    (src / "work" / "dev_config.json").write_text("{}\n")
    (src / "run.json").write_text(json.dumps({"run_dir": old}))
    cmd = (f"/q/qemu-system-arm \\\n  -M cortexm,memory-backend=ram0 \\\n"
           f"  -accel tcg,supercache={old}/cache/b3-1.sc \\\n"
           f"  -kernel /fx/b3.elf \\\n  -plugin /lib/libfastdyn.so,virtuals={old}/work/virtuals/virtuals.txt,"
           f"\n    dev_config={old}/work/dev_config.json\n")
    (src / "stdout.log").write_text("fastdyn.targets.qemu_target|INFO|  Running following qemu command:\n"
                                    + cmd + "fastdyn.x|INFO| next\n")
    out = tmp_path / "diag"
    argv = qr.prepare(str(src), str(out))
    joined = " ".join(argv)
    assert old not in joined and str(src) not in joined
    assert f"virtuals={out}/virtuals/virtuals.txt" in joined
    assert f"dev_config={out}/dev_config.json" in joined
    assert f"supercache={out}/cache/b3-1.sc" in joined
    with pytest.raises(SystemExit, match="supercache run"):
        nocache = tmp_path / "nocache-run"
        (nocache / "work" / "virtuals").mkdir(parents=True)
        (nocache / "work" / "dev_config.json").write_text("{}\n")
        (nocache / "stdout.log").write_text("fastdyn.targets.qemu_target|INFO|  Running following qemu command:\n"
                                            "/q/qemu-system-arm \\\n  -kernel /fx/b3.elf\nfastdyn.x|INFO| y\n")
        qr.prepare(str(nocache), str(tmp_path / "diag2"), cache_from=str(src))
