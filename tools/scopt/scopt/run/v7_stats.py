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

Revision 3 (trusted residual exits can be chained) adds:

``trusted_residual_exits_chainable`` / ``trusted_residual_exits_unchainable``
    Split of ``trusted_residual_exits`` by whether the exit left through an
    armed goto_tb slot of a per-member descriptor.  A chainable exit is then
    offered to ``tb_add_jump``; once chained, that slot stops returning to
    the dispatcher, so these count first uses (and re-chains), not traffic.
``trusted_residual_slots_armed``
    Residual goto_tb slots armed when trusted regions were (re)built.  Zero
    for legacy shared-descriptor caches, ``no-trusted-exit-chain`` and
    ``QEMU_SC_SEALED_SKIP_IRQ``.
``chain_allowed_trusted_source`` / ``chain_skipped_trusted_to_trusted``
    Chain decisions for a trusted source: allowed to an ordinary TB, or
    refused because the destination is a trusted region entry (which skips
    its poll).  Revision 3 always probes both ends of every chain attempt,
    so ``trusted_tb_chain_probes == 2 * (chain_attempts -
    chain_skipped_global)`` holds unconditionally.

``trusted_tb_calls`` is *not* a count of region exits.  In trusted mode it is
the number of trusted-membership lookups, which revision 2 splits exactly as
``loop_returns_nonnull + trusted_tb_chain_probes``.  ``benchmark_*`` keys are
the same counters restricted to the restore benchmark window.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "qemu-sc-v7-stats@1"
COUNTERS_REVISION = 3
#: First revision with dispatcher-return counters.
LOOP_RETURNS_REVISION = 2

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

#: Added in revision 3.
CHAIN_COUNTER_KEYS = (
    "trusted_residual_exits_chainable",
    "trusted_residual_exits_unchainable",
    "trusted_residual_slots_armed",
    "chain_allowed_trusted_source",
    "chain_skipped_trusted_to_trusted",
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
    """Return the violated revision-2/3 counter invariants ([] for legacy files)."""
    revision = counters_revision(stats)
    if revision < LOOP_RETURNS_REVISION:
        return []
    problems: list[str] = []
    keys = COUNTER_KEYS + (CHAIN_COUNTER_KEYS if revision >= 3 else ())
    missing = [key for key in keys if not _count(stats.get(key))]
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

    if revision >= 3:
        problems += _chain_problems(stats)
    if _trusted_loaded(stats):
        calls = stats.get("trusted_tb_calls")
        probes = stats["trusted_tb_chain_probes"]
        if calls != returns + probes:
            problems.append(f"trusted_tb_calls {calls} != loop_returns_nonnull "
                            f"{returns} + trusted_tb_chain_probes {probes}")
        # Every chain attempt past the global switch probes both its source
        # and its destination: always in revision 3, and in revision 2 while
        # trusted sources are never chained.
        if revision >= 3 or (stats["chain_installs_trusted_source"] == 0 and
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
                    "chain_installs_trusted_source") + (
                        CHAIN_COUNTER_KEYS if revision >= 3 else ()):
            if stats[key]:
                problems.append(f"{key} {stats[key]} without a loaded "
                                f"trusted directory")
        if regions:
            problems.append("trusted_residual_exit_regions without a loaded "
                            "trusted directory")
    return problems


def _chain_problems(stats: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    chainable = stats["trusted_residual_exits_chainable"]
    unchainable = stats["trusted_residual_exits_unchainable"]
    exits = stats["trusted_residual_exits"]
    if chainable + unchainable != exits:
        problems.append(f"trusted_residual_exits_chainable {chainable} + "
                        f"trusted_residual_exits_unchainable {unchainable} != "
                        f"trusted_residual_exits {exits}")
    allowed = stats["chain_allowed_trusted_source"]
    installs = stats["chain_installs_trusted_source"]
    if allowed > chainable:
        problems.append(f"chain_allowed_trusted_source {allowed} > "
                        f"trusted_residual_exits_chainable {chainable}")
    if installs > allowed:
        problems.append(f"chain_installs_trusted_source {installs} > "
                        f"chain_allowed_trusted_source {allowed}")
    to_trusted = stats["chain_skipped_trusted_to_trusted"]
    skipped = stats.get("chain_skipped_trusted_source")
    if _count(skipped) and to_trusted > skipped:
        problems.append(f"chain_skipped_trusted_to_trusted {to_trusted} > "
                        f"chain_skipped_trusted_source {skipped}")
    if stats["trusted_residual_slots_armed"] == 0 and chainable:
        problems.append(f"trusted_residual_exits_chainable {chainable} without "
                        "armed residual slots")
    return problems


def residual_exits(stats: dict[str, Any], window: bool = False) -> int | None:
    """Trusted region exits through the dispatcher; None for legacy files.

    Never falls back to ``trusted_tb_calls``, which counts lookups.
    """
    if counters_revision(stats) < LOOP_RETURNS_REVISION:
        return None
    return stats.get("benchmark_trusted_residual_exits" if window
                     else "trusted_residual_exits")


def loop_returns(stats: dict[str, Any], window: bool = False) -> int | None:
    """Non-null dispatcher returns; None for legacy files."""
    if counters_revision(stats) < LOOP_RETURNS_REVISION:
        return None
    return stats.get("benchmark_loop_returns_nonnull" if window
                     else "loop_returns_nonnull")


def legacy_loop_returns(stats: dict[str, Any]) -> int | None:
    """Estimate non-null dispatcher returns from a pre-revision-2 trusted file.

    The result counts dispatcher returns (LEGACY_LOOP_RETURNS_LABEL), not
    region exits.  None unless the file is legacy, loaded and trusted.
    """
    if counters_revision(stats) >= LOOP_RETURNS_REVISION or not _trusted_loaded(stats):
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
    if out["counters_revision"] >= LOOP_RETURNS_REVISION:
        out.update({
            "loop_returns_nonnull": loop_returns(stats),
            "benchmark_loop_returns_nonnull": loop_returns(stats, window=True),
            "residual_exits": residual_exits(stats),
            "benchmark_residual_exits": residual_exits(stats, window=True),
            "chain_installs_trusted_source":
                stats.get("chain_installs_trusted_source"),
        })
        if out["counters_revision"] >= 3:
            out.update({key: stats.get(key) for key in CHAIN_COUNTER_KEYS})
    else:
        out["legacy_loop_returns"] = legacy_loop_returns(stats)
        out["legacy_loop_returns_label"] = LEGACY_LOOP_RETURNS_LABEL
    return out
