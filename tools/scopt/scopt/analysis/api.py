"""AnalysisStore types (design §5.3). Interfaces only — frozen by WP0; WP9 implements
``scopt.analysis.store.AnalysisStore`` against these types.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = ["ScopeRef", "AnalysisSpec", "AnalysisResult", "AnalysisStoreAPI", "B1_ANALYSES"]

Scope = Literal["unit", "snapshot", "region"]


@dataclass(frozen=True)
class ScopeRef:
    kind: Scope
    id: str  # UnitId, SnapshotId or RegionId


@dataclass(frozen=True)
class AnalysisSpec:
    name: str
    version: int
    scope: Scope
    requires: tuple[tuple[str, int], ...]  # (analysis, min version)
    inputs: frozenset[str]  # "code", "meta", "layout", "profile:<phase>", "bundle"
    compute: Callable[[Any, ScopeRef], Any]  # (AnalysisCtx, ScopeRef) -> value
    result_schema: type


@dataclass(frozen=True)
class AnalysisResult:
    name: str
    version: int
    scope_id: str
    value: Any
    dep_units: frozenset[tuple[str, str]]  # (unit_id, content_hash or layout_hash)
    dep_results: frozenset[tuple[str, int, str]]  # (name, version, result_hash)
    dep_inputs: frozenset[str]  # snapshot_id / profile_id / bundle_id
    certified: bool
    result_hash: str


@runtime_checkable
class AnalysisStoreAPI(Protocol):
    def get(self, name: str, scope: ScopeRef, min_version: int = 1) -> AnalysisResult: ...
    def invalidate(self, changed: Mapping[str, frozenset[str]], preserved: frozenset[str]) -> int: ...
    def check_fact(self, r: AnalysisResult) -> None: ...  # raises passes.base.StaleFact


# name -> version of the B1 analyses (§5.3 table).
B1_ANALYSES: Mapping[str, int] = {
    "refs": 1,
    "layout": 1,
    "cfg.known_edges": 1,
    "recovery": 1,
    "profile.counts": 1,
    "hotness": 1,
    "locality": 1,
}
