"""Typed ELF reference linking and bounded machine-state construction."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from elftools.elf.elffile import ELFFile

from .model import BundleValidationError, hash_json
from .region import InstructionIdentity, ReferenceEdge
from .sdk import Decision


_RIP = re.compile(r"\[rip\s*([+-])\s*(0x[0-9a-fA-F]+|\d+)\]")


@dataclass(frozen=True)
class ELFObject:
    identity: str
    name: str
    address: int
    size: int
    section: str
    permissions: str
    mutability: str
    symbol_type: str


class ELFReferenceResolver:
    """Resolve x86 RIP references to an ELF object identity and permissions."""

    def __init__(self, image: Path) -> None:
        self.image = image.resolve()
        self._objects: list[ELFObject] = []
        self._sections: list[tuple[int, int, str, str, str]] = []
        with self.image.open("rb") as source:
            elf = ELFFile(source)
            for section in elf.iter_sections():
                start = int(section["sh_addr"])
                size = int(section["sh_size"])
                if not size:
                    continue
                flags = int(section["sh_flags"])
                permissions = "r"
                if flags & 0x1:
                    permissions += "w"
                if flags & 0x4:
                    permissions += "x"
                mutability = "mutable" if "w" in permissions else "immutable"
                self._sections.append((start, start + size, section.name, permissions, mutability))
            for section in elf.iter_sections():
                if section["sh_type"] not in {"SHT_SYMTAB", "SHT_DYNSYM"}:
                    continue
                for symbol in section.iter_symbols():
                    address = int(symbol["st_value"])
                    size = int(symbol["st_size"])
                    if not address:
                        continue
                    section_info = self._section(address)
                    section_name, permissions, mutability = (
                        section_info[0], section_info[1], section_info[2]
                    ) if section_info else ("unknown", "", "unknown")
                    name = symbol.name or f"anonymous@{address:#x}"
                    self._objects.append(
                        ELFObject(
                            identity=f"elf:{name}:{address:#x}:{size}",
                            name=name,
                            address=address,
                            size=size,
                            section=section_name,
                            permissions=permissions,
                            mutability=mutability,
                            symbol_type=str(symbol["st_info"]["type"]),
                        )
                    )

    def _section(self, address: int) -> tuple[str, str, str] | None:
        for start, end, name, permissions, mutability in self._sections:
            if start <= address < end:
                return name, permissions, mutability
        return None

    def object_at(self, address: int) -> ELFObject | None:
        matches = [
            item for item in self._objects
            if item.address <= address < item.address + max(item.size, 1)
        ]
        if not matches:
            return None
        return min(matches, key=lambda item: max(item.size, 1))

    def resolve_instruction(self, instruction: InstructionIdentity) -> ReferenceEdge | None:
        match = _RIP.search(instruction.operands)
        if match is None:
            return None
        displacement = int(match.group(2), 0) * (1 if match.group(1) == "+" else -1)
        target = instruction.address + instruction.size + displacement
        obj = self.object_at(target)
        if obj is None:
            return ReferenceEdge(
                reference_id=f"ref:{instruction.identity}:0",
                source_identity=instruction.identity,
                source_offset=0,
                target_kind="unknown",
                target_identity=None,
                target_address=target,
                addend=0,
                encoding="x86-rip-disp32",
                range_min=-(2**31),
                range_max=2**31 - 1,
                binding="pc_relative",
                mutability="unknown",
                permissions="",
                status="unknown",
                invalidated_by=("instruction_layout", "object_layout"),
            )
        addend = target - obj.address
        target_kind = "code" if "x" in obj.permissions else "data"
        return ReferenceEdge(
            reference_id=f"ref:{instruction.identity}:0",
            source_identity=instruction.identity,
            source_offset=0,
            target_kind=target_kind,
            target_identity=obj.identity,
            target_address=obj.address,
            addend=addend,
            encoding="x86-rip-disp32",
            range_min=-(2**31),
            range_max=2**31 - 1,
            binding="pc_relative",
            mutability=obj.mutability,
            permissions=obj.permissions,
            status="resolved",
            invalidated_by=("instruction_layout", "object_layout"),
        )

    def resolve_all(self, instructions: Iterable[InstructionIdentity]) -> tuple[ReferenceEdge, ...]:
        return tuple(
            edge for edge in (self.resolve_instruction(item) for item in instructions)
            if edge is not None
        )


@dataclass(frozen=True)
class MemorySegmentState:
    segment_id: str
    address: int
    size: int
    permissions: str
    space: str
    contents_hex: str
    observable: bool
    alias_class: str

    def __post_init__(self) -> None:
        if self.address < 0 or self.size <= 0:
            raise BundleValidationError("state memory segment is invalid")
        if len(bytes.fromhex(self.contents_hex)) != self.size:
            raise BundleValidationError("state memory contents do not match segment size")


@dataclass(frozen=True)
class MemoryRequest:
    operation: str
    instruction: str
    address: int
    size: int
    permissions: str
    segment_id: str
    outcome: str


@dataclass(frozen=True)
class AdmittedMachineState:
    state_id: str
    registers: tuple[tuple[str, int], ...]
    flags: tuple[tuple[str, int], ...]
    memory: tuple[MemorySegmentState, ...]
    requests: tuple[MemoryRequest, ...]
    alias_facts: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()

    @property
    def state_hash(self) -> str:
        return hash_json(asdict(self))


@dataclass(frozen=True)
class StateConstructionResult:
    status: str
    reason_code: str
    states: tuple[AdmittedMachineState, ...]
    diagnostics: Mapping[str, Any]

    @property
    def state_set_hash(self) -> str:
        return hash_json([asdict(item) for item in self.states])

    def decision(self) -> Decision:
        return Decision(self.status, self.reason_code, dict(self.diagnostics))


class BoundedStateConstructor:
    """Construct exact bounded states without inventing private/no-alias facts."""

    STACK_TOP = 0x700000000
    HEAP_BASE = 0x100000000

    def linked_scalar(self, edge: ReferenceEdge, *, value: int = 0) -> StateConstructionResult:
        if edge.status != "resolved" or edge.target_address is None or edge.target_identity is None:
            return StateConstructionResult(
                "unsupported",
                "reference_target_unresolved",
                (),
                {"reference": asdict(edge)},
            )
        if edge.target_kind != "data" or edge.mutability != "mutable":
            return StateConstructionResult(
                "unsupported",
                "only_mutable_scalar_data_supported",
                (),
                {"reference": asdict(edge)},
            )
        size = 8
        segment = MemorySegmentState(
            edge.target_identity,
            edge.target_address,
            size,
            edge.permissions or "rw",
            "ram",
            int(value).to_bytes(size, "little", signed=False).hex(),
            True,
            "global-mutable",
        )
        stack = MemorySegmentState(
            "stack",
            self.STACK_TOP - 0x100,
            0x100,
            "rw",
            "stack",
            bytes(0x100).hex(),
            True,
            "stack-may-alias-input-unless-excluded",
        )
        request = MemoryRequest(
            "read",
            edge.source_identity,
            edge.target_address + edge.addend,
            size,
            "r",
            edge.target_identity,
            "mapped",
        )
        state = AdmittedMachineState(
            "linked-scalar-zero",
            (("rsp", self.STACK_TOP - 8),),
            (),
            (stack, segment),
            (request,),
            ("stack/global alias not assumed",),
        )
        return StateConstructionResult(
            "accepted",
            "linked_mutable_scalar_state_constructed",
            (state,),
            {"reference": asdict(edge), "requested_address": request.address, "size": size},
        )

    def list_fixture(self, name: str) -> StateConstructionResult:
        if name not in {"vListInsertEnd", "uxListRemove"}:
            return StateConstructionResult("unsupported", "list_fixture_not_supported", (), {})
        stack = MemorySegmentState(
            "stack", self.STACK_TOP - 0x200, 0x200, "rw", "stack",
            bytes(0x200).hex(), True, "stack-visible",
        )
        list_address = self.HEAP_BASE
        item_address = self.HEAP_BASE + 0x100
        list_bytes = bytearray(0x40)
        item_bytes = bytearray(0x40)
        # A concrete, internally linked list/item graph.  No no-alias fact is inferred.
        list_bytes[0:8] = (1).to_bytes(8, "little")
        list_bytes[8:16] = (list_address + 0x10).to_bytes(8, "little")
        list_bytes[16:24] = (list_address + 0x10).to_bytes(8, "little")
        item_bytes[8:16] = (list_address + 0x10).to_bytes(8, "little")
        item_bytes[16:24] = (list_address + 0x10).to_bytes(8, "little")
        item_bytes[32:40] = list_address.to_bytes(8, "little")
        segments = (
            stack,
            MemorySegmentState("list", list_address, len(list_bytes), "rw", "ram", list_bytes.hex(), True, "list-object"),
            MemorySegmentState("item", item_address, len(item_bytes), "rw", "ram", item_bytes.hex(), True, "list-item"),
        )
        requests = tuple(
            MemoryRequest(operation, instruction, address, size, permissions, segment, "mapped")
            for operation, instruction, address, size, permissions, segment in (
                ("read_write", "list-count", list_address, 8, "rw", "list"),
                ("read_write", "list-next", list_address + 8, 8, "rw", "list"),
                ("read_write", "item-next", item_address + 8, 8, "rw", "item"),
                ("read_write", "item-prev", item_address + 16, 8, "rw", "item"),
                ("read_write", "item-container", item_address + 32, 8, "rw", "item"),
            )
        )
        registers = (("rsp", self.STACK_TOP - 8), ("rdi", list_address))
        if name == "vListInsertEnd":
            registers += (("rsi", item_address),)
        state = AdmittedMachineState(
            f"{name}-bounded-linked-graph",
            tuple(sorted(registers)),
            (),
            segments,
            requests,
            (
                "list and item are distinct in this supported state",
                "no claim that all admitted caller pointers are non-aliasing",
            ),
        )
        return StateConstructionResult(
            "accepted",
            "bounded_linked_list_state_constructed",
            (state,),
            {
                "legacy_failure": "Could not resize memory",
                "legacy_exact_failing_state": "not emitted by selected STOKE StateGen",
                "repair": "explicit admitted object graph with exact ranges and permissions",
                "requests": [asdict(item) for item in requests],
            },
        )
