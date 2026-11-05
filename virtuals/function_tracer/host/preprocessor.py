"""DWARF-assisted function-call tracer.

The host phase keeps debug-format parsing out of QEMU.  It translates each
selected subprogram's formal parameters into a compact, runtime-neutral
manifest: ABI entry location, scalar/pointer/aggregate shape, and the first
level of aggregate fields.  The C callback only reads guest state and writes
trace records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from math import ceil

from elftools.dwarf.die import DIE
from elftools.elf.elffile import ELFFile
from fastdyn.host import DwarfQueries

from fastdyn.machine import VirtualInstruction
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext,
    RunDefinition,
    RunPrepareResult,
    VirtualDefinition,
    VirtualPreparationError,
    register_run_preprocessor,
    register_virtual,
)


MAX_FUNCTIONS = 4096
MAX_ARGUMENTS = 8
MAX_FIELDS = 8
MAX_EVENT_LIMIT = 1_000_000


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    kind: str
    size: int
    signed: bool = False


@dataclass(frozen=True)
class TypeInfo:
    kind: str = "unknown"
    size: int = 0
    signed: bool = False
    fields: tuple[Field, ...] = ()


@dataclass(frozen=True)
class Argument:
    index: int
    name: str
    location: str
    size: int
    kind: str
    signed: bool
    fields: tuple[Field, ...] = ()


def _patterns(settings: dict[str, object], key: str) -> tuple[str, ...]:
    value = settings.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise VirtualPreparationError(
            f"[CPU.cpu0.plugins.function_tracer].{key} must be an array of glob patterns"
        )
    return tuple(value)


def _setting_int(settings: dict[str, object], key: str, default: int,
                 minimum: int, maximum: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VirtualPreparationError(
            f"[CPU.cpu0.plugins.function_tracer].{key} must be {minimum}..{maximum}"
        )
    return value


def _name(die: DIE) -> str | None:
    for attribute_name in ("DW_AT_linkage_name", "DW_AT_MIPS_linkage_name", "DW_AT_name"):
        attribute = die.attributes.get(attribute_name)
        if attribute:
            value = attribute.value
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return None


def _address(die: DIE) -> int | None:
    attribute = die.attributes.get("DW_AT_low_pc")
    return int(attribute.value) if attribute else None


def _type_die(die: DIE) -> DIE | None:
    attribute = die.attributes.get("DW_AT_type")
    return die.get_DIE_from_attribute("DW_AT_type") if attribute else None


def _byte_size(die: DIE | None, default: int = 0) -> int:
    if die is None:
        return default
    attribute = die.attributes.get("DW_AT_byte_size")
    return int(attribute.value) if attribute else default


def _type_info(die: DIE | None, pointer_size: int, queries: DwarfQueries,
               depth: int = 0) -> TypeInfo:
    """Return a bounded printable view of a DWARF type.

    We intentionally keep only scalar direct fields of an aggregate.  The
    runtime can still render raw aggregate bytes for everything else, which is
    safer than guessing nested layout or evaluating arbitrary DWARF locations.
    """
    if die is None or depth > 4:
        return TypeInfo(size=pointer_size if die is None else 0)
    while die.tag in {
        "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
        "DW_TAG_restrict_type", "DW_TAG_atomic_type",
    }:
        next_die = _type_die(die)
        if next_die is None:
            break
        die = next_die
    if die.tag == "DW_TAG_pointer_type":
        return TypeInfo(kind="pointer", size=_byte_size(die, pointer_size))
    if die.tag == "DW_TAG_base_type":
        encoding = int(die.attributes.get("DW_AT_encoding", {}).value) if "DW_AT_encoding" in die.attributes else 0
        if encoding == 4:
            kind = "float"
        elif encoding == 2:
            kind = "bool"
        elif encoding in {5, 6}:
            kind = "int"
        else:
            kind = "uint"
        return TypeInfo(kind=kind, size=_byte_size(die), signed=kind == "int")
    if die.tag in {"DW_TAG_structure_type", "DW_TAG_class_type", "DW_TAG_union_type"}:
        fields: list[Field] = []
        for child in die.iter_children():
            if child.tag != "DW_TAG_member" or len(fields) >= MAX_FIELDS:
                continue
            offset = queries.constant_member_offset(child)
            field_name = _name(child)
            field_type = _type_info(_type_die(child), pointer_size, queries, depth + 1)
            if offset is None or not field_name or field_type.kind not in {"int", "uint", "bool", "float", "pointer"}:
                continue
            fields.append(Field(field_name, offset, field_type.kind, field_type.size, field_type.signed))
        return TypeInfo(kind="struct", size=_byte_size(die), fields=tuple(fields))
    if die.tag == "DW_TAG_array_type":
        return TypeInfo(kind="array", size=_byte_size(die))
    return TypeInfo(size=_byte_size(die))


def _dwarf_subprograms(elf: ELFFile, architecture: str,
                       queries: DwarfQueries | None = None) -> dict[int, list[tuple[str, tuple[TypeInfo, ...], tuple[str, ...]]]]:
    if not elf.has_dwarf_info():
        return {}
    result: dict[int, list[tuple[str, tuple[TypeInfo, ...], tuple[str, ...]]]] = {}
    pointer_size = 8 if elf.elfclass == 64 else 4
    queries = queries or DwarfQueries(elf)
    for cu in elf.get_dwarf_info().iter_CUs():
        for die in cu.iter_DIEs():
            if die.tag != "DW_TAG_subprogram":
                continue
            address = _address(die)
            function_name = _name(die)
            if address is None or not function_name:
                continue
            if architecture.lower().startswith("arm"):
                address &= ~1
            types: list[TypeInfo] = []
            names: list[str] = []
            for child in die.iter_children():
                if child.tag != "DW_TAG_formal_parameter" or len(types) >= MAX_ARGUMENTS:
                    continue
                types.append(_type_info(_type_die(child), pointer_size, queries))
                names.append(_name(child) or f"arg{len(names)}")
            result.setdefault(address, []).append((function_name, tuple(types), tuple(names)))
    return result


def _symbol_entries(ctx: RunContext) -> tuple[list[tuple[int, str]], dict[int, list[tuple[str, tuple[TypeInfo, ...], tuple[str, ...]]]], int]:
    entries: dict[int, str] = {}
    try:
        elf = ctx.analysis.elf
        dwarf = _dwarf_subprograms(
            elf, ctx.architecture,
            ctx.analysis.dwarf if elf.has_dwarf_info() else None,
        )
        for symbol in ctx.analysis.symbols.all(kind="function"):
            entries.setdefault(symbol.code_address, symbol.name)
        return sorted(entries.items()), dwarf, elf.elfclass // 8
    except OSError as exc:
        raise VirtualPreparationError(
            f"function_tracer cannot read firmware ELF {ctx.binary}: {exc}"
        ) from exc


def _abi_registers(architecture: str) -> tuple[tuple[int, ...], int]:
    arch = architecture.lower()
    if arch.startswith("arm") and "64" not in arch:
        return (0, 1, 2, 3), 4
    if arch in {"aarch64", "arm64"}:
        return tuple(range(8)), 8
    if arch.startswith("riscv"):
        return tuple(range(10, 18)), 8
    if arch in {"x86_64", "amd64"}:
        return (5, 4, 3, 2, 8, 9), 8  # System V AMD64 ABI.
    raise VirtualPreparationError(
        f"function_tracer has no documented entry ABI for architecture {architecture!r}"
    )


def _arguments(types: tuple[TypeInfo, ...], names: tuple[str, ...], architecture: str,
               pointer_size: int) -> tuple[Argument, ...]:
    registers, word_size = _abi_registers(architecture)
    register_cursor = 0
    stack_offset = 0
    arguments: list[Argument] = []
    for index, type_info in enumerate(types[:MAX_ARGUMENTS]):
        size = type_info.size or pointer_size
        if type_info.kind == "pointer":
            size = pointer_size
        words = max(1, ceil(size / word_size))
        # AAPCS aligns 64-bit values to an even core-register number.
        if architecture.lower().startswith("arm") and word_size == 4 and size >= 8:
            register_cursor = (register_cursor + 1) & ~1
        if register_cursor + words <= len(registers):
            location = f"r{registers[register_cursor]}"
            register_cursor += words
        else:
            stack_offset = (stack_offset + word_size - 1) & ~(word_size - 1)
            location = f"s{stack_offset}"
            stack_offset += words * word_size
        arguments.append(Argument(
            index=index,
            name=names[index] if index < len(names) else f"arg{index}",
            location=location,
            size=min(size, 64),
            kind=type_info.kind,
            signed=type_info.signed,
            fields=type_info.fields[:MAX_FIELDS],
        ))
    return tuple(arguments)


def _safe(value: str, label: str) -> str:
    if not value or any(character in value for character in "\t\r\n"):
        raise VirtualPreparationError(f"function_tracer cannot serialize {label}: {value!r}")
    return value


def _field_text(fields: tuple[Field, ...]) -> str:
    return ";".join(
        f"{_safe(field.name, 'field name')}|{field.offset}|{field.kind}|{field.size}|{int(field.signed)}"
        for field in fields
    )


class FunctionTracerRunPreprocessor:
    """Emit entry hooks and DWARF schemas for a bounded call trace."""

    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        settings = dict(ctx.settings)
        include = _patterns(settings, "include")
        exclude = _patterns(settings, "exclude")
        limit = _setting_int(settings, "max_functions", MAX_FUNCTIONS, 1, MAX_FUNCTIONS)
        event_limit = _setting_int(settings, "max_events", 100_000, 0, MAX_EVENT_LIMIT)
        entries, dwarf, pointer_size = _symbol_entries(ctx)
        selected = [
            (address, name) for address, name in entries
            if (not include or any(fnmatchcase(name, pattern) for pattern in include))
            and not any(fnmatchcase(name, pattern) for pattern in exclude)
        ]
        if not selected:
            raise VirtualPreparationError("function_tracer selected no executable ELF functions")
        if len(selected) > limit:
            raise VirtualPreparationError(
                f"function_tracer selected {len(selected)} functions, exceeding max_functions={limit}"
            )
        functions = ctx.plugin_artifact_path("functions.tsv")
        arguments = ctx.plugin_artifact_path("arguments.tsv")
        trace = ctx.plugin_artifact_path("trace.tsv")
        settings_path = ctx.plugin_artifact_path("settings.tsv")
        function_rows = ["address\tfunction\n"]
        argument_rows = ["function\tindex\tname\tlocation\tsize\tkind\tsigned\tfields\n"]
        for address, name in selected:
            _safe(name, "function symbol")
            function_rows.append(f"0x{address:x}\t{name}\n")
            candidates = dwarf.get(address, ())
            candidate = next((item for item in candidates if item[0] == name), candidates[0] if candidates else None)
            if candidate is None:
                continue
            _dwarf_name, types, names = candidate
            for argument in _arguments(types, names, ctx.architecture, pointer_size):
                argument_rows.append(
                    f"{name}\t{argument.index}\t{_safe(argument.name, 'argument name')}\t"
                    f"{argument.location}\t{argument.size}\t{argument.kind}\t{int(argument.signed)}\t"
                    f"{_field_text(argument.fields)}\n"
                )
        functions.write_text("".join(function_rows), encoding="utf-8")
        arguments.write_text("".join(argument_rows), encoding="utf-8")
        trace.write_text("icount\tpc\tfunction\targuments\n", encoding="utf-8")
        settings_path.write_text(f"max_events\t{event_limit}\n", encoding="utf-8")
        ctx.logger.info(
            "function_tracer installed %d entry hooks with DWARF argument schemas", len(selected)
        )
        return RunPrepareResult(
            virtuals=[
                VirtualInstruction(at=address, instruction="function_tracer", args=[name])
                for address, name in selected
            ],
            artifacts=[functions, arguments, trace, settings_path],
        )


register_virtual(VirtualDefinition(name="function_tracer"))
register_run_preprocessor(
    RunDefinition(
        name="function_tracer",
        prepare=FunctionTracerRunPreprocessor(),
        enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        help=ConfigurationHelp("Trace selected function entries with DWARF-derived arguments.",
            '[CPU.cpu0.plugins.function_tracer]\nenabled = true\ninclude = ["main", "my_api_*"]\nmax_functions = 64\nmax_events = 10000',
            "docs/FunctionTracerPlugin.md"),
    )
)
