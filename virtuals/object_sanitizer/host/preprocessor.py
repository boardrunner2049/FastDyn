"""Host-side ObjectSan analysis and declarative instrumentation planning.

The runtime deliberately receives no ELF, DWARF, RTOS, or allocator knowledge.
This module resolves those details into manifests and ordinary FastDyn virtual
rules before QEMU starts.
"""
from __future__ import annotations

from dataclasses import dataclass

from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG
from fastdyn.host import ImageAddressError

from fastdyn.machine import VirtualInstruction
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext, RunDefinition, RunPrepareResult, VirtualDefinition,
    VirtualPreparationError, register_run_preprocessor, register_virtual,
)
try:  # Keep the module unit-testable without FastDyn's dynamic plugin loader.
    from _fastdyn_virtual_utils.rtos_models import identify_rtos
    from _fastdyn_virtual_utils.object_access_analysis import ObjectAccessAnalysis
except ModuleNotFoundError:
    from virtuals.utils.rtos_models import identify_rtos
    from virtuals.utils.object_access_analysis import ObjectAccessAnalysis
from .allocators import AllocationAPI, select_models


MAX_SITES = 1024


@dataclass(frozen=True)
class _CallSite:
    id: int
    api: AllocationAPI
    call_pc: int
    return_pc: int
    owner: str
    source: str


def _static_object(ctx: RunContext, selected: str):
    if ctx.analysis.elf.has_dwarf_info():
        item = ctx.analysis.dwarf.global_variable(selected)
        if item is not None and item.type.size:
            return item.address, item.type.size, "dwarf"
    matches = [item for item in ctx.analysis.symbols.find(selected, kind="object") if item.size]
    if len({(item.address, item.size) for item in matches}) == 1:
        item = matches[0]
        return item.address, item.size, "symbol"
    raise VirtualPreparationError(
        f"object_sanitizer could not resolve static object {selected!r} with a fixed address and size"
    )


def _static_objects_of_type(ctx: RunContext, selected: str) -> list[tuple[str, int, int, str]]:
    """Resolve all fixed-address globals whose DWARF type matches ``selected``."""
    if not ctx.analysis.elf.has_dwarf_info():
        raise VirtualPreparationError("object_sanitizer type selection requires DWARF")
    results = {item.address: (item.name, item.address, item.type.size, "dwarf")
               for item in ctx.analysis.dwarf.globals_of_type(selected) if item.type.size}
    if not results:
        raise VirtualPreparationError(f"object_sanitizer found no fixed-address globals of type {selected!r}")
    return [results[address] for address in sorted(results)]


def _arm_register(instruction, register: int) -> int:
    name = instruction.reg_name(register).lower()
    if name.startswith("r") and name[1:].isdigit() and int(name[1:]) <= 12:
        return int(name[1:])
    return {"sp": 13, "lr": 14, "pc": 15}.get(name, -1)


def _instruction_plan(ctx: RunContext, static_bases: dict[int, int],
                      candidates: frozenset[int]) -> list[tuple[int, str, int, int, int]]:
    """Produce conservative ARM Thumb register/memory propagation semantics.

    Unknown arithmetic is deliberately over-tainted with all its input tags.
    That retains protected provenance; it can cost reporting precision but never
    turns an unproven data flow into an omission.
    """
    if not ctx.architecture.lower().startswith("arm"):
        raise VirtualPreparationError("object_sanitizer currently requires ARM Thumb/M-profile instruction planning")
    result: dict[int, tuple[str, int, int, int]] = {}
    pending_movw: dict[int, int] = {}
    for insn in ctx.analysis.decoder.native_instructions():
        address = int(insn.address) & ~1
        operands = insn.operands
        reads, writes = insn.regs_access()
        read_regs = [_arm_register(insn, reg) for reg in reads]
        write_regs = [_arm_register(insn, reg) for reg in writes]
        read_regs = [reg for reg in read_regs if 0 <= reg <= 14]
        write_regs = [reg for reg in write_regs if 0 <= reg <= 14]
        dest = (_arm_register(insn, operands[0].reg)
                if operands and operands[0].type == ARM_OP_REG
                else (write_regs[0] if write_regs else -1))
        mem = next((operand.mem for operand in operands if operand.type == ARM_OP_MEM), None)
        base = _arm_register(insn, mem.base) if mem and mem.base else -1
        mnemonic = insn.mnemonic.lower()
        if mnemonic.startswith('str') and mem and address in candidates:
            source = (_arm_register(insn, operands[0].reg)
                      if operands and operands[0].type == ARM_OP_REG else -1)
            result[address] = ('store', -1, source, base)
        elif mnemonic.startswith('ldr') and mem and address in candidates:
            try:
                literal = int.from_bytes(ctx.analysis.image.read_bytes(
                    ((address + 4) & ~3) + mem.disp, 4), "little") if base == 15 else None
            except ImageAddressError:
                literal = None
            seed = static_bases.get(literal)
            result[address] = (('seed', dest, seed, base)
                               if seed is not None and dest >= 0
                               else ('load', dest, -1, base))
        elif (mnemonic == 'movw' and dest >= 0 and len(operands) > 1
              and operands[1].type == ARM_OP_IMM):
            pending_movw[dest] = operands[1].imm & 0xffff
            result[address] = ('clear', dest, -1, -1)
        elif (mnemonic == 'movt' and dest >= 0 and len(operands) > 1
              and operands[1].type == ARM_OP_IMM):
            value = ((operands[1].imm & 0xffff) << 16) | pending_movw.get(dest, 0)
            seed = static_bases.get(value)
            result[address] = (('seed', dest, seed, -1) if seed is not None
                               else ('clear', dest, -1, -1))
            pending_movw.pop(dest, None)
        elif (mnemonic == 'adr' and dest >= 0 and len(operands) > 1
              and operands[1].type == ARM_OP_IMM):
            seed = static_bases.get(operands[1].imm)
            result[address] = (('seed', dest, seed, -1) if seed is not None
                               else ('clear', dest, -1, -1))
        elif dest >= 0:
            sources = [reg for reg in read_regs if reg != 15]
            result[address] = ('propagate', dest, sources[0] if sources else -1,
                               sources[1] if len(sources) > 1 else -1)
    return [(address, *value) for address, value in sorted(result.items())]


def _direct_calls(ctx: RunContext, symbols: set[str]) -> list[tuple[str, int, int, str, str]]:
    addresses = {item.name: item.code_address for item in ctx.analysis.symbols.all()
                 if item.name in symbols}
    targets = {addresses[name]: name for name in symbols if name in addresses}
    result = []
    for insn in ctx.analysis.decoder.native_instructions():
        if (insn.mnemonic.lower() not in {'bl', 'blx'} or not insn.operands
                or insn.operands[0].type != ARM_OP_IMM):
            continue
        name = targets.get(int(insn.operands[0].imm) & ~1)
        if name:
            pc = int(insn.address) & ~1
            owner = ctx.analysis.symbols.containing_function(pc)
            source = (ctx.analysis.dwarf.source_location(pc)
                      if ctx.analysis.elf.has_dwarf_info() else None)
            source_text = f"{source.file}:{source.line}" if source else ""
            result.append((name, pc, pc + insn.size,
                           owner.name if owner else "<unknown>", source_text))
    return result


def _rtos_identity(ctx: RunContext) -> str | None:
    """Reuse the Introspection plugin's RTOS signature database."""
    symbols = {symbol.name: None for symbol in ctx.analysis.symbols.all()}
    identified = identify_rtos(symbols)
    return None if identified == "Unknown/Custom Baremetal" else identified


def _allocation_sites(ctx: RunContext, apis: tuple[AllocationAPI, ...], settings: dict[str, object]) -> list[_CallSite]:
    if not apis:
        raise VirtualPreparationError("dynamic ObjectSan requires allocator_model or [[allocators]]")
    by_name = {api.allocate: api for api in apis}
    found = [(by_name[name], call, ret, owner, source) for name, call, ret, owner, source in _direct_calls(ctx, set(by_name))]
    function, ordinal, allocation_site = settings.get('function'), settings.get('allocation'), settings.get('allocation_site')
    if function is not None and (not isinstance(function, str) or not function):
        raise VirtualPreparationError("object_sanitizer.function must be a non-empty function name")
    if ordinal is not None and (isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1):
        raise VirtualPreparationError("object_sanitizer.allocation must be a one-based positive integer")
    if allocation_site is not None and (not isinstance(allocation_site, str) or not allocation_site):
        raise VirtualPreparationError("object_sanitizer.allocation_site must be file:line")
    if function:
        found = [item for item in found if item[3] == function]
    if allocation_site:
        found = [item for item in found if item[4].endswith(allocation_site)]
    if ordinal:
        if len(found) < ordinal:
            raise VirtualPreparationError("object_sanitizer allocation selection exceeds matching allocation calls")
        found = [found[ordinal - 1]]
    if not (function or allocation_site or ordinal):
        raise VirtualPreparationError("dynamic ObjectSan selection requires allocation_site or function plus allocation")
    if not found:
        raise VirtualPreparationError("object_sanitizer selected no direct allocator call sites")
    if len(found) > MAX_SITES:
        raise VirtualPreparationError(f"object_sanitizer selected more than {MAX_SITES} allocation sites")
    return [_CallSite(index, api, call, ret, owner, source)
            for index, (api, call, ret, owner, source) in enumerate(found, start=1)]


class ObjectSanRunPreprocessor:
    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        settings = dict(ctx.settings)
        name = settings.get("object")
        type_name = settings.get("type")
        dynamic_requested = any(key in settings for key in ("allocation_site", "function", "allocation"))
        static_requested = bool(name) or bool(type_name)
        if static_requested == dynamic_requested or bool(name) and bool(type_name):
            raise VirtualPreparationError("object_sanitizer needs exactly one object=NAME, type=NAME, or dynamic allocation selection")
        if name is not None and (not isinstance(name, str) or not name):
            raise VirtualPreparationError("object_sanitizer.object must name one static object")
        if type_name is not None and (not isinstance(type_name, str) or not type_name):
            raise VirtualPreparationError("object_sanitizer.type must name one DWARF type")
        rtos = _rtos_identity(ctx) if dynamic_requested and "allocator_model" not in settings else None
        models = select_models(settings, rtos)
        apis = tuple(api for model in models for api in model.apis)
        static_objects = ([(name, *_static_object(ctx, name))] if name
                          else _static_objects_of_type(ctx, type_name) if type_name else [])
        if len(static_objects) > 63:
            raise VirtualPreparationError("object_sanitizer supports at most 63 simultaneously selected objects")
        sites = _allocation_sites(ctx, apis, settings) if dynamic_requested else []
        objects, violations = ctx.plugin_artifact_path("objects.tsv"), ctx.plugin_artifact_path("violations.tsv")
        object_events = ctx.plugin_artifact_path("object_events.tsv")
        allocators, plan, sites_path = (ctx.plugin_artifact_path(item) for item in ("allocators.tsv", "instructions.tsv", "allocation_sites.tsv"))
        objects.write_text("object_id\tname\tbase\tsize\tkind\tstate\tsource\n" + "".join(
            f"{index}\t{static_name}\t0x{address:x}\t{size}\tstatic\tLIVE\t{source}\n"
            for index, (static_name, address, size, source) in enumerate(static_objects, start=1)
        ), encoding="utf-8")
        violations.write_text("object_id\tname\tpc\taccess\taddress\tsize\treason\n", encoding="utf-8")
        object_events.write_text("event\tobject_id\tname\tbase\tsize\tstate\n", encoding="utf-8")
        allocators.write_text("name\tallocate\tsize_arg\tfree\tpointer_arg\treturn_pointer_arg\tallocator_id\theap_id\n" + "".join(f"{api.name}\t{api.allocate}\t{api.size_argument}\t{api.free or ''}\t{api.free_pointer_argument}\t{api.return_pointer_argument if api.return_pointer_argument is not None else -1}\t{api.allocator_id}\t{api.heap_id}\n" for api in apis), encoding="utf-8")
        sites_path.write_text("site_id\tapi\tcall_pc\treturn_pc\tsize_arg\treturn_pointer_arg\tallocator_id\theap_id\towner\tsource\n" + "".join(f"{site.id}\t{site.api.name}\t0x{site.call_pc:x}\t0x{site.return_pc:x}\t{site.api.size_argument}\t{site.api.return_pointer_argument if site.api.return_pointer_argument is not None else -1}\t{site.api.allocator_id}\t{site.api.heap_id}\t{site.owner}\t{site.source}\n" for site in sites), encoding="utf-8")
        candidates = frozenset(item.pc for item in ObjectAccessAnalysis(ctx.analysis).candidate_accesses())
        semantic_plan = _instruction_plan(
            ctx, {address: index for index, (_name, address, _size, _source) in enumerate(static_objects)}, candidates
        )
        plan.write_text("pc\tkind\tdest\tsource\tbase\n" + "".join(f"0x{pc:x}\t{kind}\t{dest}\t{source}\t{base}\n" for pc, kind, dest, source, base in semantic_plan), encoding="utf-8")
        virtuals = [item for site in sites for item in (
            VirtualInstruction(at=site.call_pc, instruction="object_sanitizer_alloc_call", args=[str(site.id)]),
            VirtualInstruction(at=site.return_pc, instruction="object_sanitizer_alloc_return", args=[str(site.id)]),
        )]
        free_apis = {api.free: api for api in apis if api.free}
        for free_name, call, _ret, _owner_name, _source in _direct_calls(ctx, set(free_apis)):
            virtuals.append(VirtualInstruction(at=call, instruction="object_sanitizer_free", args=[str(free_apis[free_name].free_pointer_argument)]))
        ctx.logger.info("object_sanitizer generated %d conservative instruction semantics and %d allocation sites", len(semantic_plan), len(sites))
        return RunPrepareResult(virtuals=virtuals, artifacts=[objects, violations, object_events, allocators, plan, sites_path])


register_virtual(VirtualDefinition(name="object_sanitizer_alloc_call"))
register_virtual(VirtualDefinition(name="object_sanitizer_alloc_return"))
register_virtual(VirtualDefinition(name="object_sanitizer_free"))
register_run_preprocessor(RunDefinition(
    name="object_sanitizer", prepare=ObjectSanRunPreprocessor(),
    enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
    help=ConfigurationHelp("Object-guided spatial and temporal memory-safety checking.",
        '[CPU.cpu0.plugins.object_sanitizer]\nenabled = true\nobject = "packet_buf"',
        "docs/ObjectSan.md"),
))
