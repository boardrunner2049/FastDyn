"""The scopt O4 pipelines: frozen ``O4-r1.toml`` and ``O4-r3.toml``, and the moving alias ``O4.toml``.

O4 revision 1 is ``sealed-pgo-7-b3`` (round 2's c7b3) with the three default-disabled B3
passes opted in by the pipeline (``enabled = true``) and ``member_slot_skip = "on"``.
"""

from scopt.manager import DEFAULT_DISABLED_PASSES, PIPELINES_DIR, PassManager, R, load_pipeline
from scopt.manager_b3 import B3Env
from tests.synth.fake_snapshot import fake_snapshot

_B3_OPT_IN = {"specialize-returns", "chain-returns", "chain-static"}
#: Frozen: O4-r1.toml must never change (a new decision adds O4-r2.toml).
_O4_R1_ID = "sha256:48e614409f3a9684bb435924710cfcbdb50e99124a51512fa729c72f47ef61d1"


def _load(name):
    return load_pipeline(PIPELINES_DIR / f"{name}.toml")


def test_o4_r1_is_frozen():
    assert _load("O4-r1").pipeline_id == _O4_R1_ID


def test_o4_r1_is_sealed_pgo_7_b3_with_b3_opted_in_and_member_skip_required():
    o4, ref = _load("O4-r1"), _load("sealed-pgo-7-b3")
    assert o4.allowed_capabilities == ref.allowed_capabilities
    assert [e.name for e in o4.passes] == [e.name for e in ref.passes]
    for got, want in zip(o4.passes, ref.passes):
        assert got.version == want.version
        assert got.enabled == (got.name in _B3_OPT_IN)
        extra = {"member_slot_skip": "on"} if got.name == "firmware-inline" else {}
        assert dict(got.params) == {**want.params, **extra}
    assert {e.name for e in o4.passes} & DEFAULT_DISABLED_PASSES == _B3_OPT_IN


#: Frozen: O4-r3.toml must never change (a new decision adds O4-r4.toml).
_O4_R3_ID = "sha256:48552af72b527e97fb66ba1a251a22ec46de31e782398a0e407c728d84048872"


def test_o4_r3_is_frozen():
    assert _load("O4-r3").pipeline_id == _O4_R3_ID


def test_o4_r3_is_o4_r1_with_priced_recursive_regions_and_ranked_chain_static():
    r1, r3 = _load("O4-r1"), _load("O4-r3")
    assert r3.allowed_capabilities == r1.allowed_capabilities
    assert [(e.name, e.version, e.enabled) for e in r3.passes] == \
        [(e.name, e.version, e.enabled) for e in r1.passes]     # every revision-1 pass stays
    for a, b in zip(r1.passes, r3.passes):
        want = dict(a.params)
        if a.name == "firmware-inline":
            del want["min_call_count"]
            want.update(mode="exhaustive", cost_model="profile")
        if a.name == "chain-static":
            want.update(rank_by_net_work=True, c_target_check=0.0)
        assert dict(b.params) == want, a.name


def test_o4_toml_is_the_newest_revision():
    newest = max((p.stem for p in PIPELINES_DIR.glob("O4-r*.toml")),
                 key=lambda n: int(n.removeprefix("O4-r")))
    alias, rev = _load("O4"), _load(newest)
    assert alias.name == "O4" and rev.name == newest
    assert alias.passes == rev.passes
    assert alias.allowed_capabilities == rev.allowed_capabilities


def test_o4_runs_the_b3_passes_without_enable():
    for name in ("O4", "O4-r1", "O4-r3"):
        pm = PassManager(fake_snapshot(2), _load(name), b3=B3Env())
        assert pm.disabled_by_default == frozenset()
        run = pm.execute()
        for stage in _B3_OPT_IN:
            assert run.outcome(stage).reason not in (R.DISABLED_BY_DEFAULT, R.ABLATION_DISABLED)


def test_o4_layout_retains_the_reference_stages_and_their_parameters():
    reference, variant = _load("O4-r3"), _load("O4-layout")
    layout = ["hotcold-partition", "cluster-hot", "order-cold"]
    expected = [entry.name for entry in reference.passes]
    index = expected.index("firmware-inline")
    assert [entry.name for entry in variant.passes] == expected[:index] + layout + expected[index:]
    for entry in reference.passes:
        selected = next(stage for stage in variant.passes if stage.name == entry.name)
        assert (selected.enabled, selected.version) == (entry.enabled, entry.version)
        changes = {"max_units": -1} if entry.name == "chain-static" else {}
        assert selected.params == {**entry.params, **changes}
