import json
import subprocess
import sys
import tomllib
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "experiments/px4_memset_layout/make_cache_config.py"
)
EXPERIMENT = SCRIPT.parent


def test_opportunity_ledger_has_complete_canonical_ids_and_current_pass_path():
    ledger = json.loads((EXPERIMENT / "opportunity-ledger.json").read_text())
    entries = ledger["entries"]
    ids = [entry["id"] for entry in entries]
    expected = [f"A{i:02d}" for i in range(1, 36)] + [
        "E01a", "E01b", "E02", "E03", "E04"
    ]
    assert ids == expected

    sources = {
        source
        for entry in entries
        for source in entry["source_locations"]
    }
    assert "firmbolt/tools/preflight/bolt/FirmBOLTFirmwarePasses.cpp" not in sources
    assert "firmbolt/native/lib/Passes/FirmBOLTFirmwarePasses.cpp" in sources


def test_cache_config_freezes_fixed_unpacked_fmu(tmp_path):
    base = tmp_path / "base.toml"
    output = tmp_path / "cache.toml"
    unpacked = tmp_path / "unpacked"
    base.write_text(
        """[Machine]
platform = "STM32H743x"

[FMU]
active = "quad"
auto_build = true

[FMU.models.quad]
output = "out/fmi3/quad"
build = true
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(base),
            "--output",
            str(output),
            "--unpacked-fmu",
            str(unpacked),
        ],
        check=True,
    )
    rendered = tomllib.loads(output.read_text(encoding="utf-8"))
    assert rendered["Machine"] == {
        "supercache": "cache/px4.sc",
        "supercache_flat": False,
        "platform": "STM32H743x",
    }
    assert rendered["FMU"]["auto_build"] is False
    assert rendered["FMU"]["models"]["quad"]["build"] is False
    assert rendered["FMU"]["models"]["quad"]["output"] == str(unpacked.resolve())
