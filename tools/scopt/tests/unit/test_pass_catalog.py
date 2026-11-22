from pathlib import Path

from scopt.manager import DEFAULT_DISABLED_PASSES, default_pass_registry


CATALOG = Path(__file__).parents[2] / "docs" / "passes.md"


def test_catalog_has_one_section_for_every_registered_pass():
    text = CATALOG.read_text(encoding="utf-8")
    registry = default_pass_registry()
    registered = sorted(registry.names())

    missing = [name for name in registered if f"## `{name}@" not in text]
    assert missing == []
    assert text.count("\n## `") == len(registered)


def test_catalog_names_every_default_disabled_pass():
    text = CATALOG.read_text(encoding="utf-8")
    disabled = sorted(DEFAULT_DISABLED_PASSES)

    assert disabled == [
        "chain-returns",
        "chain-static",
        "eliminate-state-sync",
        "local-scalar-cleanup",
        "retire-ordinary-exits",
        "specialize-edges",
        "specialize-returns",
    ]
    for name in disabled:
        assert name in text.partition("## Running one pass")[2].partition("## Pass summary")[0]
