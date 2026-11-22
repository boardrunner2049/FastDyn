import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "qemu-prof.sh"


def run_wrapper(stop=None):
    env = dict(
        os.environ,
        TREBUCHET_QEMU="/bin/echo",
        FBPROF_PLUGIN="/tmp/libtbprof.so",
        FBPROF_OUT="/tmp/mission.tbprof",
    )
    if stop is not None:
        env["FBPROF_STOP"] = stop
    return subprocess.run(
        [SCRIPT, "-M", "cortexm"], env=env, check=True, text=True,
        capture_output=True,
    ).stdout.strip()


def test_profile_wrapper_can_flush_on_ordinary_exit():
    output = run_wrapper()
    assert output == (
        "-M cortexm -plugin "
        "/tmp/libtbprof.so,out=/tmp/mission.tbprof"
    )


def test_profile_wrapper_retains_guest_stop_mode():
    output = run_wrapper("0x8021234")
    assert output.endswith(
        "-plugin /tmp/libtbprof.so,out=/tmp/mission.tbprof,stop=0x8021234"
    )
