"""``CacheSnapshot``: the immutable imported snapshot (design §4.3, §5.2). WP7.

A :class:`CacheSnapshot` is what ``scopt import`` builds (and what
:func:`scopt.importer.import_cache.open_snapshot` rebuilds from a store
directory). It implements the ``SnapshotView`` protocol of
:mod:`scopt.model.unit` (``snapshot_id``, ``header``, ``runtime``, ``bundle``,
``units()``, ``unit()``, ``by_key()``, ``layout()``) plus ``capture_level``,
which WP9's PassManager admission reads.

It lives in its own module (not ``scopt/model/snapshot.py``) because WP9 adds
``snapshot.py`` (``WorkingSet``) on a parallel branch; WP9's ``WorkingSet``
accepts any ``SnapshotView``, so ``WorkingSet.initial(cache_snapshot)`` works.

Store layout (``store/snapshots/<hex>/`` where ``<hex>`` is the SnapshotId
without its ``sha256:`` prefix)::

    cache.sc                  sparse copy of the .sc (read-only)
    capture-<sha16>.scm       each .scm, named by its content sha256 (read-only)
    manifest.json             FastDyn manifest (<cache>.sc.fastdyn.json), if any
    bundle.json               the ExecutionBundle the import was bound to, if any
    runtime_identity.json     the RuntimeIdentity, if any
    snapshot.json             ids, header, status histogram, capture completeness
    units.jsonl               one CacheRegion v1 per valid slot, slot order
    code/<sha256>.bin         [tc_offset, end_offset) of each unit
    evidence.jsonl            one row per check per unit (plus snapshot rows)

Everything in it is canonical and contains no timestamps, so re-importing the
same inputs reproduces every file byte for byte.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..formats.sc_v5 import ScHeader
from ..ids import TBKey
from .unit import TBUnit

__all__ = ["CacheSnapshot", "snapshot_dir_name"]


def snapshot_dir_name(snapshot_id: str) -> str:
    """Directory name of a snapshot in the store: the hex part of its id."""
    return snapshot_id.split(":", 1)[1] if ":" in snapshot_id else snapshot_id


@dataclass(frozen=True)
class CacheSnapshot:
    snapshot_id: str
    header: ScHeader
    header_bytes: bytes
    _units: tuple[TBUnit, ...]
    runtime: Mapping[str, Any] | None = None
    bundle: Mapping[str, Any] | None = None
    capture_level: int | None = None
    run_info: Any = None  # scopt.formats.scm_v1.RunInfo of the first capture
    regions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)  # uid -> UnitCheck (importer)
    path: str | None = None  # store directory, when loaded from / written to a store
    _by_uid: Mapping[str, TBUnit] = field(default_factory=dict, repr=False)
    _by_key: Mapping[TBKey, tuple[TBUnit, ...]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        by_uid = {u.uid: u for u in self._units}
        if len(by_uid) != len(self._units):
            raise ValueError("duplicate UnitId in snapshot")
        by_key: dict[TBKey, list[TBUnit]] = {}
        for u in self._units:
            by_key.setdefault(u.key, []).append(u)
        object.__setattr__(self, "_by_uid", MappingProxyType(by_uid))
        object.__setattr__(self, "_by_key",
                           MappingProxyType({k: tuple(v) for k, v in by_key.items()}))
        object.__setattr__(self, "regions", MappingProxyType(dict(self.regions)))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    # -- SnapshotView ------------------------------------------------------------
    def units(self) -> tuple[TBUnit, ...]:
        """Units in slot order."""
        return self._units

    def unit(self, uid: str) -> TBUnit:
        try:
            return self._by_uid[uid]
        except KeyError:
            raise KeyError(f"unknown unit {uid}") from None

    def by_key(self, key: TBKey) -> tuple[TBUnit, ...]:
        return self._by_key.get(key, ())

    def layout(self) -> Mapping[str, int]:
        return MappingProxyType({u.uid: u.tb_offset for u in self._units})

    # -- extras --------------------------------------------------------------------
    def unit_ids(self) -> tuple[str, ...]:
        return tuple(u.uid for u in self._units)

    def __contains__(self, uid: object) -> bool:
        return uid in self._by_uid

    def __len__(self) -> int:
        return len(self._units)

    def by_slot(self, slot: int) -> TBUnit:
        for u in self._units:
            if u.slot == slot:
                return u
        raise KeyError(f"no valid unit in slot {slot}")

    def region(self, uid: str) -> Mapping[str, Any]:
        return self.regions[uid]

    def status(self, uid: str) -> str:
        """``ok_relocatable`` | ``pinned`` | ``no_meta`` (§2.7 result per TB)."""
        u = self.unit(uid)
        if u.meta_status == "no_meta":
            return "no_meta"
        return "ok_relocatable" if u.reloc.relocatable else "pinned"

    def status_histogram(self) -> dict[str, int]:
        out: dict[str, int] = {"no_meta": 0, "ok_relocatable": 0, "pinned": 0}
        for u in self._units:
            out[self.status(u.uid)] += 1
        return out

    @property
    def max_end(self) -> int:
        return max((u.end_offset for u in self._units), default=0)
