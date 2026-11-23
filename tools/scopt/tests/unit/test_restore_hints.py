from copy import deepcopy
from types import SimpleNamespace

import pytest

from scopt.canon import sha256_id
from scopt.formats.enums import TbFlag
from scopt.run.restore_hints import (RestoreHintsError, _record_eligible, callback_addresses,
                                     validate_restore_hints)

HINT = b"FD_SC_RESTORE_HINTS 1 0x10 0x20 1\n0x40 0xa\n"


def cache(record_checksum_ok=True):
    header = SimpleNamespace(version=7, reserved=0, checksum_ok=True,
                             runtime_signature=16, checksum=32,
                             record_count=1)
    record = SimpleNamespace(tb_offset=64, checksum=10, checksum_ok=record_checksum_ok)
    return SimpleNamespace(loadable=True, header=header, skipped=[], count=1, valid_slots=(0,),
                           records=[record])


@pytest.mark.parametrize("data,problem", (
    (HINT.replace(b"FD_SC", b"BADXX"), "magic"),
    (HINT.replace(b"0x20", b"0x21"), "header binding"),
    (HINT.replace(b"0xa\n", b"0xb\n"), "record/checksum"),
    (HINT + b"0x40 0xa\n", "duplicate"),
))
def test_refuse_bad_hint_binding(data, problem):
    with pytest.raises(RestoreHintsError, match=problem):
        validate_restore_hints(data, cache())


def test_refuse_corrupt_record_even_when_hint_matches_stored_checksum():
    assert validate_restore_hints(HINT, cache()) == ((64, 10),)
    with pytest.raises(RestoreHintsError, match="record/checksum"):
        validate_restore_hints(HINT, cache(False))


def private_configuration():
    config = {
        "Machine": {"platform": "generic-cortexm", "supercache": "cache.sc",
                    "supercache_flat": False, "log_options": "none", "coverage": False},
        "Memory": {"main": {"id": "ram0", "base_address": "0x20000000",
                            "memory_size": "64K", "backend": "file", "share": False}},
        "CPU": {"cpu0": [{"arch": "arm", "machine": "cortexm", "cpu": "cortex-m3",
                         "virtuals": [{"at": "0x8000110", "instruction": "benchmark_start"}],
                         "modifiers": [{"at": "0x8000118", "patch": "r0 <- 3"}]}]},
    }
    manifest = {"inputs": {
        "native_files": {"plugin": {"path": "/opt/FastDyn/build/libfastdyn.so"}},
        "settings": {"log_options": "none", "supercache_flat": False, "cpus": [
            {"arch": "arm", "machine": "cortexm", "cpu": "cortex-m3"}],
            "plugin_options": ["coverage=0", "edge_coverage=0", "fuzzing=0",
                               "exact_budget_stop=0", "twintrace=off"]},
        "content_files": {"device_config": {"size": 2, "sha256": sha256_id(b"{}")[7:]}},
    }}
    return config, manifest


def test_full_span_excludes_builtin_virtual_modifier_and_magic_callback():
    config, manifest = private_configuration()
    _, pcs = callback_addresses(config, manifest)
    chunk = SimpleNamespace(vaddr=0x0800010E, tb_chunk_version=1, flags=0,
                            native_fragments=(), stub_start=None, tb_flags=TbFlag(0),
                            insns=(1, 2), guest_bytes=b"\0"*4)
    unit = SimpleNamespace(meta_status="ok", origin="elf_verified", vaddrs=(chunk.vaddr,),
                           key=SimpleNamespace(page_addr1=None), icount=2, guest_size=4)
    assert not _record_eligible(unit, chunk, pcs)  # callback is in the second halfword
    chunk.vaddr = 0x08000112
    unit.vaddrs = (chunk.vaddr,)
    assert _record_eligible(unit, chunk, pcs)
    for address in (0x08000116, 0x2080004E):
        chunk.vaddr = address
        unit.vaddrs = (address,)
        assert not _record_eligible(unit, chunk, pcs)
    chunk.vaddr = 0x08000112
    unit.vaddrs = (chunk.vaddr,)
    chunk.tb_flags = TbFlag.PLUGIN_CBS_PRESENT
    chunk.flags = int(chunk.tb_flags)
    assert not _record_eligible(unit, chunk, pcs)


def test_config_rejects_logging_unknown_callbacks_and_dynamic_pc_writes():
    config, manifest = private_configuration()
    logging = deepcopy(config)
    logging["Machine"]["log_options"] = "in_asm"
    with pytest.raises(RestoreHintsError, match="tracing"):
        callback_addresses(logging, manifest)
    custom = deepcopy(config)
    custom["CPU"]["cpu0"][0]["virtuals"][0]["instruction"] = "custom"
    with pytest.raises(RestoreHintsError, match="unsupported virtual"):
        callback_addresses(custom, manifest)
    config["CPU"]["cpu0"][0]["modifiers"][0]["patch"] = "pc <- 0x1234"
    with pytest.raises(RestoreHintsError, match="PC-writing"):
        callback_addresses(config, manifest)
