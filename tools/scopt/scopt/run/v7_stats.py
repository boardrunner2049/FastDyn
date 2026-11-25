"""Meaning and consistency of the QEMU_SC_V7_STATS counters.

The JSON keeps the schema string ``qemu-sc-v7-stats@1``; revision 2 of the
counters (``counters_revision``) adds keys only:

``loop_returns_nonnull``
    Returns to ``cpu_exec_loop`` that left a chainable ``last_tb``.  Counted
    whenever stats are enabled, also when no v7 directory is loaded.
``trusted_residual_exits`` (+ ``trusted_residual_exits_slot`` [goto_tb 0, 1]
and the per-region ``trusted_residual_exit_regions``)
    The subset of those returns whose ``last_tb`` is a trusted region
    descriptor: region exits taken through the dispatcher.  Exits through
    ``exit_tb(0)`` and longjmps have no ``last_tb``, so this is a lower bound.
``trusted_tb_chain_probes``
    Trusted-membership lookups made by the generic chaining path.
``chain_installs_trusted_source``
    Installed goto_tb patches whose source is a trusted region descriptor.

``trusted_tb_calls`` is *not* a count of region exits.  In trusted mode it is
the number of trusted-membership lookups, which revision 2 splits exactly as
``loop_returns_nonnull + trusted_tb_chain_probes``.  ``benchmark_*`` keys are
the same counters restricted to the restore benchmark window.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "qemu-sc-v7-stats@1"
COUNTERS_REVISION = 2

COUNTER_KEYS = (
    "loop_returns_nonnull",
    "trusted_residual_exits",
    "trusted_tb_chain_probes",
    "chain_installs_trusted_source",
    "benchmark_loop_returns_nonnull",
    "benchmark_trusted_residual_exits",
    "benchmark_trusted_tb_chain_probes",
    "benchmark_chain_installs_trusted_source",
)

LEGACY_LOOP_RETURNS_LABEL = ("non-null dispatcher returns estimated from "
                             "trusted_tb_calls, not region exits")


def counters_revision(stats: dict[str, Any]) -> int:
    value = stats.get("counters_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _trusted_loaded(stats: dict[str, Any]) -> bool:
    return stats.get("loaded") is True and stats.get("mode") == "trusted"


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def counter_problems(stats: dict[str, Any]) -> list[str]:
    """Return the violated revision-2 counter invariants ([] for legacy files)."""
    if counters_revision(stats) < COUNTERS_REVISION:
        return []
    problems: list[str] = []
    missing = [key for key in COUNTER_KEYS if not _count(stats.get(key))]
    if missing:
        return [f"counter keys missing or not counts: {missing}"]
    slots = stats.get("trusted_residual_exits_slot")
    regions = stats.get("trusted_residual_exit_regions")
    if (not isinstance(slots, list) or len(slots) != 2 or
            not all(_count(v) for v in slots)):
        return [f"trusted_residual_exits_slot is not two counts: {slots!r}"]
    if not isinstance(regions, list) or not all(
            isinstance(r, dict) and _count(r.get("exits")) and
            _count(r.get("benchmark_exits")) for r in regions):
        return ["trusted_residual_exit_regions is not a list of region counts"]

    returns = stats["loop_returns_nonnull"]
    exits = stats["trusted_residual_exits"]
    if exits > returns:
        problems.append(f"trusted_residual_exits {exits} > "
                        f"loop_returns_nonnull {returns}")
    if sum(slots) != exits:
        problems.append(f"trusted_residual_exits_slot sum {sum(slots)} != "
                        f"trusted_residual_exits {exits}")
    region_sum = sum(r["exits"] for r in regions)
    if region_sum != exits:
        problems.append(f"trusted_residual_exit_regions exits sum {region_sum} "
                        f"!= trusted_residual_exits {exits}")
    window_sum = sum(r["benchmark_exits"] for r in regions)
    if window_sum != stats["benchmark_trusted_residual_exits"]:
        problems.append(f"trusted_residual_exit_regions benchmark_exits sum "
                        f"{window_sum} != benchmark_trusted_residual_exits "
                        f"{stats['benchmark_trusted_residual_exits']}")
    over = [r.get("region") for r in regions if r["benchmark_exits"] > r["exits"]]
    if over:
        problems.append(f"regions with benchmark_exits > exits: {over}")
    for key in COUNTER_KEYS:
        if key.startswith("benchmark_"):
            total = key[len("benchmark_"):]
            if stats[key] > stats[total]:
                problems.append(f"{key} {stats[key]} > {total} {stats[total]}")

    if _trusted_loaded(stats):
        calls = stats.get("trusted_tb_calls")
        probes = stats["trusted_tb_chain_probes"]
        if calls != returns + probes:
            problems.append(f"trusted_tb_calls {calls} != loop_returns_nonnull "
                            f"{returns} + trusted_tb_chain_probes {probes}")
        # While trusted sources are never chained, every chain attempt past
        # the global switch probes both its source and its destination.
        if (stats["chain_installs_trusted_source"] == 0 and
                stats.get("chain_skipped_trusted_source") == 0):
            attempts = stats.get("chain_attempts")
            skipped = stats.get("chain_skipped_global")
            if _count(attempts) and _count(skipped) and \
                    probes != 2 * (attempts - skipped):
                problems.append(f"trusted_tb_chain_probes {probes} != 2 * "
                                f"(chain_attempts {attempts} - "
                                f"chain_skipped_global {skipped})")
    else:
        for key in ("trusted_residual_exits", "trusted_tb_chain_probes",
                    "chain_installs_trusted_source"):
            if stats[key]:
                problems.append(f"{key} {stats[key]} without a loaded "
                                f"trusted directory")
        if regions:
            problems.append("trusted_residual_exit_regions without a loaded "
                            "trusted directory")
    return problems


def residual_exits(stats: dict[str, Any], window: bool = False) -> int | None:
    """Trusted region exits through the dispatcher; None for legacy files.

    Never falls back to ``trusted_tb_calls``, which counts lookups.
    """
    if counters_revision(stats) < COUNTERS_REVISION:
        return None
    return stats.get("benchmark_trusted_residual_exits" if window
                     else "trusted_residual_exits")


def loop_returns(stats: dict[str, Any], window: bool = False) -> int | None:
    """Non-null dispatcher returns; None for legacy files."""
    if counters_revision(stats) < COUNTERS_REVISION:
        return None
    return stats.get("benchmark_loop_returns_nonnull" if window
                     else "loop_returns_nonnull")


def legacy_loop_returns(stats: dict[str, Any]) -> int | None:
    """Estimate non-null dispatcher returns from a pre-revision-2 trusted file.

    The result counts dispatcher returns (LEGACY_LOOP_RETURNS_LABEL), not
    region exits.  None unless the file is legacy, loaded and trusted.
    """
    if counters_revision(stats) >= COUNTERS_REVISION or not _trusted_loaded(stats):
        return None
    values = [stats.get(key) for key in ("trusted_tb_calls", "chain_attempts",
                                         "chain_skipped_global",
                                         "chain_skipped_trusted_source")]
    if not all(_count(v) for v in values[:3]):
        return None
    calls, attempts, skipped, trusted_source = values
    return calls - 2 * (attempts - skipped) + (trusted_source or 0)


def summary(stats: dict[str, Any]) -> dict[str, Any]:
    """The dispatcher-return counters of one stats file, labelled by revision."""
    out: dict[str, Any] = {"counters_revision": counters_revision(stats)}
    if out["counters_revision"] >= COUNTERS_REVISION:
        out.update({
            "loop_returns_nonnull": loop_returns(stats),
            "benchmark_loop_returns_nonnull": loop_returns(stats, window=True),
            "residual_exits": residual_exits(stats),
            "benchmark_residual_exits": residual_exits(stats, window=True),
            "chain_installs_trusted_source":
                stats.get("chain_installs_trusted_source"),
        })
    else:
        out["legacy_loop_returns"] = legacy_loop_returns(stats)
        out["legacy_loop_returns_label"] = LEGACY_LOOP_RETURNS_LABEL
    return out
