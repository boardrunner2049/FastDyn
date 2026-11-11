from pathlib import Path
from types import SimpleNamespace

import pytest

from fastdyn.host import (AnalysisSession, AnalysisSessionPool, ImageAddressError,
                          AmbiguousSymbolError, UnsupportedTargetError,
                          ConstantPropagation, KnownMemoryAccess,
                          constant_member_offset,
                          find_known_memory_accesses,
                          resolve_address)
from fastdyn.host.symbols import SymbolIndex


VARIABLE_ELF = Path("tests/binaries/variable_watch/variable_watch.elf")
FREERTOS_ELF = Path("tests/binaries/freertos_stm32f429i_discovery/freertos_stm32f429i_discovery.elf")


def arm_session(path):
    return AnalysisSession.open(path, architecture="arm", instruction_mode="thumb-mclass")


def test_session_is_lazy_and_pool_reuses_only_matching_target(tmp_path):
    pool = AnalysisSessionPool()
    first = pool.get(VARIABLE_ELF, architecture="arm", instruction_mode="thumb-mclass")
    second = pool.get(VARIABLE_ELF.resolve(), architecture="ARM", instruction_mode="thumb-mclass")
    other = pool.get(VARIABLE_ELF, architecture="arm", instruction_mode="default")
    assert first is second
    assert first is not other
    assert first._elf is None
    first.symbols.all()
    assert first._elf is not None
    pool.close()
    assert first._elf is None


def test_run_contexts_share_the_pool_session_without_parsing(tmp_path):
    from fastdyn.virtual_preprocessing import _run_context_for_cpu

    machine = SimpleNamespace(irq_map={})
    cpu = SimpleNamespace(binary=str(VARIABLE_ELF), arch="arm", machine="cortexm",
                          cpu="cortex-m3", symbol_dict={}, plugin_config={},
                          machine_obj=machine)
    pool = AnalysisSessionPool()
    first = _run_context_for_cpu(cpu, tmp_path, "first", pool)
    second = _run_context_for_cpu(cpu, tmp_path, "second", pool)
    assert first.analysis is second.analysis
    assert first.analysis._elf is None
    pool.close()


def test_symbols_normalize_only_functions_and_preserve_aliases_and_zero():
    session = arm_session(FREERTOS_ELF)
    reset = session.symbols.lookup("Reset_Handler", kind="function")
    assert reset.address & 1
    assert reset.code_address == reset.address & ~1
    assert "_start" in reset.aliases

    variable = arm_session(VARIABLE_ELF).symbols.lookup("vectors")
    assert variable.address == variable.code_address == 0


def test_inferred_function_ranges_stay_in_their_section():
    session = arm_session(FREERTOS_ELF)
    inferred = [item for item in session.symbols.function_ranges()
                if item.range_provenance == "inferred"]
    assert inferred
    sections = {section.name: (int(section["sh_addr"]),
                               int(section["sh_addr"]) + int(section["sh_size"]))
                for section in session.elf.iter_sections()}
    assert all(sections[item.section][0] <= item.start < item.end <= sections[item.section][1]
               for item in inferred)


def test_image_reads_distinguish_file_zero_fill_and_unmapped():
    image = arm_session(VARIABLE_ELF).image
    assert image.read(0, 4).source == "file"
    zero = image.read(0x20000000, 12)
    assert zero.source == "zero-fill"
    assert zero.data == bytes(12)
    with pytest.raises(ImageAddressError, match="unmapped"):
        image.read(0x10000000, 4)


def test_dwarf_member_expression_decodes_multibyte_uleb128():
    dwarf = arm_session(VARIABLE_ELF).dwarf._dwarf
    assert constant_member_offset(dwarf, bytes((0x23, 0xAC, 0x02))) == 300


def test_shared_dwarf_global_and_nested_member_query():
    dwarf = arm_session(VARIABLE_ELF).dwarf
    parent = dwarf.global_variable("motor_state")
    field = dwarf.global_variable("motor_state.temperature")
    assert parent is not None and parent.type.size == 12
    assert field is not None and field.address >= parent.address
    assert dwarf.global_variable("motor_state.missing") is None


def test_decoder_return_forms_and_unsupported_mode():
    session = arm_session(VARIABLE_ELF)
    assert any(item.mnemonic == "bx" for item in session.decoder.instructions()
               if item.return_site)
    unsupported = AnalysisSession.open(VARIABLE_ELF, architecture="arm")
    with pytest.raises(UnsupportedTargetError):
        _ = unsupported.decoder


def test_constant_propagation_is_shared_cached_and_address_class_agnostic():
    # Vector table; literal load of 0x40010000; word load through the recovered
    # base. The shared service reports the known address without classifying it.
    blob = bytes.fromhex(
        "00100020" "09000000" "014c" "2068" "fee7" "00bf" "00000140"
    )
    direct = find_known_memory_accesses(
        blob=blob, base_va=0, arch="arm32", mode="thumb",
    )
    assert [(item.address, item.direction, item.transfer_size) for item in direct] == [
        (0x40010000, "read", 4),
    ]

    shared = ConstantPropagation(
        ((0, blob),), architecture="arm", instruction_mode="thumb-mclass",
    )
    first = shared.memory_accesses()
    assert first is shared.memory_accesses()
    assert first == tuple(direct)

    session = arm_session(VARIABLE_ELF)
    assert session.constant_propagation is session.constant_propagation
    with pytest.raises(UnsupportedTargetError):
        find_known_memory_accesses(blob=blob, base_va=0, arch="mips")


def test_mmio_discovery_filters_shared_constant_facts_without_redecoding():
    from fastdyn.mmio_plan import discover_firmware

    access = KnownMemoryAccess(
        pc=0x08000100, instruction_size=2, transfer_size=4,
        direction="read", arch="ARM32-THUMB", instruction="ldr r0, [r1]",
        base_register="r1", address=0x40010000, note="unit",
    )

    class SharedConstants:
        def memory_accesses(self):
            return (access,)

    analysis = SimpleNamespace(constant_propagation=SharedConstants())
    assert discover_firmware(
        VARIABLE_ELF, ((0x40010000, 0x400100ff),), analysis=analysis,
    ) == [access]
    assert discover_firmware(
        VARIABLE_ELF, ((0x50000000, 0x500000ff),), analysis=analysis,
    ) == []


def test_register_list_memory_transfers_remain_candidates():
    from virtuals.utils.object_access_analysis import ObjectAccessAnalysis

    session = arm_session("tests/binaries/rtos/zephyr.elf")
    register_lists = [item for item in session.decoder.instructions()
                      if item.mnemonic.startswith(("pop", "ldm", "stm"))
                      and not item.has_memory_operand]
    candidates = {item.pc for item in ObjectAccessAnalysis(session).candidate_accesses()}
    assert register_lists
    assert all(item.address in candidates for item in register_lists)


def test_resolution_supports_numeric_and_symbol_offsets():
    assert resolve_address("0x20", {}) == 0x20
    assert resolve_address("thing+12", {"thing": 0x100}) == 0x10C
    assert resolve_address("thing-0x10", {"thing": 0x100}) == 0xF0


def test_symbol_index_preserves_odd_data_and_rejects_ambiguity(monkeypatch):
    import fastdyn.host.symbols as symbol_module

    class Section:
        name = ".text"
        def __getitem__(self, key):
            return {"sh_addr": 0x100, "sh_size": 0x300, "sh_flags": 0x4}[key]

    class Table:
        name = ".symtab"
        def __init__(self, entries):
            self.entries = entries
        def __getitem__(self, key):
            return {"sh_addr": 0, "sh_size": 0, "sh_flags": 0}[key]
        def iter_symbols(self):
            return iter(self.entries)

    def entry(name, address, kind, shndx=0):
        return SimpleNamespace(
            name=name,
            entry=SimpleNamespace(st_shndx=shndx, st_info=SimpleNamespace(type=kind),
                                  st_value=address, st_size=0),
        )

    monkeypatch.setattr(symbol_module, "SymbolTableSection", Table)
    table = Table([entry("odd_data", 0x101, "STT_OBJECT"),
                   entry("same", 0x201, "STT_FUNC"),
                   entry("same", 0x301, "STT_FUNC"),
                   entry("undefined", 0, "STT_FUNC", "SHN_UNDEF")])
    index = SymbolIndex(SimpleNamespace(iter_sections=lambda: iter((Section(), table))),
                        normalize_thumb_functions=True)
    odd = index.lookup("odd_data")
    assert odd.address == odd.code_address == 0x101
    with pytest.raises(AmbiguousSymbolError):
        index.lookup("same")
