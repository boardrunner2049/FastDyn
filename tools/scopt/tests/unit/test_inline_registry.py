"""One discoverable inliner, with the former names usable by existing callers."""

import pytest

from scopt.manager import PipelineError, default_pass_registry, load_pipeline


def test_one_registered_inliner_and_legacy_pipeline_defaults():
    reg = default_pass_registry()
    assert [n for n in reg.names() if n.startswith("host-inline")] == ["host-inline"]
    assert not reg.versions("host-inline-unknown")
    assert reg.versions("host-inline") == reg.versions("host-inline-splice") == (1,)
    for legacy, mode in (("host-inline-splice", "splice"), ("host-inline-extreme", "extreme")):
        entry, = load_pipeline(legacy).passes
        assert entry.name == "host-inline"
        assert entry.params["mode"] == mode and entry.params["allow_faulting"] is True
        # An old custom TOML can still explicitly name either backend.
        assert reg.get(legacy, 1).descriptor.name == legacy


def test_aliases_for_controls_and_parameter_files():
    pl = load_pipeline("native-program")
    alias = "host-inline-splice"
    overrides = {alias: {"max_sites": 13}, "host-inline": {"min_tb_entries": 2}}
    assert pl.resolve_pass_options((alias,), (), alias, overrides) == (
        ("host-inline",), (), "host-inline", {"host-inline": {"max_sites": 13, "min_tb_entries": 2}})
    with pytest.raises(PipelineError, match="conflicting overrides"):
        pl.resolve_pass_options((), (), None, {alias: {"max_sites": 7}, "host-inline": {"max_sites": 13}})
    enabled, disabled, only, _ = pl.resolve_pass_options((alias,), ("host-inline",), None, None)
    with pytest.raises(PipelineError, match="both enabled and disabled"):
        pl.validate_pass_options(enabled, disabled, only)
    assert pl.resolve_pass_options((), (), "host-inline-extreme", None)[2] == "host-inline-extreme"


def test_custom_pipeline_with_multiple_legacy_entries_preserves_independent_options():
    names = ("host-inline", "host-inline-splice", "host-inline-extreme")
    pl = load_pipeline({"name": "old-pipeline", "allowed_capabilities": ["INLINE_CALL"],
                        "pass": [{"name": n, "version": 1} for n in names]})
    params = {n: {"max_sites": i} for i, n in enumerate(names)}
    assert pl.resolve_pass_options((), names[:1], names[-1], params) == ((), names[:1], names[-1], params)
