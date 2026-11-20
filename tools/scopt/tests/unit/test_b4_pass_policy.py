"""Exercise the public B4 packager through the same command used for replay."""
import json

import pytest
from click.testing import CliRunner

from scopt.cli import cli, load_commands
from scopt.formats import envlayout_v1
from tests.synth.rich_tb import rich_tb, write_rich_pair


@pytest.fixture
def b4_package(tmp_path):
    load_commands()
    source = tmp_path / "capture"
    source.mkdir()
    write_rich_pair([rich_tb(64)], str(source))
    snapshots = tmp_path / "imported"
    runner = CliRunner()
    imported = runner.invoke(cli, ["import", str(source), "--store", str(snapshots)])
    assert imported.exit_code == 0, imported.output
    snapshot, = (snapshots / "snapshots").iterdir()
    store = tmp_path / "optimized"
    return runner, ["b4", "package", str(snapshot), "--store", str(store)], store


@pytest.mark.parametrize("options", [[], ["--classes", "D2", "--coalesce-nops", "--absorb-nops"]])
def test_b4_packager_skips_disabled_pass(b4_package, monkeypatch, options):
    runner, command, store = b4_package
    monkeypatch.setattr(envlayout_v1, "find_envlayout", lambda *_: pytest.fail("disabled pass requested a layout"))
    result = runner.invoke(cli, [*command, *options])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert (doc["status"], doc["reason"]) == ("no_change", "disabled_by_default")
    assert doc["proposal"]["units"] == 0 and doc["ledger"][0]["counts"]["edits"] == 0
    assert "artifact_id" not in doc and not store.exists()


def test_b4_packager_requires_layout_after_explicit_opt_in(b4_package, monkeypatch):
    runner, command, _ = b4_package
    monkeypatch.setattr(envlayout_v1, "find_envlayout", lambda *_: None)
    result = runner.invoke(cli, [*command, "--enable", "eliminate-state-sync"])
    assert result.exit_code == 2 and "guard_layout_unbound" in result.output
