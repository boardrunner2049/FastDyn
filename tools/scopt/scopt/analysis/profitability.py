"""Training profitability (B3/B4 design §10.3, spec:110). WP21.

All counts come from the **train** profile of the base snapshot (steady phase);
the constants from WP16's calibration (``runs/b3-explore/wp16/calibration.json``,
design WP16 note item 1: ``c_lookup`` 7.6 ns, ``c_guard`` 1.5 ns, ``c_fail``
not separable from 0). Defaults are those values; a different calibration can
be passed in (``params`` of the passes, or :func:`load_calibration`).

* **Redirected exit** (GENERAL or SPEC slot): ``saving(E) = Σ n_i·c_lookup −
  N_E·c_guard − (N_E − Σ n_i)·c_fail``, ``n_i`` the train counts of the guarded
  targets, ``N_E`` every lookup of the exit. Rejected (``not_profitable``) when
  ``saving ≤ 0`` or the guard pass share ``Σ n_i / N_E < 0.5``.
* **CP / BF / DHO** on a variant: ``saving = work units removed × C' entries``,
  rejected when ``≤ 0``. The design counts "executed non-nop host instructions
  removed"; CP replaces an env *load* by an immediate form (no instruction is
  removed) and BF may replace a ``jcc`` by a ``jmp``, so the unit here is: one per
  env load folded (CP), one per conditional branch decided (BF), one per
  instruction nopped (DHO) — recorded as a WP21 implementation note.
* **B4**: ``saving = removed stores × unit entries``. No calibrated cost exists
  (WP16: D1 below the noise floor), so validated B4 edits are always applied and
  the artifact is flagged ``predicted_unprofitable`` when the model predicts less
  than 0.5 % of the train BENCH time (``predicted_fraction``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULTS", "load_calibration", "ExitSaving", "exit_saving", "simplification_saving",
           "b4_saving", "b4_predicted_unprofitable", "MIN_PASS_SHARE", "CALIBRATION_PATH"]

#: WP16 calibration, ns (design WP16 implementation note item 1)
DEFAULTS: Mapping[str, float] = {"c_lookup": 7.6, "c_guard": 1.5, "c_fail": 0.0,
                                 "ns_per_tb_entry": 10.7}
MIN_PASS_SHARE = 0.5
CALIBRATION_PATH = os.environ.get(
    "SCOPT_B3_CALIBRATION", "/data/extra_copy/fastdyn/runs/b3-explore/wp16/calibration.json")


def load_calibration(path: str | None = None) -> dict[str, float]:
    """``c_lookup``/``c_guard``/``c_fail`` from a WP16 ``calibration.json`` (``value_ns``);
    a CI that includes 0 for ``c_fail`` gives 0 (WP16: "not separable"); missing file or
    keys keep :data:`DEFAULTS`."""
    out = dict(DEFAULTS)
    p = path or CALIBRATION_PATH
    try:
        doc = json.loads(open(p).read())
    except (OSError, ValueError):
        return out
    c = doc.get("constants", {})
    for k in ("c_lookup", "c_guard"):
        v = (c.get(k) or {}).get("value_ns")
        if isinstance(v, (int, float)):
            out[k] = float(v)
    cf = c.get("c_fail") or {}
    ci = cf.get("ci95_ns")
    if isinstance(ci, list) and len(ci) == 2 and ci[0] <= 0 <= ci[1]:
        out["c_fail"] = 0.0
    elif isinstance(cf.get("value_ns"), (int, float)):
        out["c_fail"] = max(0.0, float(cf["value_ns"]))
    return out


@dataclass(frozen=True)
class ExitSaving:
    guarded: int  # Σ n_i
    total: int  # N_E
    saving_ns: float
    pass_share: float
    profitable: bool
    reason: str | None  # not_profitable | None

    def to_json(self) -> dict[str, Any]:
        return {"guarded": self.guarded, "total": self.total,
                "saving_ns": round(self.saving_ns, 3), "pass_share": round(self.pass_share, 6),
                "profitable": self.profitable, "reason": self.reason}


def exit_saving(guarded: int, total: int, c: Mapping[str, float] | None = None, *,
                min_share: float = MIN_PASS_SHARE) -> ExitSaving:
    k = {**DEFAULTS, **(c or {})}
    total = max(int(total), int(guarded))
    s = guarded * k["c_lookup"] - total * k["c_guard"] - (total - guarded) * k["c_fail"]
    share = guarded / total if total else 0.0
    ok = s > 0 and share >= min_share
    return ExitSaving(int(guarded), int(total), float(s), share, ok,
                      None if ok else "not_profitable")


def simplification_saving(units_removed: int, entries: int) -> tuple[int, bool]:
    """(saving, profitable) of a CP/BF/DHO patch set on a variant (§10.3)."""
    s = int(units_removed) * int(entries)
    return s, s > 0


def b4_saving(removed_stores: int, entries: int) -> int:
    return int(removed_stores) * int(entries)


def b4_predicted_unprofitable(removed_store_execs: int, train_bench_ns: float | None, *,
                              ns_per_store: float = 0.0361,
                              threshold: float = 0.005) -> dict[str, Any]:
    """§10.3 B4 flag: predicted saving below ``threshold`` of the train BENCH time.
    ``ns_per_store`` default: WP16's measured D1 saving per store (CI includes 0)."""
    if not train_bench_ns:
        return {"predicted_fraction": None, "predicted_unprofitable": None,
                "reason": "no train BENCH time"}
    frac = removed_store_execs * ns_per_store / float(train_bench_ns)
    return {"predicted_fraction": frac, "predicted_unprofitable": frac < threshold}
