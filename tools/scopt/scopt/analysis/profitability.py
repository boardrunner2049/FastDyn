"""Training profitability (B3/B4 design §10.3, spec:110). WP21.

All counts come from the **train** profile of the base snapshot (steady phase);
the constants from WP16's calibration (``runs/b3-explore/wp16/calibration.json``,
design WP16 note item 1: ``c_lookup`` 7.6 ns, ``c_guard`` 1.5 ns, ``c_fail``
not separable from 0). Defaults are those values; a different calibration can
be loaded by the B3 manager or supplied through pass parameters.

* **SPEC redirected exit**: ``saving(E) = Σ n_i·c_lookup −
  N_E·c_guard − (N_E − Σ n_i)·c_fail``, ``n_i`` the train counts of the guarded
  targets, ``N_E`` every lookup of the exit. Rejected (``not_profitable``) when
  ``saving ≤ 0`` or the guard pass share ``Σ n_i / N_E < 0.5``.
  The GENERAL model in :mod:`scopt.passes.chain_returns` additionally accounts
  for first-hit link installation in each training replay.
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
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import fastdyn_root

__all__ = ["DEFAULTS", "load_calibration", "ExitSaving", "exit_saving", "simplification_saving",
           "b4_saving", "b4_predicted_unprofitable", "MIN_PASS_SHARE", "CALIBRATION_PATH",
           "calibration_params"]

#: WP16 calibration, ns (design WP16 implementation note item 1)
DEFAULTS: Mapping[str, float] = {"c_lookup": 7.6, "c_guard": 1.5, "c_fail": 0.0,
                                 "ns_per_tb_entry": 10.7}
MIN_PASS_SHARE = 0.5
_CALIBRATION_FILE = Path("runs/b3-explore/wp16/calibration.json")
CALIBRATION_PATH = str(fastdyn_root() / _CALIBRATION_FILE)
_COSTS = ("c_lookup", "c_guard", "c_fail", "c_link")


def _finite_number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _measured_costs(values: Mapping[str, Any]) -> dict[str, float]:
    costs = {}
    for key in _COSTS:
        value = _finite_number(values.get(key))
        if value is not None and value >= 0:
            costs[key] = value
    return costs


def load_calibration(path: str | Path | None = None) -> dict[str, float]:
    """Read finite, nonnegative WP16 costs, keeping each missing or invalid default.

    ``c_fail`` is zero when its finite 95% confidence interval contains zero.
    A measured ``c_link`` is optional. It applies only to GENERAL installation.
    """
    if path is None:
        path = os.environ.get("SCOPT_B3_CALIBRATION") or fastdyn_root() / _CALIBRATION_FILE
    out = dict(DEFAULTS)
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    constants = doc.get("constants") if isinstance(doc, dict) else None
    if not isinstance(constants, dict):
        return out
    measurements = {key: row for key, row in constants.items() if isinstance(row, dict)}
    out.update(_measured_costs({key: row.get("value_ns") for key, row in measurements.items()}))
    interval = measurements.get("c_fail", {}).get("ci95_ns")
    if isinstance(interval, list) and len(interval) == 2:
        lo, hi = map(_finite_number, interval)
        if lo is not None and hi is not None and lo <= 0 <= hi:
            out["c_fail"] = 0.0
    return out


def calibration_params(explicit: Mapping[str, Any] | None = None) -> dict[str, float]:
    """Return just the B3 costs that differ from unchanged built-in behavior."""
    costs = load_calibration()
    if explicit is not None:
        costs.update(_measured_costs(explicit))
    return {key: costs[key] for key in _COSTS
            if key in costs and costs[key] != DEFAULTS.get(key, 0.0)}


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
