"""WP12 fixture consistency checks (static; no QEMU).

The fixtures live outside this repo in $FASTDYN_ROOT/fixtures:
  fault/                     fault-attribution images (F-B2-3)
  freertos-mps2-compute/     compute-heavy FreeRTOS workload (F-B2-7)
  inputs/{train,heldout,final}/freertos-mps2-compute/
These tests check that the generated TOML templates, the expected outputs
and the input files agree with the ELF images and with each other.  The
dynamic checks (nocache/cold/warm, fingerprint invariance) are
tests/qemu/wp12_fixtures_smoke.sh.
"""
import binascii
import hashlib
import importlib.util
import json
import os
import re
import struct
import tomllib

import pytest
from scopt.paths import fastdyn_root

FIX = str(fastdyn_root() / "fixtures")
FAULT = os.path.join(FIX, "fault")
COMPUTE = os.path.join(FIX, "freertos-mps2-compute")
INPUTS = os.path.join(FIX, "inputs")
COMPUTE_ELF = os.path.join(COMPUTE, "FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/build/gcc/output/RTOSDemo.out")
VARIANTS = ["a", "b", "c", "d", "fixture"]

pytestmark = pytest.mark.skipif(not os.path.isdir(FAULT) or not os.path.isdir(COMPUTE),
                                reason="WP12 fixtures not present")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _elf_symbols(path):
    from elftools.elf.elffile import ELFFile
    out = {}
    with open(path, "rb") as f:
        elf = ELFFile(f)
        for sym in elf.get_section_by_name(".symtab").iter_symbols():
            if sym.name:
                out[sym.name] = sym["st_value"] & ~1
    return out


def _template(path):
    text = open(path).read()
    # fdrun.py fills these lines; any value parses
    text = text.replace("@SUPERCACHE@", "").replace("@ICOUNT@", "").replace("@QEMU@", "/q")
    return tomllib.loads(text), text


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ------------------------------------------------------------------ fault
@pytest.mark.parametrize("v", VARIANTS)
def test_fault_elf_sha_recorded(v):
    recorded = dict(line.split()[::-1] for line in open(os.path.join(FAULT, "ELF.sha256")))
    assert recorded[f"fault_{v}.elf"] == _sha(os.path.join(FAULT, f"fault_{v}.elf"))


@pytest.mark.parametrize("v", VARIANTS)
def test_fault_template_virtuals_match_elf(v):
    sym = _elf_symbols(os.path.join(FAULT, f"fault_{v}.elf"))
    cfg, text = _template(os.path.join(FAULT, f"fault-{v}.toml.in"))
    virt = cfg["CPU"]["cpu0"][0]["virtuals"]
    by_addr = {int(x["at"], 16): x for x in virt}
    assert by_addr[sym["fault_bench_start"]]["instruction"] == "benchmark_start"
    assert by_addr[sym["fault_all_done"]]["instruction"] == "benchmark_end"
    if v != "d":
        for name, reg in (("fh_p0", "0"), ("fh_p1", "1"), ("fh_p2", "2"), ("fh_p3", "3")):
            assert by_addr[sym[name]] == {"at": f"0x{sym[name]:08x}", "instruction": "printreg", "args": [reg]}
    assert cfg["CPU"]["cpu0"][0]["binary"] == os.path.join(FAULT, f"fault_{v}.elf")
    # run paths are relative so a rendered TOML is identical in every run dir
    assert cfg["Memory"]["main"]["memory_file"] == "ram0.ram"
    assert cfg["Machine"]["qmp_socket"] == "qmp.sock"


@pytest.mark.parametrize("v", VARIANTS)
def test_fault_expected_pcs_are_elf_addresses(v):
    """The stacked-PC lines of expected/<v>.norm equal the faulting insns' ELF addresses."""
    sym = _elf_symbols(os.path.join(FAULT, f"fault_{v}.elf"))
    exp = _load(os.path.join(FAULT, "expected.py"), f"fault_expected_{v}")
    text = exp.expected(v)
    assert text == open(os.path.join(FAULT, "expected", f"{v}.norm")).read()
    lines = text.splitlines()
    order = {"a": "a", "b": "b", "c": "c", "d": "", "fixture": "abc"}[v]
    fault_sym = {"a": "a_fault_ldr", "b": "b_fault_sdiv", "c": "c_fault_ldrb"}
    pcs = [int(lines[i + 1].split(":")[1], 16) for i, l in enumerate(lines)
           if re.fullmatch(r"Reg 0:0x0000000[abc]", l)]
    assert pcs == [sym[fault_sym[x]] for x in order]


# --------------------------------------------------------------- compute
def test_compute_elf_sha_recorded():
    assert open(os.path.join(COMPUTE, "RTOSDemo.sha256")).read().strip() == _sha(COMPUTE_ELF)


def test_compute_template_matches_elf_and_has_input_bank():
    sym = _elf_symbols(COMPUTE_ELF)
    cfg, text = _template(os.path.join(COMPUTE, "configs", "freertos-compute.toml.in"))
    virt = {int(x["at"], 16): x for x in cfg["CPU"]["cpu0"][0]["virtuals"]}
    assert virt[sym["vComputeWindowStart"]]["instruction"] == "benchmark_start"
    assert virt[sym["vComputeDone"] + 2]["instruction"] == "benchmark_end"
    for i in range(1, 5):
        for r in range(4):
            assert virt[sym[f"vComputeReport{i}"] + 2 * r] == {
                "at": f"0x{sym[f'vComputeReport{i}'] + 2 * r:08x}", "instruction": "printreg", "args": [str(r)]}
    banks = cfg["Memory"]["input"]
    bank = banks[0] if isinstance(banks, list) else banks
    assert bank["id"] == "input" and int(bank["base_address"], 16) == 0x30000000
    assert bank["memory_file"] == "input.bin"
    # nothing input-specific is in the template (inputs must not touch the fingerprint)
    assert ".bin\"" not in text.replace('memory_file  = "input.bin"', "")
    assert "inputs/" not in text


def test_compute_firmware_input_constants():
    src = open(os.path.join(COMPUTE, "FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/main_compute.c")).read()
    gen = _load(os.path.join(COMPUTE, "gen_inputs.py"), "gen_inputs")
    assert f"0x{gen.MAGIC:08X}UL" in src
    assert f"#define MAX_PAYLOAD         {gen.MAX_PAYLOAD}U" in src
    assert "#define INPUT_BASE          0x30000000UL" in src


SPLITS = {"train": ["train-1", "train-2", "train-3"],
          "heldout": ["heldout-1", "heldout-2", "heldout-3"],
          "final": ["final-1", "final-2", "final-3"]}


@pytest.mark.parametrize("split", sorted(SPLITS))
def test_inputs_manifest_and_model(split):
    gen = _load(os.path.join(COMPUTE, "gen_inputs.py"), "gen_inputs")
    d = os.path.join(INPUTS, split, "freertos-mps2-compute")
    man = json.load(open(os.path.join(d, "MANIFEST.json")))
    assert sorted(man["inputs"]) == SPLITS[split]
    for name, e in man["inputs"].items():
        blob = open(os.path.join(d, e["file"]), "rb").read()
        assert hashlib.sha256(blob).hexdigest() == e["sha256"]
        assert len(blob) <= 1 << 20
        magic, ver, slen, passes, seed, frames, scrc, hcrc = struct.unpack("<8I", blob[:32])
        assert (magic, ver) == (gen.MAGIC, 1)
        assert binascii.crc32(blob[:28]) == hcrc
        stream = blob[32:32 + slen]
        assert binascii.crc32(stream) == scrc and slen == len(blob) - 32
        counters, _ = gen.model_rx(stream, passes)
        assert counters == e["model_rx"]
        # deterministic regeneration from the recorded seed
        prof = gen.PROFILES[e["profile"]]
        again, _, _ = gen.build(seed, frames, passes, prof)
        assert again == blob


def test_input_splits_are_distinct():
    shas = []
    for split, names in SPLITS.items():
        man = json.load(open(os.path.join(INPUTS, split, "freertos-mps2-compute", "MANIFEST.json")))
        shas += [man["inputs"][n]["sha256"] for n in names]
    assert len(set(shas)) == 9


@pytest.mark.parametrize("name", [n for names in SPLITS.values() for n in names])
def test_compute_expected_norm_counters_match_model(name):
    """expected/<input>.icount.norm (observed) agrees with the model's counters."""
    split = name.split("-")[0]
    man = json.load(open(os.path.join(INPUTS, split, "freertos-mps2-compute", "MANIFEST.json")))
    m = man["inputs"][name]["model_rx"]
    regs = [int(x, 16) for x in re.findall(r"^Reg [0-3]:0x([0-9a-f]{8})$",
                                           open(os.path.join(COMPUTE, "expected", f"{name}.icount.norm")).read(), re.M)]
    assert len(regs) == 16
    assert regs[1] == regs[2] == m["frames_ok"] and regs[3] == 0
    assert regs[4:8] == [m["err_crc"], m["err_len"], m["err_seq"], m["garbage_bytes"]]
