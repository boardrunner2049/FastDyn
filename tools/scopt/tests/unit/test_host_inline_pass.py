"""Framework checks for the standalone native inline pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.api import AnalysisSpec, ScopeRef
from scopt.analysis.host_calls import HostCallSite
from scopt.analysis.native_callees import NativeCallBinding
from scopt.analysis.registry import AnalysisRegistry
from scopt.analysis.store import AnalysisInput
from scopt.canon import sha256_id
from scopt.formats.enums import Encoding, SiteKind
from scopt.manager import PIPELINES_DIR, PassManager, PassRegistry, load_pipeline
from scopt.model.native import supports_tcg_integer_abi
from scopt.model.unit import Bindings, HelperBinding, Site
from scopt.passes import host_inline
from scopt.passes.base import Outcome, PassDescriptor, PassResult, StaleFact
from tests.synth.fake_snapshot import FakeSnapshot, fake_snapshot

_IMAGE_ID = sha256_id(b"native image e.g. validated in a separate resolver test")
_NATIVE_INPUT = sha256_id(b"native image manifest")
_CALLEE_CODE = bytes.fromhex("83c007c3")


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = fake_snapshot(2)
    units, analyses = [], {}
    for u in fake.units():
        host = fake.header.code_address + u.tc_offset
        target = host + 0x2000
        displacement = target - (host + 5)
        call_bytes = b"\xe8" + displacement.to_bytes(4, "little")
        caller = call_bytes + b"\x90\x90"
        row = Site(1, 0, 4, Encoding.REL32, SiteKind.HELPER_CALL, 7, target, 0, displacement)
        helper = HelperBinding(7, "test", "qemu", "helper_test", 5, target)
        u = replace(u, code=caller, tc_size=len(caller), ldst_end=len(caller), pool_start=None,
                    sites=(row,), bindings=Bindings((helper,), ()))
        units.append(u)
        call = HostCallSite(
            uid=u.uid, host_off=0, original_bytes=call_bytes, encoding="rel32", continuation_off=5,
            insn_idx=0, guest_pcs=u.vaddrs, opmark_off=0, helper_id=7, helper_name="test",
            helper_flags=5, target_abs=target, pool_slot_off=None, module="qemu",
            symbol="helper_test", call_class="pure", recovery_return_off=5, reason=None)
        callee = SimpleNamespace(
            path="/exact/to/qemu", elf_address=0x5000, image_offset=0x5000,
            image_id=_IMAGE_ID, code_hash=sha256_id(_CALLEE_CODE), symbol="helper_test",
            size=len(_CALLEE_CODE), code=_CALLEE_CODE, role="qemu", abi="sysv-amd64",
            runtime_address=target, tcg_typemask=2 | (2 << 3))
        analyses[u.uid] = (NativeCallBinding(call, callee, None, _NATIVE_INPUT),)
    snap = FakeSnapshot(units, fake.snapshot_id, fake.header, runtime={"supercache_flat": False})

    def native_lookup(ctx, scope):
        assert ctx.external_input("native-images").input_id == _NATIVE_INPUT
        ctx.certify()
        return analyses[scope.id]

    def code_fact(ctx, scope):
        ctx.certify()
        return ctx.unit(scope.id).content_hash

    registry = AnalysisRegistry([
        AnalysisSpec("envlayout", 1, "snapshot", (), frozenset(),
                     lambda _ctx, _scope: {"bound": True}, dict),
        AnalysisSpec("native-callees", 1, "unit", (), frozenset({"external:native-images"}),
                     native_lookup, tuple),
        AnalysisSpec("code-fact", 1, "unit", (), frozenset({"code"}), code_fact, str),
    ])
    bridge = tmp_path / "bridge"
    bridge.write_bytes(b"synthetic bridge adapter")
    monkeypatch.setenv("SCOPT_HOST_INLINE_BRIDGE", str(bridge))

    def bridge_result(argv, deadline, request=None):
        if argv[1:] == ["--version"]:
            return b"scopt-native bridge 1 (LLVM/BOLT 20.1.2)"
        assert json.loads(request)["code_sha256"] == sha256_id(_CALLEE_CODE)
        return json.dumps({
            "version": 1, "status": "ok", "code": "83c007e900000000",
            "origins": [{"source_off": 0, "source_size": 3, "output_off": 0, "output_size": 3},
                        {"source_off": 3, "source_size": 1, "output_off": 3, "output_size": 5}],
            "relocations": [{"field_off": 4, "width": 4, "target": "continuation"}],
        }).encode()

    monkeypatch.setattr(host_inline, "_bridge_call", bridge_result)
    monkeypatch.setattr(host_inline, "prepare_native_images",
                        lambda *args, **kwargs: AnalysisInput(_NATIVE_INPUT, ()))
    return snap, registry


def test_host_inline_publishes_generation_and_invalidates_old_code(world):
    snap, registry = world
    seen = []

    class Observe:
        descriptor = PassDescriptor(name="observe", version=1, requires=(("code-fact", 1),))

        def run(self, view, ctx, edits):
            u = view.units()[0]
            assert view.working_set.generation == 1
            assert len(u.native_fragments) == 1
            seen.append(ctx.fact("code-fact", ScopeRef("unit", u.uid)).value)
            return PassResult(Outcome.NO_CHANGE, "observed")

    pipe = {"name": "test", "allowed_capabilities": ["INLINE_CALL"],
            "pass": [{"name": "host-inline", "version": 1, "params": {"max_sites": 1}},
                     {"name": "observe", "version": 1}]}
    pm = PassManager(snap, pipe, passes=PassRegistry((host_inline.HostInline, Observe)),
                     analyses=registry)
    orig = snap.units()[0]
    scope = ScopeRef("unit", orig.uid)
    old_fact = pm.store.get("code-fact", scope)
    result = pm.execute()
    assert seen == [result.working_set.units()[0].content_hash] and seen != [orig.content_hash]
    assert not orig.native_fragments
    assert len(result.edits) == 1
    assert result.edits[0].payload.target == orig.uid
    assert result.passes[0].stats["sites:site_budget"] == 1
    with pytest.raises(StaleFact):
        pm.store.check_fact(old_fact)


def test_host_inline_budget_exact_growth_and_ablation(world):
    snap, registry = world
    pipeline = load_pipeline("host-inline")
    enabled = PassManager(snap, pipeline, analyses=registry,
                          params={"host-inline": {"max_total_growth": 8}}).execute()
    assert len(enabled.edits) == 1
    assert enabled.passes[0].stats["emitted_growth_bytes"] == 8
    row = next(r for r in enabled.ledger if r.id == "host-inline@1")
    assert row.counts["emitted_growth_bytes"] == 8
    provenance = enabled.pipeline_provenance()[0]
    assert provenance["x_counts"]["emitted_growth_bytes"] == 8
    assert provenance["x_diagnostics"]["sites"]
    assert enabled.passes[0].stats["sites:growth_budget"] == 1
    disabled = PassManager(snap, pipeline, analyses=registry, disable=["host-inline"]).execute()
    assert not disabled.edits and disabled.working_set.generation == 0
    assert disabled.passes[0].reason == "ablation_disabled"


@pytest.mark.parametrize(("typemask", "admitted"), [
    (2, True), (4 | (2 << 3), True), (sum(2 << (3 * i) for i in range(7)), True),
    (sum(2 << (3 * i) for i in range(8)), False), (6 | (2 << 3), False),
    (2 | (6 << 3), False), (2 | (7 << 3), False), (0 | (2 << 3), False),
    (2 | (2 << 6), False),
])
def test_register_only_tcg_argument_types(typemask, admitted):
    assert supports_tcg_integer_abi(typemask) is admitted


@pytest.mark.parametrize(("prefix", "exact"), [
    (b"", True),
    (bytes.fromhex("90"), True),
    (bytes.fromhex("7401 90"), True),  # both paths converge at the call
    (bytes.fromhex("75fe"), False),    # backward edge changes prefix count
    (bytes.fromhex("e800000000"), False),  # earlier helper may not return
    (bytes.fromhex("c3"), False),
])
def test_tb_entry_is_call_count_only_for_must_execute_site(prefix, exact):
    call = bytes.fromhex("e800000000")
    unit = SimpleNamespace(code=prefix + call, tc_offset=0, pool_start=None,
                           ldst_end=len(prefix) + len(call))
    binding = SimpleNamespace(call=SimpleNamespace(call_class="pure",
                                                   host_off=len(prefix)))

    assert host_inline.HostInline._tb_entries_are_call_count(
        unit, binding, 0x1000) is exact


def test_slow_path_tb_entries_are_never_call_counts():
    call = bytes.fromhex("e800000000")
    unit = SimpleNamespace(code=call, tc_offset=0, pool_start=None,
                           ldst_end=len(call))
    binding = SimpleNamespace(call=SimpleNamespace(call_class="ldst_slow", host_off=0))

    assert not host_inline.HostInline._tb_entries_are_call_count(unit, binding, 0x1000)



def test_host_inline_refuses_stale_envlayout(world):
    snap, registry = world
    registry.register(AnalysisSpec("envlayout", 1, "snapshot", (), frozenset(),
                                   lambda _ctx, _scope: {"bound": False}, dict), replace=True)
    result = PassManager(snap, "host-inline", analyses=registry).execute()
    assert not result.edits and result.passes[0].reason == "guard_layout_unbound"


# -- hot_loop_kinds: which observed transfers close a hot_loop_only cycle -----

#: The host-inline entry of sealed-pgo-7 and sealed-pgo-7-b3 (round 2's p7 and
#: c7b3) and its params hash, recorded before hot_loop_kinds existed.  The
#: parameter defaults to "transfer" in the pass, never in a pipeline, so the
#: hash and the receipt of these pipelines stay as timed.
_PGO7_HOST_PARAMS = {"mode": "maximal", "trusted_all_helpers": True, "trusted_rank_by": "frequency",
                     "trusted_selected_root": True, "hot_loop_only": True, "min_site_calls": 1000}
_PGO7_HOST_PARAMS_SHA256 = "sha256:1e16960e26dfd595395c0ddf2ba17a0d2a84ce7a157aa1be5859a8e6be2fc668"
#: O4 candidate A4 (O4-design.md 1.8, o4-hotgraph.toml).
_HOTGRAPH_PARAMS = {"mode": "maximal", "trusted_all_helpers": True,
                    "trusted_max_source_graph_bytes": 32768, "trusted_rank_by": "frequency",
                    "hot_loop_only": True, "hot_loop_kinds": "firmware", "min_site_calls": 1000}
_LOOKUP_GRAPH_BYTES = 50_345      # helper_lookup_tb_ptr's captured graph on the -O4 images


def _hot_edges():
    """Profile rows (src, dst, kind, count) over the six hot-world units.

    a <-> b: a thread-mode cycle closed only by exit_null (a BASEPRI critical
    section); isr -> c exit_null from handler mode (a possible EXC_RETURN),
    c -> isr chained; d <-> e: an ordinary chained/looked-up cycle.
    """
    return [("a", "b", "chain0", 5000), ("b", "a", "exit_null", 5000),
            ("isr", "c", "exit_null", 5000), ("c", "isr", "chain0", 5000),
            ("d", "e", "chain0", 5000), ("e", "d", "lookup_hit", 5000)]


@pytest.fixture
def hot_world(tmp_path, monkeypatch):
    """One helper call per unit; exact edges and whole-run site counts for each."""
    from scopt.passes.host_inline_graph import HostInlineGraph
    from scopt.validate_sym.native_inline import NativeInlineError

    from .test_firmware_inline import _fake_view

    names = ("a", "b", "isr", "c", "d", "e")
    units = {n: SimpleNamespace(
        uid=f"tb:{n}", slot=i, key=SimpleNamespace(cs_base=1 if n == "isr" else 0),
        sites=(SimpleNamespace(kind=SiteKind.HELPER_CALL, insn_off=0),),
        bindings=SimpleNamespace(helpers=()), stub_start=None, native_fragments=(),
        splice_applied=False, content_hash=sha256_id(n.encode()), vaddrs=(0x1000 + 4 * i,))
        for i, n in enumerate(names)}
    by_uid = {unit.uid: unit for unit in units.values()}
    snapshot = SimpleNamespace(snapshot_id=sha256_id(b"hot world"),
                               units=lambda: tuple(units.values()), unit=by_uid.__getitem__)
    bindings = {}
    for n, unit in units.items():
        lookup = n == "a"
        callee = SimpleNamespace(
            path="/exact/to/qemu", elf_address=0x5000, image_offset=0x5000, image_id=_IMAGE_ID,
            code_hash=sha256_id(b"\xc3"), size=1, code=b"\xc3", role="qemu", abi="sysv-amd64",
            symbol="helper_lookup_tb_ptr" if lookup else "helper_v7m_msr",
            frame_chain=(SimpleNamespace(code=bytes(_LOOKUP_GRAPH_BYTES - 1)),) if lookup else (),
            runtime_address=0x9000, tcg_typemask=2)
        call = HostCallSite(
            uid=unit.uid, host_off=0, original_bytes=b"\xe8\0\0\0\0", encoding="rel32",
            continuation_off=5, insn_idx=0, guest_pcs=unit.vaddrs, opmark_off=0, helper_id=7,
            helper_name=callee.symbol[7:], helper_flags=5, target_abs=0x9000, pool_slot_off=None,
            module="qemu", symbol=callee.symbol, call_class="pure", recovery_return_off=5, reason=None)
        bindings[unit.uid] = (NativeCallBinding(call, callee, None, _NATIVE_INPUT),)
    counts = tmp_path / "helper-calls.json"
    counts.write_text(json.dumps({
        "schema": "scopt.helper-call-profile/1", "complete": True,
        "snapshot_id": snapshot.snapshot_id,
        "counts": [{"unit": u.uid, "call_off": 0, "count": 5000} for u in units.values()]}))
    profile = _fake_view(snapshot, [(f"tb:{s}", f"tb:{d}", k, c) for s, d, k, c in _hot_edges()])

    def analysis(name, scope, min_version=1):
        if name == "envlayout":
            return SimpleNamespace(value={"bound": True})
        assert name == "native-callees" and min_version == 2
        return SimpleNamespace(value=bindings[scope.id])

    def stub_lowering(self, unit, request, code_address, binding, params, growth_room):
        # Selection is what is under test: every site that reaches lowering
        # stops here with a recognizable reason.
        raise NativeInlineError("reached_lowering", binding.callee.symbol)

    monkeypatch.setattr(host_inline, "prepare_native_images",
                        lambda *args, **kwargs: AnalysisInput(_NATIVE_INPUT, ()))
    monkeypatch.setattr(host_inline, "InlineCall", lambda **fields: SimpleNamespace(**fields))
    monkeypatch.setattr(HostInlineGraph, "_prepare_request", stub_lowering)
    view = SimpleNamespace(header=SimpleNamespace(version=7, reserved=0, code_address=0x7fff0000),
                           working_set=SimpleNamespace(base=snapshot, is_pinned=lambda uid: False),
                           units=lambda: tuple(units.values()),
                           unit=snapshot.unit)
    site_profile = {"site_profile_path": str(counts),
                    "site_profile_sha256": sha256_id(counts.read_bytes())}

    def run(params):
        ctx = SimpleNamespace(params={**params, **site_profile}, deadline=None,
                              runtime={"supercache_flat": False}, snapshot_id=snapshot.snapshot_id,
                              analysis=analysis, store=SimpleNamespace(set_input=lambda *a: None),
                              profile=lambda phase: profile)
        result = host_inline.HostInline().run(view, ctx, SimpleNamespace())
        detail = json.loads(result.detail) if result.detail.startswith("{") else result.detail
        outcomes = ({row["unit"][3:]: row["outcome"] for row in detail["sites"]}
                    if isinstance(detail, dict) else None)
        return result, detail, outcomes
    return run


def test_hot_loop_units_firmware_kinds_follow_thread_mode_exit_null():
    units = {"a": SimpleNamespace(key=SimpleNamespace(cs_base=0)),
             "b": SimpleNamespace(key=SimpleNamespace(cs_base=0)),
             "isr": SimpleNamespace(key=SimpleNamespace(cs_base=1)),
             "c": SimpleNamespace(key=SimpleNamespace(cs_base=0)),
             "d": SimpleNamespace(key=SimpleNamespace(cs_base=0)),
             "e": SimpleNamespace(key=SimpleNamespace(cs_base=0))}
    edges = [dict(src=s, dst=d, kind=k, count=c, flags=(), status="exact")
             for s, d, k, c in _hot_edges()]
    assert host_inline.hot_loop_units(edges, "transfer", units.get) == {"d", "e"}
    assert host_inline.hot_loop_units(edges, "firmware", units.get) == {"a", "b", "d", "e"}
    # The firmware kinds are firmware hot-loop's, with the same exit_null filter.
    from scopt.analysis.helper_hot_loop import FIRMWARE_LOOP_KINDS, observed_loop_units, real_exit_null

    assert host_inline.hot_loop_units(edges, "firmware", units.get) == observed_loop_units(
        edges, kinds=FIRMWARE_LOOP_KINDS, accept=real_exit_null(units.get))
    assert host_inline.hot_loop_units(edges, "transfer", units.get) == observed_loop_units(edges)
    with pytest.raises(ValueError):
        host_inline.hot_loop_units(edges, "all", units.get)


def test_hot_loop_kinds_defaults_to_transfer_and_keeps_sealed_pgo_7(hot_world):
    from scopt.canon import canonical_json

    assert host_inline._DEFAULTS["hot_loop_kinds"] == "transfer"
    assert host_inline.HostInline.descriptor.params_schema["hot_loop_kinds"] is str
    for name in ("sealed-pgo-7", "sealed-pgo-7-b3"):
        entry = load_pipeline(PIPELINES_DIR / f"{name}.toml").passes[0]
        assert (entry.name, dict(entry.params)) == ("host-inline", _PGO7_HOST_PARAMS)
        assert sha256_id(canonical_json(dict(entry.params))) == _PGO7_HOST_PARAMS_SHA256
    result, detail, outcomes = hot_world(_PGO7_HOST_PARAMS)
    # Receipt and stats as before the parameter: transfer cycles only, so the
    # critical-section cycle a <-> b stays outside_observed_loop.
    assert result.reason == "no_eligible_native_calls"
    assert set(detail) == {"native_images_id", "site_profile_sha256", "ranking", "sites"}
    assert detail["ranking"] == "measured_site_calls_in_observed_loops"
    assert outcomes == {"a": "outside_observed_loop", "b": "outside_observed_loop",
                        "isr": "outside_observed_loop", "c": "outside_observed_loop",
                        "d": "reached_lowering", "e": "reached_lowering"}
    assert dict(result.stats) == {"sites_seen": 6, "sites_applied": 0, "emitted_growth_bytes": 0,
                                  "selected_tb_entries_proxy": 0, "growth_policy_unbounded": 1,
                                  "site_policy_unbounded": 1, "sites:outside_observed_loop": 4,
                                  "sites:reached_lowering": 2, "loop_units": 2}
    explicit = hot_world({**_PGO7_HOST_PARAMS, "hot_loop_kinds": "transfer"})
    assert explicit[1] == detail and dict(explicit[0].stats) == dict(result.stats)


def test_hot_loop_kinds_firmware_reaches_critical_section_sites_under_the_graph_cap(hot_world):
    result, detail, outcomes = hot_world(_HOTGRAPH_PARAMS)
    assert result.reason == "no_eligible_native_calls" and detail["hot_loop_kinds"] == "firmware"
    # b's helper sits in a cycle closed by a thread-mode exit_null and is now
    # hot; a's is helper_lookup_tb_ptr, whose 50,345-byte graph the 32768-byte
    # cap keeps out; the handler-mode exit_null never closes isr <-> c.
    assert outcomes == {"a": "helper_graph_size_policy", "b": "reached_lowering",
                        "isr": "outside_observed_loop", "c": "outside_observed_loop",
                        "d": "reached_lowering", "e": "reached_lowering"}
    assert result.stats["loop_units"] == 4
    # Without the cap the lookup graph would be lowered too (the full-graph
    # path, not the selected-root path).
    _, _, uncapped = hot_world({k: v for k, v in _HOTGRAPH_PARAMS.items()
                                if k != "trusted_max_source_graph_bytes"})
    assert uncapped["a"] == "reached_lowering"
    # transfer kinds on the same graph policy: only the d <-> e cycle.
    _, detail, outcomes = hot_world({**_HOTGRAPH_PARAMS, "hot_loop_kinds": "transfer"})
    assert "hot_loop_kinds" not in detail
    assert {n for n, o in outcomes.items() if o != "outside_observed_loop"} == {"d", "e"}


def test_hot_loop_only_no_longer_requires_selected_root(hot_world):
    params = {k: v for k, v in _PGO7_HOST_PARAMS.items() if k != "trusted_selected_root"}
    result, _, outcomes = hot_world(params)
    assert result.outcome is Outcome.NO_CHANGE and outcomes["d"] == "reached_lowering"
    # It still needs trusted, frequency-ranked, exact-count helpers.
    for bad in ({"trusted_rank_by": "all"}, {"trusted_allow_tb_entry_proxy": True},
                {"trusted_all_helpers": False, "trusted_rank_by": "all"}):
        result, detail, _ = hot_world({**params, **bad})
        assert (result.outcome, result.reason) == (Outcome.FAILED, "invalid_params"), bad


@pytest.mark.parametrize("value", ["loops", "", "Firmware", 1, None])
def test_invalid_hot_loop_kinds_is_invalid_params(hot_world, value):
    result, detail, _ = hot_world({**_HOTGRAPH_PARAMS, "hot_loop_kinds": value})
    assert (result.outcome, result.reason) == (Outcome.FAILED, "invalid_params")
    assert "hot_loop_kinds" in detail
    from scopt.manager import _check_params

    if not isinstance(value, str):
        assert "hot_loop_kinds" in _check_params(host_inline.HostInline.descriptor,
                                                 {"hot_loop_kinds": value})
