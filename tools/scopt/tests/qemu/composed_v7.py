"""Composed v7 (sealed firmware regions + B3) on a compiled sealed launch.

python composed_v7.py path/to/compiled/launch.json fresh/output/dir [--baseline BASE.json]

The launch must select a composed canonical v7 cache (``scopt optimize SNAP
--pipeline sealed-pgo-7-b3`` or another ``sealed-*-b3`` pipeline;
``docs/b3-passes.md``).  Every run uses a private copy of the cache with
QEMU_SC_SEALED=1 and QEMU_SC_TRUSTED=1.

``composed``  the artifact as published: guest output equals the optional
              ``--baseline`` launch (configuration 7 without B3), the v7 stats
              report ``sc6_composed`` and every region loaded, and the v6
              stats show activation without invariant violations.  With member
              skip flags (counters revision 4) the armed and skipped residual
              slots match the directory: equal to the static sums when every
              region was built once, else positive and bounded by them.

Then one fail-loud mutant per loader-facing format rule.  Each must exit
non-zero with the stated loader message and print no guest line:

``bit30_alone``          header reserved = 1 << 30              unknown feature bits
``safe_directory``       composed directory ext_version 2       sc6 composition requires a trusted directory
``arrays_limit``         sc6 arrays reach the 0xFF00 header     ext table too large
``variant_overlap``      variant 0 moved onto region 0          arena spans overlap
``member_flag_bit2``     a member's flags |= 4                  invalid source, region, or flags
``member_skip_missing``  a required skip bit cleared            skip flags
``member_skip_extra``    an original slot's skip bit set        skip flags
``member_skip_legacy``   skip bit 0 on a legacy-region member   skip flags

The ``member_skip_*`` mutants run only when the directory has such a member.
Trusted input skips the sc6 and directory checksums, which is what lets the
last mutants reach the size, arena and member checks.  Writes
OUT/composed-v7.json.
"""

import argparse
import json
import os
from pathlib import Path
import struct
import subprocess

from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_v5 import ScHeader, read_sc, sparse_copy
from scopt.formats.sc_v6 import ARRAYS_OFFSET, EXT_OFFSET, read_sc6
from scopt.formats.sc_v7 import SC7_COMPOSED_DIR_OFFSET, SC7_FEATURE_SC6_REGIONS, \
    SC7_REGION_MEMBER_DESCRIPTORS, _HEADER, _MEMBER, _REGION, decode_directory, read_sc7
from scopt.link import residual_goto_tb_slots
from scopt.run.helper_pgo import _guest_lines, _launch
from scopt.run.v7_stats import MEMBER_SKIP_REVISION, counter_problems

V6_KEYS = ("loaded", "sealed", "sealed_activation", "activations", "links_armed",
           "variants_built", "tlb_hook_calls", "slots_unlinked", "invariant_violations")
V7_KEYS = ("loaded", "mode", "sc6_composed", "regions_loaded", "members_loaded",
           "region_activations", "trusted_region_activations", "trusted_residual_slots_armed",
           "trusted_residual_slots_skipped", "counters_revision")


def source_cache(launch):
    accel = launch["argv"][launch["argv"].index("-accel") + 1]
    source = next(part.split("=", 1)[1] for part in accel.split(",")
                  if part.startswith("supercache="))
    return Path(launch["cwd"]) / source


def launch_run(launch, rd, source, mutate=None):
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    if mutate is not None:
        with cache.open("r+b") as stream:
            mutate(stream)
    argv, env, _ = _launch(launch, rd, cache, trusted=True)
    env = {k: v for k, v in env.items() if k != "QEMU_SC_V7_EXPERIMENT"}
    env["QEMU_SC_V6_STATS"] = str(rd / "v6-stats.json")
    env["QEMU_SC_V7_STATS"] = str(rd / "v7-stats.json")
    with (rd / "qemu.log").open("w") as stream:
        result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                                cwd=launch["cwd"], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=300)
    return result.returncode, (rd / "qemu.log").read_text(errors="replace")


def _set_reserved(reserved):
    def mutate(stream):
        stream.seek(0)
        header = ScHeader.from_bytes(stream.read(80))
        stream.seek(0)
        stream.write(header.replace(reserved=reserved).with_checksum().to_bytes())
    return mutate


def _safe_directory(stream):
    stream.seek(SC7_COMPOSED_DIR_OFFSET + 8)
    stream.write(struct.pack("<I", 2))


def _arrays_limit(stream):
    # n_slots so that ARRAYS_OFFSET + arrays reaches past 0xFF00 but not 0x10000.
    stream.seek(EXT_OFFSET + 16)
    nv, ns, nl = struct.unpack("<III", stream.read(12))
    room = SC7_COMPOSED_DIR_OFFSET - ARRAYS_OFFSET - 64 * nv - 16 * nl
    stream.seek(EXT_OFFSET + 20)
    stream.write(struct.pack("<I", room // 32 + 1))


def _variant_overlap(directory, semantic):
    region = decode_directory(directory)["regions"][0]
    v = semantic.ext.variants[0]
    tb = region["start_offset"]                       # 64-aligned
    tc = tb + (v.tc_offset - v.tb_offset)
    end = tc + (v.end_offset - v.tc_offset)
    others = max((w.end_offset for w in semantic.ext.variants[1:]), default=0)

    def mutate(stream):
        stream.seek(ARRAYS_OFFSET + 8)                 # variant 0: tb, tc, size, end
        stream.write(struct.pack("<QQQQ", tb, tc, v.tc_size, end))
        stream.seek(EXT_OFFSET + 40)                   # variants_max_end
        stream.write(struct.pack("<Q", max(end, others)))
    return mutate


def static_slots(source, directory):
    """``(chainable, skipped)`` residual slots over all member-descriptor
    regions, from the file: what QEMU arms and skips when every region is
    built once."""
    sc = read_sc(source, versions=(7,))
    by_tb = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    rows = decode_directory(directory)
    chainable = skipped = 0
    with open(source, "rb") as stream:
        for region in rows["regions"]:
            if not region["flags"] & SC7_REGION_MEMBER_DESCRIPTORS:
                continue
            lo = region["first_member"]
            for gi in range(lo, lo + region["n_members"]):
                member = rows["members"][gi]
                stream.seek(SC_CODE_OFFSET + member["tc_offset"])
                code = stream.read(member["tc_size"])
                internal = {e["field_offset"] for e in rows["edges"] if e["source_member"] == gi}
                count, problems = residual_goto_tb_slots(
                    by_tb[member["source_tb_offset"]], code, member["tc_offset"],
                    internal_fields=internal, code_address=sc.header.code_address,
                    skip_slots=member["flags"])
                assert not problems, (gi, problems)
                chainable += count
                skipped += bin(member["flags"]).count("1")
    return chainable, skipped


def _member_flags(member, flags):
    """Overwrite one member's flags in the composed directory table (trusted
    input skips the directory checksum, so QEMU reaches member validation)."""
    def mutate(stream):
        stream.seek(SC7_COMPOSED_DIR_OFFSET)
        fields = _HEADER.unpack(stream.read(_HEADER.size))
        n_regions, table = fields[3], fields[7]
        stream.seek(SC_CODE_OFFSET + table + _REGION.size * n_regions +
                    _MEMBER.size * member + _MEMBER.size - 4)
        stream.write(struct.pack("<I", flags))
    return mutate


def member_mutants(source, directory):
    """The ``member_*`` mutants this directory supports: (name, mutate, needle)."""
    sc = read_sc(source, versions=(7,))
    by_tb = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    rows = decode_directory(directory)
    region_of = {}
    for region in rows["regions"]:
        for gi in range(region["first_member"], region["first_member"] + region["n_members"]):
            region_of[gi] = region
    out = [("member_flag_bit2", _member_flags(0, rows["members"][0]["flags"] | 4),
            "invalid source, region, or flags")]
    flagged = next((gi for gi, m in enumerate(rows["members"]) if m["flags"]), None)
    if flagged is not None:
        flags = rows["members"][flagged]["flags"]
        out.append(("member_skip_missing", _member_flags(flagged, flags & (flags - 1)),
                    "skip flags"))
    for gi, member in enumerate(rows["members"]):
        if not region_of[gi]["flags"] & SC7_REGION_MEMBER_DESCRIPTORS:
            continue
        record = by_tb[member["source_tb_offset"]]
        extra = [n for n in (0, 1) if record.jmp_reset_offset[n] != 0xFFFF and
                 not member["flags"] >> n & 1]
        if extra:
            out.append(("member_skip_extra",
                        _member_flags(gi, member["flags"] | 1 << extra[0]), "skip flags"))
            break
    legacy = next((gi for gi in range(len(rows["members"]))
                   if not region_of[gi]["flags"] & SC7_REGION_MEMBER_DESCRIPTORS), None)
    if legacy is not None:
        out.append(("member_skip_legacy", _member_flags(legacy, 1), "skip flags"))
    return tuple(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--baseline", type=Path,
                        help="compiled launch of the same sealed capture without B3 (config 7)")
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    source = source_cache(launch)
    semantic = read_sc6(source)
    assert semantic.is_composed and semantic.problem is None, \
        f"{source} is not a loadable composed v7 cache ({semantic.problem})"
    directory = read_sc7(source)
    assert directory.composed and directory.trusted
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    doc = {}

    rc, log = launch_run(launch, out / "composed", source)
    assert rc == 0, log[-2000:]
    assert "sealed supercache miss" not in log
    observed = _guest_lines(launch, log)
    if args.baseline:
        base = json.loads(args.baseline.read_text())
        rc, base_log = launch_run(base, out / "baseline", source_cache(base))
        assert rc == 0, base_log[-2000:]
        assert observed == _guest_lines(base, base_log), "guest output differs from config 7"
    v6 = json.loads((out / "composed" / "v6-stats.json").read_text())
    v7 = json.loads((out / "composed" / "v7-stats.json").read_text())
    assert v6["loaded"] is True and v6["sealed_activation"] is True
    assert v6["invariant_violations"] == 0 and v6["activations"] > 0
    assert v7["loaded"] is True and v7["mode"] == "trusted" and v7["sc6_composed"] is True
    assert v7["regions_loaded"] == directory.n_regions
    assert counter_problems(v7) == [], counter_problems(v7)
    chainable, skipped = static_slots(source, directory)
    if skipped:
        # Flags load only on a QEMU with member skip flags (revision 4).
        assert v7["counters_revision"] >= MEMBER_SKIP_REVISION
        armed, seen = v7["trusted_residual_slots_armed"], v7["trusted_residual_slots_skipped"]
        if v7["trusted_region_activations"] == v7["regions_loaded"]:
            assert (armed, seen) == (chainable, skipped), (armed, seen, chainable, skipped)
        else:
            assert 0 < seen <= skipped and armed <= chainable, (armed, seen, chainable, skipped)
            assert armed > 0 or not chainable
    doc["composed"] = {"v6": {k: v6.get(k) for k in V6_KEYS},
                       "v7": {k: v7.get(k) for k in V7_KEYS},
                       "static": {"chainable_residual_slots": chainable,
                                  "skipped_b3_slots": skipped}}

    reserved = semantic.sc.header.reserved
    mutants = (
        ("bit30_alone", _set_reserved(SC7_FEATURE_SC6_REGIONS), "unknown feature bits"),
        ("safe_directory", _safe_directory, "sc6 composition requires a trusted directory"),
        ("arrays_limit", _arrays_limit, "ext table too large"),
    )
    if semantic.ext.variants:
        mutants += (("variant_overlap", _variant_overlap(directory, semantic),
                     "arena spans overlap"),)
    mutants += member_mutants(source, directory)
    doc["mutants"] = {}
    for name, mutate, needle in mutants:
        rc, log = launch_run(launch, out / name, source, mutate)
        guest = [line for line in log.splitlines() if line.startswith("Reg ")]
        assert rc != 0 and needle in log and not guest, (name, log[-2000:])
        doc["mutants"][name] = {"exit": rc, "message": needle}
    doc["reserved"] = hex(reserved)
    (out / "composed-v7.json").write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    main()
