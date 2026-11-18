"""Canonical JSON, sha256 ids and the FNV-1a-64 checksums used by QEMU.

Frozen by WP0 (design §4.1). Every JSON artifact scopt writes and every
content-derived id goes through this module, so the rules live in one place:

* canonical JSON: UTF-8, ``sort_keys``, separators ``(",", ":")``, no NaN or
  infinity, string keys only, ``ensure_ascii=False``. Integers are JSON
  numbers. Addresses are *not* converted here: callers put them in as
  ``"0x…"`` strings (use :func:`hexaddr`).
* ids: ``"sha256:" + lowercase hex`` of the bytes they cover.
* FNV-1a-64 exactly as ``hash_bytes`` in qemu tcg/supercache.c -- including
  QEMU's non-standard offset basis (see ``FNV64_OFFSET``) -- and the 32-bit
  record checksum fold ``h ^ (h >> 32)`` (truncated to u32).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

# NOTE: QEMU's SUPERCACHE_FNV_OFFSET is 1469598103934665603 (0x14650fb0739d0383),
# which is NOT the standard FNV-1a-64 offset basis 14695981039346656037
# (0xcbf29ce484222325) -- one decimal digit is missing. Every "FNV-1a-64" in the
# .sc, .scm and .qprof formats means QEMU's variant, so this is the frozen value.
FNV64_OFFSET = 1469598103934665603  # supercache.c SUPERCACHE_FNV_OFFSET (0x14650fb0739d0383)
FNV64_OFFSET_STANDARD = 0xCBF29CE484222325  # reference only; never used by the formats
FNV64_PRIME = 0x100000001B3  # 1099511628211 (SUPERCACHE_FNV_PRIME, standard prime)
U64_MASK = 0xFFFF_FFFF_FFFF_FFFF
U32_MASK = 0xFFFF_FFFF

SHA256_PREFIX = "sha256:"

# .sc v5: the header checksum covers bytes [0, 72), the record checksum [0, 92).
SC_HEADER_CHECKSUM_LEN = 72
SC_RECORD_CHECKSUM_LEN = 92


# --------------------------------------------------------------------------
# FNV-1a-64
# --------------------------------------------------------------------------

def fnv1a64(data: bytes | bytearray | memoryview, h: int = FNV64_OFFSET) -> int:
    """FNV-1a-64 of ``data``, continuing from ``h`` (supercache.c ``hash_bytes``)."""
    prime = FNV64_PRIME
    for b in bytes(data):
        h = ((h ^ b) * prime) & U64_MASK
    return h


def record_checksum_fold(h: int) -> int:
    """Fold a 64-bit FNV value to the u32 record checksum: ``(u32)(h ^ (h >> 32))``."""
    h &= U64_MASK
    return (h ^ (h >> 32)) & U32_MASK


def sc_header_checksum(header: bytes) -> int:
    """Checksum of a v5 ``.sc`` header: FNV-1a-64 over bytes [0, 72)."""
    if len(header) < SC_HEADER_CHECKSUM_LEN:
        raise ValueError("header shorter than 72 bytes")
    return fnv1a64(header[:SC_HEADER_CHECKSUM_LEN])


def sc_record_checksum(record: bytes) -> int:
    """Checksum of a v5 ``.sc`` record: fold(FNV-1a-64 over bytes [0, 92))."""
    if len(record) < SC_RECORD_CHECKSUM_LEN:
        raise ValueError("record shorter than 92 bytes")
    return record_checksum_fold(fnv1a64(record[:SC_RECORD_CHECKSUM_LEN]))


# --------------------------------------------------------------------------
# sha256
# --------------------------------------------------------------------------

def sha256_hex(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def sha256_id(data: bytes | bytearray | memoryview) -> str:
    """``"sha256:" + hex`` of ``data``."""
    return SHA256_PREFIX + sha256_hex(data)


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Plain hex sha256 of a file's full byte stream (holes read as zeros)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def is_sha256_id(s: Any) -> bool:
    if not isinstance(s, str) or not s.startswith(SHA256_PREFIX):
        return False
    hx = s[len(SHA256_PREFIX):]
    return len(hx) == 64 and all(c in "0123456789abcdef" for c in hx)


# --------------------------------------------------------------------------
# Canonical JSON
# --------------------------------------------------------------------------

class CanonicalJSONError(TypeError):
    pass


def _check(obj: Any, path: str = "$") -> None:
    if obj is None or isinstance(obj, (bool, int, str)):
        return
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise CanonicalJSONError(f"{path}: NaN/inf not allowed")
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalJSONError(f"{path}: non-string key {k!r}")
            _check(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalJSONError(f"{path}: type {type(obj).__name__} is not JSON")


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON bytes (sorted keys, no whitespace, UTF-8)."""
    _check(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_jsonl(rows: Iterable[Any]) -> bytes:
    """One canonical JSON document per line, each terminated by ``\\n``."""
    return b"".join(canonical_json(r) + b"\n" for r in rows)


def content_id(obj: Mapping[str, Any], id_field: str, drop: Iterable[str] = ()) -> str:
    """``sha256:`` of the canonical JSON of ``obj`` without ``id_field`` and ``drop``."""
    removed = {id_field, *drop}
    body = {k: v for k, v in obj.items() if k not in removed}
    return sha256_id(canonical_json(body))


# --------------------------------------------------------------------------
# Address strings
# --------------------------------------------------------------------------

def hexaddr(v: int) -> str:
    """Address/flag value as a lowercase ``"0x…"`` string (no padding)."""
    if v < 0:
        raise ValueError("negative address")
    return f"0x{v:x}"


def parse_hex(s: str | int) -> int:
    if isinstance(s, int):
        return s
    if not isinstance(s, str) or not s.startswith("0x"):
        raise ValueError(f"not a 0x string: {s!r}")
    return int(s, 16)
