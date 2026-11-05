"""Shared, conservative DWARF layout and source queries."""
from __future__ import annotations

from dataclasses import dataclass

from elftools.dwarf.dwarf_expr import DWARFExprParser

from .errors import DwarfQueryError


_QUALIFIERS = {
    "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
    "DW_TAG_restrict_type", "DW_TAG_atomic_type",
}


@dataclass(frozen=True)
class TypeResult:
    name: str | None
    kind: str
    size: int | None
    supported: bool
    reason: str | None = None


@dataclass(frozen=True)
class MemberResult:
    name: str
    offset: int
    type: TypeResult


@dataclass(frozen=True)
class GlobalResult:
    name: str
    address: int
    type: TypeResult
    path: tuple[MemberResult, ...] = ()


@dataclass(frozen=True)
class SourceLocation:
    address: int
    file: str
    line: int | None


def _text(attribute) -> str | None:
    if attribute is None:
        return None
    value = attribute.value
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def constant_member_offset(dwarf, value) -> int | None:
    """Decode the constant member-location forms supported by FastDyn."""
    if isinstance(value, int):
        return int(value)
    try:
        operations = DWARFExprParser(dwarf.structs).parse_expr(value)
    except Exception:
        return None
    if len(operations) == 1 and operations[0].op_name in {
        "DW_OP_plus_uconst", "DW_OP_constu", "DW_OP_consts",
    }:
        return int(operations[0].args[0])
    return None


class DwarfQueries:
    def __init__(self, elf):
        if not elf.has_dwarf_info():
            raise DwarfQueryError("ELF has no DWARF information")
        self._elf = elf
        self._dwarf = elf.get_dwarf_info()
        self.address_size = elf.elfclass // 8
        self.byteorder = "little" if elf.little_endian else "big"
        self._dies = None
        self._globals = None
        self._sources = None

    def dies(self):
        if self._dies is None:
            self._dies = tuple(die for cu in self._dwarf.iter_CUs() for die in cu.iter_DIEs())
        return self._dies

    @staticmethod
    def type_die(die):
        return die.get_DIE_from_attribute("DW_AT_type") \
            if die is not None and "DW_AT_type" in die.attributes else None

    def unwrap(self, die):
        seen = set()
        while die is not None and die.tag in _QUALIFIERS and die.offset not in seen:
            seen.add(die.offset)
            die = self.type_die(die)
        return die

    def type_info(self, die) -> TypeResult:
        original = die
        die = self.unwrap(die)
        if die is None:
            return TypeResult(None, "unknown", None, False, "missing type reference")
        name = _text(die.attributes.get("DW_AT_name"))
        kind = {
            "DW_TAG_base_type": "base", "DW_TAG_pointer_type": "pointer",
            "DW_TAG_array_type": "array", "DW_TAG_structure_type": "struct",
            "DW_TAG_class_type": "class", "DW_TAG_union_type": "union",
            "DW_TAG_enumeration_type": "enum",
        }.get(die.tag, die.tag.removeprefix("DW_TAG_"))
        direct = die.attributes.get("DW_AT_byte_size")
        if direct is not None:
            return TypeResult(name, kind, int(direct.value), True)
        if die.tag == "DW_TAG_pointer_type":
            return TypeResult(name, kind, self.address_size, True)
        if die.tag == "DW_TAG_array_type":
            element = self.type_info(self.type_die(die))
            if element.size is None:
                return TypeResult(name, kind, None, False, "array element size is unknown")
            count = 1
            saw_bound = False
            for child in die.iter_children():
                if child.tag != "DW_TAG_subrange_type":
                    continue
                bound = child.attributes.get("DW_AT_count")
                if bound is not None:
                    count *= int(bound.value)
                    saw_bound = True
                    continue
                bound = child.attributes.get("DW_AT_upper_bound")
                if bound is None:
                    return TypeResult(name, kind, None, False, "array bound is unknown")
                count *= int(bound.value) + 1
                saw_bound = True
            return (TypeResult(name, kind, element.size * count, True) if saw_bound
                    else TypeResult(name, kind, None, False, "array bound is missing"))
        target = self.type_die(die)
        if target is not None and target is not original:
            nested = self.type_info(target)
            return TypeResult(name or nested.name, kind, nested.size,
                              nested.supported, nested.reason)
        return TypeResult(name, kind, None, False, "DW_AT_byte_size is missing")

    def constant_member_offset(self, member) -> int | None:
        attribute = member.attributes.get("DW_AT_data_member_location")
        if attribute is None:
            return 0
        return constant_member_offset(self._dwarf, attribute.value)

    def members(self, die) -> tuple[MemberResult, ...]:
        die = self.unwrap(die)
        if die is None or die.tag not in {
            "DW_TAG_structure_type", "DW_TAG_class_type", "DW_TAG_union_type"
        }:
            return ()
        result = []
        for child in die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            name = _text(child.attributes.get("DW_AT_name"))
            offset = self.constant_member_offset(child)
            if name is not None and offset is not None:
                result.append(MemberResult(name, offset, self.type_info(self.type_die(child))))
        return tuple(result)

    def fixed_address(self, attribute) -> int | None:
        if attribute is None or not isinstance(attribute.value, (bytes, bytearray, list)):
            return None
        expression = bytes(attribute.value)
        if not expression or expression[0] != 0x03 or len(expression) != 1 + self.address_size:
            return None
        return int.from_bytes(expression[1:], self.byteorder)

    def _global_dies(self):
        if self._globals is None:
            result = {}
            for die in self.dies():
                if die.tag != "DW_TAG_variable":
                    continue
                name = _text(die.attributes.get("DW_AT_name"))
                if name:
                    result.setdefault(name, []).append(die)
            self._globals = result
        return self._globals

    def global_variable(self, expression: str) -> GlobalResult | None:
        parent, *fields = expression.split(".")
        if not parent or any(not field for field in fields):
            raise DwarfQueryError("global expression must be NAME or NAME.field")
        for die in self._global_dies().get(parent, ()):
            address = self.fixed_address(die.attributes.get("DW_AT_location"))
            typed = self.type_die(die)
            if address is None:
                continue
            path: list[MemberResult] = []
            for field in fields:
                aggregate = self.unwrap(typed)
                member = next((child for child in aggregate.iter_children()
                               if child.tag == "DW_TAG_member"
                               and _text(child.attributes.get("DW_AT_name")) == field), None) \
                    if aggregate is not None and aggregate.tag in {
                        "DW_TAG_structure_type", "DW_TAG_class_type", "DW_TAG_union_type"
                    } else None
                offset = self.constant_member_offset(member) if member is not None else None
                if member is None or offset is None:
                    return None
                typed = self.type_die(member)
                info = self.type_info(typed)
                path.append(MemberResult(field, offset, info))
                address += offset
            return GlobalResult(parent, address, self.type_info(typed), tuple(path))
        return None

    def globals_of_type(self, type_name: str) -> tuple[GlobalResult, ...]:
        result = []
        for name in self._global_dies():
            item = self.global_variable(name)
            if item is None:
                continue
            die = next(iter(self._global_dies()[name]))
            typed = self.unwrap(self.type_die(die))
            raw_name = _text(typed.attributes.get("DW_AT_name")) if typed else None
            prefix = {"DW_TAG_structure_type": "struct ", "DW_TAG_union_type": "union ",
                      "DW_TAG_enumeration_type": "enum "}.get(typed.tag, "") if typed else ""
            if type_name in {raw_name, prefix + raw_name if raw_name else None}:
                result.append(item)
        return tuple(sorted(result, key=lambda item: item.address))

    def source_location(self, address: int) -> SourceLocation | None:
        if self._sources is None:
            sources = []
            for cu in self._dwarf.iter_CUs():
                program = self._dwarf.line_program_for_CU(cu)
                if not program:
                    continue
                for entry in program.get_entries():
                    state = entry.state
                    if state is None or state.end_sequence or state.file == 0:
                        continue
                    try:
                        file_entry = program["file_entry"][state.file - 1]
                        directory = (program["include_directory"][file_entry.dir_index - 1].decode()
                                     if file_entry.dir_index else "")
                        filename = file_entry.name.decode("utf-8", "replace")
                    except (IndexError, AttributeError):
                        continue
                    sources.append(SourceLocation(int(state.address),
                                                  f"{directory + '/' if directory else ''}{filename}",
                                                  int(state.line) if state.line is not None else None))
            self._sources = tuple(sorted(sources, key=lambda item: item.address))
        previous = None
        for item in self._sources:
            if item.address > address:
                break
            previous = item
        return previous
