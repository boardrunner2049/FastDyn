"""Identity functions (design §4.1). Frozen by WP0.

Exact byte-level definitions (any change is a contract change):

``tb_uid``
    ``"tb:" + sha256(b"tb1" ‖ le64 page_addr0 ‖ le64 page_addr1 ‖ le32 flags ‖
    le64 cs_base ‖ le32 cflags ‖ le16 guest_size ‖ le16 icount ‖
    le64 guest_hash).digest()[:16].hex()`` — i.e. the first **16 bytes** of the
    digest, 32 lowercase hex characters. ``page_addr1`` is the raw record value;
    "no second page" is ``tb_page_addr_t`` -1 = ``0xffffffffffffffff``
    (``TBKey.page_addr1 is None`` encodes as that value).

``UnitId``
    ``tb_uid + "#" + k`` where ``k`` is the occurrence index (0, 1, …) among equal
    ``tb_uid`` values in slot order.

``SnapshotId``
    ``"sha256:" + sha256(header[0:72] ‖ for each valid slot in slot order:
    record[0:92] ‖ sha256(code[tc_offset:end_offset]).digest())`` — the per-slot
    code hash is the raw 32-byte digest.

``BundleId`` / ``ProfileId`` / ``RegionId`` / ``RuntimeIdentityId``
    ``"sha256:" + sha256(canonical_json(obj without its id field))``; a region
    also drops ``provenance``.

``ArtifactId``
    ``"sha256:" + sha256(MANIFEST.sha256 file bytes)``.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .canon import SHA256_PREFIX, U64_MASK, canonical_json, content_id, sha256_id

PAGE_ADDR_NONE = U64_MASK  # (tb_page_addr_t)-1
TB_UID_PREFIX = "tb:"
TB_UID_TAG = b"tb1"
TB_UID_HEX_LEN = 32  # 16 digest bytes
_TB_UID_FMT = struct.Struct("<QQIQIHHQ")

SC_HEADER_ID_LEN = 72
SC_RECORD_ID_LEN = 92


@dataclass(frozen=True, slots=True)
class TBKey:
    """Translation key ``(page_addr0, page_addr1, flags, cs_base, cflags)``.

    The pc is implicitly 0 (CF_PCREL); non-PCREL guests are unsupported.
    ``page_addr1`` is ``None`` when the TB does not span a second page.
    """

    page_addr0: int
    page_addr1: int | None
    flags: int
    cs_base: int
    cflags: int

    @classmethod
    def from_raw(cls, page_addr0: int, page_addr1: int, flags: int, cs_base: int,
                 cflags: int) -> "TBKey":
        return cls(page_addr0, None if page_addr1 == PAGE_ADDR_NONE else page_addr1,
                   flags, cs_base, cflags)

    @property
    def page_addr1_raw(self) -> int:
        return PAGE_ADDR_NONE if self.page_addr1 is None else self.page_addr1


def tb_uid_bytes(key: TBKey, guest_size: int, icount: int, guest_hash: int) -> bytes:
    """The exact message hashed by :func:`tb_uid`."""
    return TB_UID_TAG + _TB_UID_FMT.pack(
        key.page_addr0, key.page_addr1_raw, key.flags, key.cs_base, key.cflags,
        guest_size, icount, guest_hash)


def tb_uid(key: TBKey, guest_size: int, icount: int, guest_hash: int) -> str:
    digest = hashlib.sha256(tb_uid_bytes(key, guest_size, icount, guest_hash)).digest()
    return TB_UID_PREFIX + digest[:16].hex()


def unit_ids(tb_uids_in_slot_order: Iterable[str]) -> list[str]:
    """Assign ``UnitId``s (``tb_uid#k``) to tb_uids given in slot order."""
    seen: dict[str, int] = {}
    out = []
    for u in tb_uids_in_slot_order:
        k = seen.get(u, 0)
        seen[u] = k + 1
        out.append(f"{u}#{k}")
    return out


def parse_unit_id(uid: str) -> tuple[str, int]:
    """Split ``"tb:<hex>#k"`` into ``("tb:<hex>", k)``; raises ValueError."""
    base, sep, k = uid.rpartition("#")
    if (not sep or not base.startswith(TB_UID_PREFIX)
            or len(base) != len(TB_UID_PREFIX) + TB_UID_HEX_LEN
            or not k.isdigit() or (len(k) > 1 and k[0] == "0")):
        raise ValueError(f"malformed UnitId {uid!r}")
    int(base[len(TB_UID_PREFIX):], 16)
    return base, int(k)


def is_unit_id(uid: Any) -> bool:
    try:
        parse_unit_id(uid)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def snapshot_id(header: bytes, valid_slots: Iterable[tuple[bytes, bytes]]) -> str:
    """SnapshotId from the 80-byte ``.sc`` header and ``(record, code)`` pairs.

    ``valid_slots`` yields, in slot order, the 104 (or ≥92) record bytes and the
    code bytes ``[tc_offset, end_offset)`` of every *valid* record.
    """
    if len(header) < SC_HEADER_ID_LEN:
        raise ValueError("header shorter than 72 bytes")
    h = hashlib.sha256()
    h.update(header[:SC_HEADER_ID_LEN])
    for record, code in valid_slots:
        if len(record) < SC_RECORD_ID_LEN:
            raise ValueError("record shorter than 92 bytes")
        h.update(record[:SC_RECORD_ID_LEN])
        h.update(hashlib.sha256(code).digest())
    return SHA256_PREFIX + h.hexdigest()


def bundle_id(bundle: Mapping[str, Any]) -> str:
    return content_id(bundle, "bundle_id")


def profile_id(profile: Mapping[str, Any]) -> str:
    return content_id(profile, "profile_id")


def region_id(region: Mapping[str, Any]) -> str:
    return content_id(region, "region_id", drop=("provenance",))


def runtime_identity_id(runtime: Mapping[str, Any]) -> str:
    return content_id(runtime, "runtime_identity_id")


def artifact_id(manifest_bytes: bytes) -> str:
    return sha256_id(manifest_bytes)


def candidate_id(obj: Mapping[str, Any]) -> str:
    """Id for a rejected candidate (``store/rejections/<id>.json``)."""
    return sha256_id(canonical_json(obj))


__all__ = [
    "PAGE_ADDR_NONE", "TBKey", "tb_uid", "tb_uid_bytes", "unit_ids", "parse_unit_id",
    "is_unit_id", "snapshot_id", "bundle_id", "profile_id", "region_id",
    "runtime_identity_id", "artifact_id", "candidate_id",
]
