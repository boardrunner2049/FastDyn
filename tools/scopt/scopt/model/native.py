"""Immutable preview of an offline native call transplant."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..canon import canonical_json, sha256_id


UDIV_KIND = "guarded-udiv"
UDIV_29_KIND = "guarded-udiv-29"
UDIV_29_INLINE_KIND = "guarded-udiv-29-inline"
UDIV_CLEANUP_KIND = "guarded-udiv-cleanup"
FULL_UDIV_KIND = "full-udiv"
FULL_UDIV_CLEANUP_KIND = "full-udiv-cleanup"
FULL_UDIV_KINDS = frozenset((FULL_UDIV_KIND, FULL_UDIV_CLEANUP_KIND))
GUARDED_UDIV_KINDS = frozenset((UDIV_KIND, UDIV_CLEANUP_KIND, UDIV_29_KIND, UDIV_29_INLINE_KIND))
UDIV_KINDS = GUARDED_UDIV_KINDS | FULL_UDIV_KINDS
# QEMU TCG u32 (env, u32, u32) on the admitted x86-64 SysV host.
UDIV_TYPEMASK = 2 | (6 << 3) | (2 << 6) | (2 << 9)


def supports_tcg_integer_abi(typemask: int) -> bool:
    """An integer return and at most six integer SysV GPR arguments."""
    integer_codes = (2, 3, 4, 5)
    if type(typemask) is not int or typemask < 0 or typemask & 7 not in integer_codes:
        return False
    args = typemask >> 3
    for _ in range(6):
        if args == 0:
            return True
        if args & 7 not in integer_codes:
            return False
        args >>= 3
    return args == 0


@dataclass(frozen=True, slots=True)
class NativeOrigin:
    source_off: int
    source_size: int
    output_off: int
    output_size: int

    def to_json(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in
                ("source_off", "source_size", "output_off", "output_size")}


@dataclass(frozen=True, slots=True)
class NativeFragment:
    """Island bytes with continuation relocations expressed as field offsets."""

    code: bytes
    origins: tuple[NativeOrigin, ...]
    continuation_fields: tuple[int, ...]

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code.hex(), "origins": [origin.to_json() for origin in self.origins],
                "continuation_fields": list(self.continuation_fields)}


@dataclass(frozen=True, slots=True)
class NativeRelocation:
    """A signed rel32 to an external target, based at the instruction end."""

    field_off: int
    next_off: int
    target_abs: int

    def to_json(self) -> dict[str, int]:
        return {"field_off": self.field_off, "next_off": self.next_off,
                "target_abs": self.target_abs}


@dataclass(frozen=True, slots=True)
class NativeBranchRelaxation:
    """An ordinary two-byte TCG branch widened by eight aligned bytes."""

    source_off: int
    source_bytes: bytes

    @property
    def target_off(self) -> int:
        return self.source_off + 2 + int.from_bytes(self.source_bytes[1:], "little", signed=True)

    @property
    def near_opcode(self) -> bytes:
        opcode = self.source_bytes[0]
        return b"\xe9" if opcode == 0xeb else bytes((0x0f, 0x80 | (opcode & 15)))

    def to_json(self) -> dict[str, Any]:
        return {"source_off": self.source_off, "source_bytes": self.source_bytes.hex()}


BRANCH_RELAXATION_GROWTH = 8
MAX_BRANCH_RELAXATIONS = 8
MAX_INTEGER_SPLICES = 2


@dataclass(frozen=True, slots=True)
class NativeFrameBody:
    """One complete x86-64 function at its ELF virtual address."""

    address: int
    code: bytes

    def to_json(self) -> dict[str, Any]:
        return {"address": self.address, "code": self.code.hex()}


@dataclass(frozen=True, slots=True)
class NativeSplice:
    """Fall-through replacement of a native call and any rewritten argument setup."""

    start_off: int
    prefix: bytes
    code: bytes
    kind: str = "integer"
    relocations: tuple[NativeRelocation, ...] = ()
    depth: int = 1
    compact_frame: bool = False
    branches: tuple[NativeBranchRelaxation, ...] = ()
    frame_chain: tuple[NativeFrameBody, ...] = ()

    @property
    def physical_start(self) -> int:
        return self.start_off + BRANCH_RELAXATION_GROWTH * sum(
            row.source_off < self.start_off for row in self.branches)

    def valid_branch_spans(self, continuation: int, original_code_size: int) -> bool:
        if not self.branches:
            return True
        if self.kind != "integer" or len(self.branches) > MAX_BRANCH_RELAXATIONS:
            return False
        previous_end = 0
        for row in self.branches:
            off = row.source_off
            raw = row.source_bytes
            if (type(off) is not int or not previous_end <= off <= original_code_size - 2
                    or len(raw) != 2 or not (raw[0] == 0xeb or 0x70 <= raw[0] <= 0x7f)
                    or off < continuation and self.start_off < off + 2
                    or not 0 <= row.target_off < original_code_size
                    or self.start_off < row.target_off < continuation
                    or any(row.target_off == other.source_off + 1 for other in self.branches)):
                return False
            previous_end = off + 2
        return True

    def to_json(self) -> dict[str, Any]:
        doc = {"start_off": self.start_off, "prefix": self.prefix.hex(), "code": self.code.hex()}
        if self.kind != "integer" or self.relocations:
            doc.update(kind=self.kind, relocations=[row.to_json() for row in self.relocations])
        if self.compact_frame:
            doc["compact_frame"] = True
        if self.depth != 1:
            doc["depth"] = self.depth
        if self.branches:
            doc["branches"] = [row.to_json() for row in self.branches]
        if self.frame_chain:
            doc["frame_chain"] = [body.to_json() for body in self.frame_chain]
        return doc


@dataclass(frozen=True, slots=True)
class InlineCall:
    target: str
    unit_hash: str
    call_off: int
    call_bytes: bytes
    continuation_off: int
    helper_id: int
    target_abs: int
    pool_slot_off: int | None
    guest_pcs: tuple[int, ...]
    image_id: str
    elf_address: int
    image_offset: int
    symbol: str
    callee_code: bytes
    native_input_id: str
    fragment: NativeFragment
    bridge_sha256: str | None = None
    splice: NativeSplice | None = None
    tcg_typemask: int | None = None

    @property
    def callee_hash(self) -> str:
        return sha256_id(self.callee_code)

    @property
    def certificate_id(self) -> str:
        return sha256_id(canonical_json(self.to_json()))

    @property
    def growth(self) -> int:
        if self.splice is not None:
            return (len(self.splice.code) - (self.continuation_off - self.splice.start_off)
                    + BRANCH_RELAXATION_GROWTH * len(self.splice.branches))
        return len(self.fragment.code)

    def to_json(self) -> dict[str, Any]:
        doc = {"version": 1, "target": self.target, "unit_hash": self.unit_hash,
                "call_off": self.call_off, "call_bytes": self.call_bytes.hex(),
                "continuation_off": self.continuation_off, "helper_id": self.helper_id,
                "target_abs": self.target_abs, "pool_slot_off": self.pool_slot_off,
                "guest_pcs": list(self.guest_pcs), "image_id": self.image_id,
                "elf_address": self.elf_address, "image_offset": self.image_offset,
                "symbol": self.symbol, "callee_code": self.callee_code.hex(),
                "callee_hash": self.callee_hash, "native_input_id": self.native_input_id,
                "fragment": self.fragment.to_json(), "bridge_sha256": self.bridge_sha256}
        if self.splice is not None:
            doc.update(splice=self.splice.to_json(), tcg_typemask=self.tcg_typemask)
        return doc

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> InlineCall:
        if data["version"] != 1:
            raise ValueError("unsupported native fragment version")
        body = data["fragment"]
        fragment = NativeFragment(bytes.fromhex(body["code"]),
                                  tuple(NativeOrigin(**row) for row in body["origins"]),
                                  tuple(body["continuation_fields"]))
        splice_doc = data.get("splice")
        splice = None if splice_doc is None else NativeSplice(
            splice_doc["start_off"], bytes.fromhex(splice_doc["prefix"]),
            bytes.fromhex(splice_doc["code"]), splice_doc.get("kind", "integer"),
            tuple(NativeRelocation(**row) for row in splice_doc.get("relocations", ())),
            splice_doc.get("depth", 1), splice_doc.get("compact_frame", False),
            tuple(NativeBranchRelaxation(row["source_off"], bytes.fromhex(row["source_bytes"]))
                  for row in splice_doc.get("branches", ())),
            tuple(NativeFrameBody(row["address"], bytes.fromhex(row["code"]))
                  for row in splice_doc.get("frame_chain", ())))
        result = cls(data["target"], data["unit_hash"], data["call_off"],
                     bytes.fromhex(data["call_bytes"]), data["continuation_off"],
                     data["helper_id"], data["target_abs"], data["pool_slot_off"],
                     tuple(data["guest_pcs"]), data["image_id"], data["elf_address"],
                     data["image_offset"], data["symbol"], bytes.fromhex(data["callee_code"]),
                     data["native_input_id"], fragment,
                     data["bridge_sha256"], splice, data.get("tcg_typemask"))
        if result.to_json() != data:
            raise ValueError("non-canonical inline fragment")
        return result
