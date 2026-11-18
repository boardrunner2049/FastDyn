"""Close-out B: the F-B4-11 fixture (fixtures/b3 b4_11.s, b3_b411.elf; design/closeout-logreg.md B.2).

* the earlier images are untouched (ELF.sha256 of the WP24 images);
* phase 0x201 of b3_b411.elf runs the new kernel (b3.elf's still runs F-B4-1);
* no flag-setting encoding lies between each ``cmp`` and its flag readers (b3_b411.lst);
* the oracle is poison-independent and every (sub-phase, flag) deletion is predicted to
  change exactly the iterations whose poison bit differs from the true flag.
"""

from __future__ import annotations

import os
import re
import struct
import sys
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

FX = fastdyn_root() / "fixtures" / "b3"
WP24_ELFS = {
    "b3.elf": "3b0296188a1f2463d28b7c8fca5d2d11802286b0e8685812edb1a4b37eb0b581",
    "b3_O0.elf": "a522afd25d92436033336b9b6ac7fbd723d732bdb9130e8ef8994c7312a17cc6",
    "b3_Os.elf": "182b7fc208c09db0fca67b11dddbf2148cacba90c9a7c2d691ba5d4ec01ed3f2",
    "b3_16d.elf": "6472b76dd22c7f4e4ab7edf7082a8c6ea2a235bebb6854fe78488254c4cf806f",
}
B411_SHA = "4a7ec91e7787924b4d545839c83d54adbdb2c5a46d412ec884a7a4d8f3fb0440"

pytestmark = pytest.mark.real


@pytest.fixture(scope="module")
def fx():
    if not (FX / "b3_b411.elf").is_file():
        pytest.skip(f"{FX}/b3_b411.elf not present")
    import importlib.util

    def load(name: str, alias: str):
        spec = importlib.util.spec_from_file_location(alias, FX / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod  # dataclasses resolve their module through sys.modules
        spec.loader.exec_module(mod)
        return mod

    # expected.py does `import b3conf` (its own directory): give it the fixture's copy
    sys.path.insert(0, str(FX))
    saved = sys.modules.pop("b3conf", None)
    try:
        b3conf = load("b3conf", "b3conf")
        ex = load("expected", "_closeout_b3_expected")
    finally:
        sys.path.remove(str(FX))
        if saved is not None:
            sys.modules["b3conf"] = saved
        else:
            sys.modules.pop("b3conf", None)
    return b3conf, ex


def _sha(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_images(fx):
    for name, sha in WP24_ELFS.items():
        assert _sha(FX / name) == sha, name
    assert _sha(FX / "b3_b411.elf") == B411_SHA
    elf_sha = dict(line.split()[::-1] for line in (FX / "ELF.sha256").read_text().splitlines())
    assert elf_sha["b3_b411.elf"] == B411_SHA
    assert all(elf_sha[n] == s for n, s in WP24_ELFS.items())


def _table_target(elf: str) -> list[int]:
    d = (FX / elf).read_bytes()
    out, i = [], d.find(struct.pack("<I", 0x201))
    while i != -1:
        fn = struct.unpack_from("<I", d, i + 4)[0]
        if fn & 1 and fn < 0x10000:
            out.append(fn & ~1)
        i = d.find(struct.pack("<I", 0x201), i + 1)
    return out


def test_phase_table_resolves_to_the_new_kernel(fx):
    b3conf, _ = fx
    new = b3conf.symbols("b3_b411.elf")["phase_b4_1"] & ~1
    old = b3conf.symbols("b3.elf")["phase_b4_1"] & ~1
    assert new in _table_target("b3_b411.elf") and old not in _table_target("b3_b411.elf")
    assert old in _table_target("b3.elf")
    cfg = b3conf.BY_NAME["b4-11"]
    assert (cfg.fixture, cfg.phase, cfg.elf) == ("F-B4-11", 0x201, "b3_b411.elf")
    assert cfg.virts == [("b4_11_pp", "printreg", ["4"])] and not cfg.machine


_FLAGSET = re.compile(r"^(adds|subs|movs|lsls|lsrs|asrs|ands|orrs|eors|bics|mvns|negs|rsbs|adcs|"
                      r"sbcs|muls|rors|cmp|cmn|tst|teq)\b")


def test_no_flag_setting_between_cmp_and_readers(fx):
    lst = (FX / "b3_b411.lst").read_text()
    for lab, reader in (("b4_11_a", "mrs"), ("b4_11_b", "it"), ("b4_11_c", "it")):
        m = re.search(rf"^[0-9a-f]+ <{lab}>:\n(.*?)\n\s*[0-9a-f]+:\s+[0-9a-f]{{4}}\s+b\.n",
                      lst, re.S | re.M)
        assert m, lab
        ins = [re.split(r"\t", x.strip())[-2] + " " + re.split(r"\t", x.strip())[-1]
               for x in m.group(1).splitlines() if re.match(r"\s+[0-9a-f]+:\t", x)]
        assert ins[0].startswith("cmp"), (lab, ins)
        assert any(i.startswith(reader) for i in ins), (lab, ins)
        flagset = [i for i in ins[1:] if _FLAGSET.match(i)]
        assert not flagset, (lab, flagset)
        if lab == "b4_11_a":
            assert "mrs\tr2, CPSR" in m.group(1) or "mrs r2, CPSR" in " ".join(ins)
        if lab == "b4_11_b":
            assert "PRIMASK" in m.group(1)


def test_oracle_is_poison_independent_and_every_deletion_diverges(fx):
    _, ex = fx
    w = ex.b411_words()
    assert len(w) == 48
    for s in range(3):
        for i in range(0, 16, 2):  # both poisons of a pair give the same word
            assert w[16 * s + i] == w[16 * s + i + 1]
    for sub in "ABC":
        for f in "NZCV":
            d = ex.b411_divergence(sub, f)
            assert len(d) == 8, (sub, f)
            s = "ABC".index(sub)
            assert all(0x10 + 16 * s <= k < 0x10 + 16 * (s + 1) for k in d)
    # every flag takes both values over the pair table
    vals = [ex.nzcv_sub(a, b) for a, b in ex.B411_PAIRS]
    assert all({v[k] for v in vals} == {0, 1} for k in range(4))


def test_frozen_oracle_satisfies_the_invariants(fx):
    _, ex = fx
    norm = FX / "expected" / "b4-11.norm"
    assert not ex.compare(norm.read_text(), "b4-11", "icount")
