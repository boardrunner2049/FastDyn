"""Sealed B3 (configuration 6 + B3) on a compiled sealed launch.

python sealed_b3.py path/to/compiled/launch.json fresh/output/dir [--baseline BASE.json]

The launch must select a sealed exact-PC cache carrying a B3 semantic
extension (``scopt optimize SNAP --pipeline b3`` with a ``profile run
--sealed`` steady profile; ``docs/b3-passes.md``). Each arm runs on a private
copy of the same cache bytes with QEMU_SC_SEALED=1 and QEMU_SC_TRUSTED=1:

``sc6``            default: sealed lookups activate the extension.
``no-sealed-sc6``  QEMU_SC_V7_EXPERIMENT=no-sealed-sc6 (old sealed behaviour).

Both must print the same guest output, and the same output as the optional
``--baseline`` launch (configuration 6 without B3). The default arm must
activate, arm every SPEC link it can, never violate the disarm invariant and
unlink slots whenever a TLB/MPU change cleared live slots; the token arm must
activate nothing. Writes OUT/sealed-b3.json with the v6 stats of each arm.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess

from scopt.formats.sc_v5 import sparse_copy
from scopt.formats.sc_v6 import read_sc6
from scopt.run.helper_pgo import _guest_lines, _launch

ARMS = (("sc6", {}),
        ("no-sealed-sc6", {"QEMU_SC_V7_EXPERIMENT": "no-sealed-sc6"}))
KEYS = ("loaded", "sealed", "sealed_activation", "n_variants", "n_slots", "n_links",
        "activations", "links_armed", "refused", "variants_built", "tlb_hook_calls",
        "disarm_tlb_events", "slots_unlinked", "variant_slots_unlinked",
        "invariant_violations", "forbid_requests", "forbidden", "forbid_reason")


def source_cache(launch):
    accel = launch["argv"][launch["argv"].index("-accel") + 1]
    source = next(part.split("=", 1)[1] for part in accel.split(",")
                  if part.startswith("supercache="))
    return Path(launch["cwd"]) / source


def run(launch, rd, source, env_extra):
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    argv, env, _ = _launch(launch, rd, cache, trusted=True)
    env = {k: v for k, v in env.items() if k != "QEMU_SC_V7_EXPERIMENT"}
    env.update(env_extra)
    env["QEMU_SC_V6_STATS"] = str(rd / "v6-stats.json")
    with (rd / "qemu.log").open("w") as stream:
        result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                                cwd=launch["cwd"], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=300)
    log = (rd / "qemu.log").read_text(errors="replace")
    assert result.returncode == 0, (rd.name, log[-2000:])
    assert "sealed supercache miss" not in log, rd.name
    return _guest_lines(launch, log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--baseline", type=Path,
                        help="compiled launch of the same sealed capture without B3")
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    source = source_cache(launch)
    f = read_sc6(source)
    assert f.has_ext and f.problem is None and f.ext is not None, \
        f"{source} carries no loadable B3 extension ({f.problem})"
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)

    expected, stats = None, {}
    if args.baseline:
        base = json.loads(args.baseline.read_text())
        expected = run(base, out / "baseline", source_cache(base), {})
    for arm, env_extra in ARMS:
        observed = run(launch, out / arm, source, env_extra)
        expected = observed if expected is None else expected
        assert observed == expected, arm
        stats[arm] = json.loads((out / arm / "v6-stats.json").read_text())

    on, off = stats["sc6"], stats["no-sealed-sc6"]
    for s in (on, off):
        assert s["loaded"] is True and s["sealed"] is True
        assert s["invariant_violations"] == 0
    assert on["sealed_activation"] is True and off["sealed_activation"] is False
    assert on["activations"] > 0
    if f.ext.links:
        assert on["links_armed"] > 0, on["refused"]
    if on["disarm_tlb_events"]:
        assert on["slots_unlinked"] + on["variant_slots_unlinked"] > 0
    assert off["activations"] == 0 and off["links_armed"] == 0
    assert off["variants_built"] == 0 and off["disarm_tlb_events"] == 0

    (out / "sealed-b3.json").write_text(json.dumps(
        {arm: {k: s.get(k) for k in KEYS} for arm, s in stats.items()}, indent=2) + "\n")


if __name__ == "__main__":
    main()
