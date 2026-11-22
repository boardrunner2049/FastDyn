import subprocess
import sys
import tomllib
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "experiments/px4_memset_layout/make_cache_config.py"
)


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
