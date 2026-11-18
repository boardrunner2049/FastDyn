"""WP19: the §9.3 mutation suite (T-B3-5): 100 % rejected, statically.

Unit stage: the mutants that need no guard emitter, on synthetic units (V18
runs the same suite) and as same-length mutants of real compute/minimal/fault
units. The guard-stub mutants run here with WP19's reference emitter of the
§4.1 template (``needs_emitter``); with WP18's emitter/placement they are the
WP19 integration acceptance (not run at the unit stage).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from scopt.validate_sym.mutants import DETECTORS, STATIC_ONLY, real_mutants, synthetic_suite
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
SNAPS = {
    "minimal": ROOT / "runs/wp15a/minimal/store/snapshots/"
    "f1ed14d98db4bcae54a1c37f07b1d6fabcfcea345a56edf1df1846c4d0aaee44",
    "fault": ROOT / "runs/wp15a/fault/store/snapshots/"
    "4e404d5e7335399768249ef95453594fe8098647ef7927c1234c5a27dee16841",
    "compute": ROOT / "runs/b2/compute/store/snapshots/"
    "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2",
}

UNIT_MUTANTS = {  # WP19 unit acceptance: the mutants that need no guard emitter
    "fold_flip", "fold_without_fact", "fact_falsified", "fact_claim_mismatch",
    "guest_store_delete", "update_mem_delete", "log_reg_delete", "flag_store_before_ldst",
    "flag_store_before_helper", "brcond_reload", "cdio_last_insn_direct_chain",
    "it_block_d4", "icount_immediate", "search_row", "host_abs_in_env",
    "nop_dead_result_ldst",
}


@pytest.fixture(scope="module")
def suite():
    return synthetic_suite()


def test_synthetic_suite_all_rejected(suite):
    doc = suite.to_json()
    assert doc["all_rejected"], [r for r in doc["results"] if not r["rejected"]]
    assert doc["controls_ok"], doc["controls"]
    names = {r.name for r in suite.results}
    assert UNIT_MUTANTS <= names
    # rejected as a static *failure*, never only as "inconclusive"
    assert {r.verdict for r in suite.results} == {"mismatch"}


@pytest.mark.parametrize("name,reason", [
    ("guest_store_delete", "edit_inside_ldst_block"),
    ("nop_dead_result_ldst", "edit_inside_ldst_block"),
    ("it_block_d4", "edit_inside_ldst_block"),
    ("search_row", "recovery_rows_changed"),
    ("host_abs_in_env", "host_abs_aliases_env"),
    ("fact_claim_mismatch", "fact_derivation_mismatch"),
    ("falsified_fact_link", "fact_derivation_mismatch"),
    ("drop_g_bp", "guard_contract_violation"),
    ("drop_g_pc", "guard_contract_violation"),
    ("shared_variant_suffix", "fact_not_constant"),
])
def test_specific_reasons(suite, name, reason):
    r = next(x for x in suite.results if x.name == name)
    assert r.reason == reason, r.detail


def test_every_mutant_has_a_detector_or_a_static_only_reason(suite):
    for r in suite.results:
        assert r.name in DETECTORS, r.name
        if DETECTORS[r.name] is None:
            assert r.name in STATIC_ONLY
    # close-out: the LOG_REG spin mutants are observable only on a full coverage ring
    # (design/closeout-logreg.md §A.8; disclosed in the gate reports)
    # F13: rewrites around a NO_RWG call that writes env (no shipped template makes one;
    # notes/no-rwg-soundness.md)
    assert set(STATIC_ONLY) == {"drop_g_cond", "drop_g_flags", "drop_g_flags2",
                                "logreg_spin_nop", "logreg_spin_retarget",
                                "nrwg_hflags_writeback", "nrwg_hflags_read_hoisted",
                                "nrwg_hflags_const_after_call", "nrwg_hflags_fact_after_call",
                                "nrwg_unknown_helper_writeback", "nrwg_drop_g_flags_after_call",
                                "nrwg_drop_g_flags2_after_call"}


def test_guard_mutants_use_reference_emitter(suite):
    guard = [r for r in suite.results if r.needs_emitter]
    assert {r.name for r in guard} >= {"drop_g_bp", "drop_g_thumb", "drop_g_cond", "drop_g_flags",
                                       "drop_g_flags2", "drop_g_pc", "guard_before_effect",
                                       "falsified_fact_link", "shared_variant_suffix"}
    assert all(r.rejected for r in guard)


def _real(name: str, limit: int):
    p = SNAPS[name]
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    from scopt.analysis.hostops import build_hostops
    from scopt.analysis.obs_contract import build_contract
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot
    from scopt.validate_sym import program_of, world_of

    s = open_snapshot(p)
    lay = find_envlayout(s.runtime["qemu_sha256"])
    w = world_of(s, lay)
    c = build_contract(lay, s.runtime)
    progs = {u.uid: program_of(u, w) for u in s.units()}
    hops = {u.uid: build_hostops(u, lay, s.header.code_address) for u in s.units()}
    return real_mutants(progs, hops, c, lay, limit_per_kind=limit,
                        cdio_contract=dataclasses.replace(c, tb_start_cb_possible=True))


@pytest.mark.real
@pytest.mark.parametrize("name", ["minimal", "fault"])
def test_real_mutants_small(name):
    r = _real(name, 10)
    assert r.results
    assert all(x.rejected for x in r.results), [x.to_json() for x in r.results if not x.rejected]


@pytest.mark.real
@pytest.mark.slow
def test_real_mutants_compute():
    r = _real("compute", 10)
    kinds = {x.name for x in r.results}
    assert {"guest_store_delete", "icount_immediate", "search_row", "brcond_reload",
            "cdio_last_insn_direct_chain"} <= kinds
    assert all(x.rejected for x in r.results), [x.to_json() for x in r.results if not x.rejected]
    assert {x.verdict for x in r.results} == {"mismatch"}
