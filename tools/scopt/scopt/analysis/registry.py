"""Analysis registry (design §5.3).

An :class:`AnalysisRegistry` maps ``name -> {version: AnalysisSpec}``. Lookups
return the highest registered version that is at least the requested minimum.

Discovery (conflict-free across WPs): every module of the ``scopt.analysis``
package other than ``api``, ``store`` and ``registry`` may define a module-level
``ANALYSES`` sequence of :class:`~scopt.analysis.api.AnalysisSpec`, or (when it
has no ``ANALYSES``) a single module-level ``SPEC``;
:func:`default_registry` imports them all and registers their specs. WP7
(``refs``, ``layout``, ``cfg``, ``recovery``), WP6 (``profile.counts``) and WP11
(``hotness``, ``locality``) add analyses that way.

Built in here: ``annotations@1`` (snapshot scope), which is how later passes
see ANNOTATE edits (§5.5).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from .api import AnalysisSpec

__all__ = [
    "AnalysisRegistry", "MissingAnalysis", "ANNOTATIONS", "BUILTIN_ANALYSES",
    "discover", "default_registry",
]

_SKIP_MODULES = frozenset({"api", "store", "registry"})


class MissingAnalysis(LookupError):
    """No registered analysis satisfies ``(name, min_version)``."""

    reason = "missing_analysis"


class AnalysisRegistry:
    def __init__(self, specs: Iterable[AnalysisSpec] = ()):
        self._specs: dict[str, dict[int, AnalysisSpec]] = {}
        for s in specs:
            self.register(s)

    def register(self, spec: AnalysisSpec, *, replace: bool = False) -> AnalysisSpec:
        if not isinstance(spec, AnalysisSpec):
            raise TypeError(f"not an AnalysisSpec: {spec!r}")
        if spec.version < 1:
            raise ValueError(f"{spec.name}: version must be >= 1")
        if spec.scope not in ("unit", "snapshot", "region"):
            raise ValueError(f"{spec.name}: bad scope {spec.scope!r}")
        byv = self._specs.setdefault(spec.name, {})
        old = byv.get(spec.version)
        if old is not None and old is not spec and not replace:
            raise ValueError(f"analysis {spec.name}@{spec.version} already registered")
        byv[spec.version] = spec
        return spec

    def lookup(self, name: str, min_version: int = 1) -> AnalysisSpec:
        """Highest registered version ``>= min_version``; raises :class:`MissingAnalysis`."""
        byv = self._specs.get(name, {})
        ok = [v for v in byv if v >= min_version]
        if not ok:
            have = sorted(byv)
            raise MissingAnalysis(f"analysis {name}@>={min_version} not registered"
                                  + (f" (have {have})" if have else ""))
        return byv[max(ok)]

    def exact(self, name: str, version: int) -> AnalysisSpec:
        try:
            return self._specs[name][version]
        except KeyError:
            raise MissingAnalysis(f"analysis {name}@{version} not registered") from None

    def has(self, name: str, min_version: int = 1) -> bool:
        return any(v >= min_version for v in self._specs.get(name, {}))

    def versions(self, name: str) -> tuple[int, ...]:
        return tuple(sorted(self._specs.get(name, {})))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[AnalysisSpec, ...]:
        return tuple(self._specs[n][v] for n in sorted(self._specs) for v in sorted(self._specs[n]))

    def copy(self) -> "AnalysisRegistry":
        return AnalysisRegistry(self.specs())

    def __contains__(self, name: object) -> bool:
        return name in self._specs


# ---------------------------------------------------------------------------
# Built-in: annotations@1
# ---------------------------------------------------------------------------

def _compute_annotations(ctx: Any, scope: Any) -> Mapping[str, Mapping[str, Any]]:
    """UnitId -> annotations, for every unit that has any (slot order)."""
    out = {}
    for uid in ctx.unit_ids():
        a = ctx.annotations(uid)
        if a:
            out[uid] = a
    return MappingProxyType(out)


ANNOTATIONS = AnalysisSpec(
    name="annotations", version=1, scope="snapshot", requires=(),
    inputs=frozenset({"annotations"}), compute=_compute_annotations,
    result_schema=Mapping)

BUILTIN_ANALYSES: tuple[AnalysisSpec, ...] = (ANNOTATIONS,)


def discover(registry: AnalysisRegistry | None = None,
             package: str = "scopt.analysis") -> AnalysisRegistry:
    """Register :data:`BUILTIN_ANALYSES` and every ``ANALYSES`` of ``package``'s modules."""
    reg = registry if registry is not None else AnalysisRegistry()
    for s in BUILTIN_ANALYSES:
        reg.register(s, replace=True)
    pkg = importlib.import_module(package)
    for mi in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if mi.name in _SKIP_MODULES or mi.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{package}.{mi.name}")
        specs = getattr(mod, "ANALYSES", None)
        if specs is None:
            one = getattr(mod, "SPEC", None)
            specs = (one,) if isinstance(one, AnalysisSpec) else ()
        for spec in specs:
            reg.register(spec, replace=True)
    return reg


_DEFAULT: AnalysisRegistry | None = None


def default_registry() -> AnalysisRegistry:
    """The discovered registry (built once per process; callers get a copy)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = discover()
    return _DEFAULT.copy()
