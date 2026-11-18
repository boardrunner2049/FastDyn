"""``hostsym`` coverage: how much of a snapshot the kernel can analyse (§9.2, T-B3-4). WP19.

A unit is *analysable* when every path from offset 0 ends in an exit within
the budget (no UNSUPPORTED path) **and** the identity check (the unit against
itself) is ``equivalent``. Coverage is reported by unit count and weighted
by a profile phase (``steady`` entries of exact counts), the WP19 reported
outcome (target ≥ 99 % of compute steady weight).
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Mapping
from typing import Any

from .equiv import check_equiv
from .paths import explore
from .program import program_of, world_of

__all__ = ["coverage", "phase_weights"]


def phase_weights(profile: Mapping[str, Any] | str, phase: str = "steady") -> dict[str, int]:
    """uid -> exact entry count of ``phase`` (units with a non-exact count are omitted)."""
    doc = json.loads(open(profile).read()) if isinstance(profile, str) else profile
    pid = next((str(p["id"]) for p in doc["phases"] if p["name"] == phase), None)
    if pid is None:
        raise ValueError(f"profile has no phase {phase!r}")
    out: dict[str, int] = {}
    for u in doc["units"]:
        c = u.get("phase", {}).get(pid)
        if c is not None and u.get("status") == "exact":
            out[u["unit"]] = int(c.get("entries", 0))
    return out


def coverage(snapshot: Any, layout: Any, *, weights: Mapping[str, int] | None = None,
             contract: Any = None, identity: bool = True) -> dict[str, Any]:
    w = world_of(snapshot, layout)
    t0 = time.time()
    reasons: Counter[str] = Counter()
    n = ok = 0
    paths: Counter[int] = Counter()
    w_all = w_ok = 0
    bad: list[dict[str, Any]] = []
    for u in snapshot.units():
        n += 1
        wt = int((weights or {}).get(u.uid, 0))
        w_all += wt
        if u.meta_status != "ok":
            reasons[f"meta:{u.meta_status}"] += 1
            bad.append({"uid": u.uid, "weight": wt, "why": u.meta_status})
            continue
        p = program_of(u, w)
        ex = explore(p, layout=layout)
        paths[len(ex.paths)] += 1
        why = None
        if ex.budget_exhausted:
            why = "path_budget_exhausted"
        elif ex.unsupported:
            why = "unsupported:" + ",".join(sorted({x for x, _ in ex.unsupported}))
        elif identity:
            v = check_equiv(p, p, None, None, contract, layout=layout)
            if not v.ok:
                why = f"identity:{v.status}:{v.reason}"
        if why is None:
            ok += 1
            w_ok += wt
        else:
            reasons[why] += 1
            bad.append({"uid": u.uid, "vaddr": hex(u.vaddrs[0]) if u.vaddrs else None,
                        "weight": wt, "why": why})
    bad.sort(key=lambda d: -d["weight"])
    return {"units": n, "analysable": ok, "unit_coverage": ok / n if n else None,
            "weight_total": w_all, "weight_analysable": w_ok,
            "weight_coverage": (w_ok / w_all) if w_all else None,
            "reasons": dict(reasons.most_common()), "paths_histogram": dict(sorted(paths.items())),
            "max_paths": max(paths) if paths else 0, "not_analysable": bad[:50],
            "seconds": round(time.time() - t0, 1)}
