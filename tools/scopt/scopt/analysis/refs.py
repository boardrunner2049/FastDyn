"""``refs.v1`` and ``recovery.v1`` analyses (design §5.3). WP7.

Both are thin views of what the importer (§2.7, :mod:`scopt.importer`)
already computed and stored on each ``TBUnit``; they exist so that passes read
relocation and recovery facts through the AnalysisStore with dependency
tracking (WP9 discovers module-level ``ANALYSES``).

``refs.v1`` (unit scope) value::

    {"unit": uid, "status": "ok_relocatable"|"pinned"|"no_meta",
     "relocatable": bool, "reasons": [...],
     "fixups": [{"field_off", "width", "rule", "site_kind", "target_class", "target"}],
     "site_kinds": {"HELPER_CALL": n, ...}}

``recovery.v1`` (unit scope) value::

    {"unit": uid, "points": [{"id", "host_return_offset", "insn", "words", "source"}]}
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..canon import hexaddr
from ..formats.enums import SiteKind
from .api import AnalysisSpec, ScopeRef

__all__ = ["REFS", "RECOVERY", "ANALYSES", "refs_value", "recovery_value"]


def _status(u: Any) -> str:
    if u.meta_status == "no_meta":
        return "no_meta"
    return "ok_relocatable" if u.reloc.relocatable else "pinned"


def refs_value(u: Any) -> dict[str, Any]:
    return {
        "unit": u.uid, "status": _status(u), "relocatable": bool(u.reloc.relocatable),
        "reasons": list(u.reloc.reasons),
        "fixups": [{"field_off": f.field_off, "width": f.width, "rule": f.rule,
                    "site_kind": SiteKind(f.site_kind).name, "target_class": f.target_class,
                    "target": None if f.target_abs is None else hexaddr(f.target_abs)}
                   for f in u.reloc.fixups],
        "site_kinds": dict(sorted(Counter(s.kind.name for s in u.sites).items())),
    }


def recovery_value(u: Any) -> dict[str, Any]:
    return {"unit": u.uid,
            "points": [{"id": p.id, "host_return_offset": p.host_return_offset, "insn": p.insn,
                        "words": [hexaddr(w) for w in p.words], "source": p.source}
                       for p in u.recovery]}


def _refs(ctx: Any, scope: ScopeRef) -> dict[str, Any]:
    return refs_value(ctx.unit(scope.id))


def _recovery(ctx: Any, scope: ScopeRef) -> dict[str, Any]:
    return recovery_value(ctx.unit(scope.id))


REFS = AnalysisSpec(name="refs", version=1, scope="unit", requires=(),
                    inputs=frozenset({"code", "meta"}), compute=_refs, result_schema=dict)
RECOVERY = AnalysisSpec(name="recovery", version=1, scope="unit", requires=(),
                        inputs=frozenset({"code", "meta"}), compute=_recovery, result_schema=dict)
ANALYSES = (REFS, RECOVERY)
