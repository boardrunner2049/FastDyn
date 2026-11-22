import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

import pytest

from path_counts import summarize


def test_guard_paths_distinguish_unlinked_matches():
    counts = dict(entry=79, match=70, reset=5, fallback=14)
    observed = summarize({"kind": "SPEC", "points": counts}, counts)
    assert observed == dict(predicate_matched=70, predicate_missed=9, matched_unlinked=5,
                            matched_linked=65, lookup_fallback=14)
    counts["fallback"] = 9
    assert summarize({"kind": "GENERAL", "points": counts}, counts)["lookup_fallback"] == 9


def test_refuse_bad_udiv_conservation():
    counts = dict(entry=7, fast=6, fallback=0)
    with pytest.raises(ValueError, match="conserve"):
        summarize({"kind": "udiv", "points": counts}, counts)
    counts["fallback"] = 1
    assert summarize({"kind": "udiv", "points": counts}, counts)["nonzero_inline"] == 6


@pytest.mark.skipif(platform.machine() != "x86_64" or not shutil.which("gdb") or not shutil.which("cc"),
                    reason="needs native x86-64 GDB, compiler and hardware instruction breakpoints")
def test_native_debugger_counts_taken_fallthrough_and_unobserved_sites(tmp_path):
    src = tmp_path / "branch.s"
    src.write_text("""
.global _start
_start:
    mov $7,%ecx
entry:
    test %ecx,%ecx
    je done
loop_body:
    dec %ecx
    jmp entry
unreached:
    nop
done:
    xor %edi,%edi
    mov $60,%eax
    syscall
""")
    binary = tmp_path / "branch"
    subprocess.run(["cc", "-nostdlib", "-no-pie", str(src), "-o", str(binary)], check=True)
    symbols = {}
    for row in subprocess.check_output(["nm", str(binary)], text=True).splitlines():
        address, _, name = row.split()
        symbols[name] = int(address, 16)
    prefixes = dict(entry="85c9", loop_body="ffc9", unreached="90", done="31ff")
    points = {name: dict(offset=symbols[name], expected_hex=prefix) for name, prefix in prefixes.items()}
    plan = tmp_path / "points.json"
    plan.write_text(json.dumps(dict(base=0, points=points)))
    cmd = ["gdb", "-q", "-nx", "-batch", "-x", str(Path(__file__).with_name("path_counts.gdb")),
           "--args", str(binary)]
    subprocess.run(cmd, check=True, env={**os.environ, "PYE_PATH_COUNTER_PLAN": str(plan)}, timeout=30)
    observed = json.loads((tmp_path / "counts.json").read_text())
    assert observed["exit_code"] == 0 and observed["errors"] == []
    assert observed["counts"] == dict(entry=8, loop_body=7, unreached=0, done=1)
    assert observed["checked"] == {key: prefix for key, prefix in prefixes.items() if key != "unreached"}
