"""Chainable trusted residual exits on a sealed member-descriptor launch.

python trusted_exit_chain.py path/to/compiled/launch.json fresh/output/dir

The launch must select a trusted firmware-region artifact written with per-
member descriptors (region flag SC7_REGION_MEMBER_DESCRIPTORS, for example a
rebuilt packet config 7, or a composed c7b3 whose members carry skip flags).
Each arm runs on a private copy of the same cache bytes:

``chain``      default: residual goto_tb exits are armed and chained; a
               trusted region entry destination is entered at its +0 poll
               (counters revision 4).
``no-t2t``     QEMU_SC_V7_EXPERIMENT=no-trusted-to-trusted-chain: the
               revision-3 behaviour, trusted->trusted chains refused.
``unchained``  QEMU_SC_V7_EXPERIMENT=no-trusted-exit-chain (old behaviour).
``skip-irq``   QEMU_SC_SEALED_SKIP_IRQ=1: trusted sources never chain.
``corrupt``    one residual rel32 made nonzero: QEMU must refuse to load.
``bad-poll``   every region's entry poll exit stub (its rax operand)
               corrupted: QEMU loads, marks every region's entry poll
               unverified, refuses every chain into a region entry as
               unverified (fail closed) and prints the same output.
``bad-io``     every region's entry can_do_io store made 0, which the
               loader's prefix check accepts: the same, through the rule
               that a +0 entry must store 1.

The ``bad-*`` arms always run: the load verdicts need no traffic.  They must
refuse at least one trusted->trusted chain only when the ``no-t2t`` arm
refused one.  All arms but ``corrupt`` must print the same guest output, and
on the same bytes ``chain`` must return to the dispatcher less often than
``no-t2t`` (apart from exits that escaped before a chain decision).

Flagged (B3-registered) member slots are never armed.  QEMU adds a region's
armed and skipped slot counts each time it builds the region (lazily, and
again after a tb_flush), so in the ``chain`` and ``no-t2t`` arms
``trusted_residual_slots_armed`` and ``_skipped`` must lie within
``per_build_bounds`` of that arm's ``trusted_region_activations``, its
regions with residual exits and, on a composed load, v6 ``flush_resets``:
exactly the static sums when every region was built once and nothing was
flushed.  With member skip flags the ``chain`` arm must skip some slots,
and the ``unchained`` and ``skip-irq`` arms arm and skip nothing.  Writes
OUT/trusted-exit-chain.json with the per-arm counter summaries.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess

from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_v5 import read_sc, sparse_copy
from scopt.formats.sc_v7 import (SC7_REGION_MEMBER_DESCRIPTORS, decode_directory,
                                 read_sc7)
from scopt.link import residual_goto_tb_slots
from scopt.run.helper_pgo import _guest_lines, _launch
from scopt.run.v7_stats import T2T_REVISION, counter_problems, per_build_bounds, summary

ARMS = (("chain", {}),
        ("no-t2t", {"QEMU_SC_V7_EXPERIMENT": "no-trusted-to-trusted-chain"}),
        ("unchained", {"QEMU_SC_V7_EXPERIMENT": "no-trusted-exit-chain"}),
        ("skip-irq", {"QEMU_SC_SEALED_SKIP_IRQ": "1"}))
#: Entry prefix: mov ebx,[rbp-8]; test ebx,ebx; jl rel32 (poll exit stub).
_POLL = bytes.fromhex("8b5df885db0f8c")
_LEA_RAX = bytes.fromhex("488d05")       # lea rax,[rip+disp32]; jmp tb_ret
_MOVABS_RAX = bytes.fromhex("48b8")      # movabs rax,imm64; jmp tb_ret


def residual_slots(cache):
    """Per region, the residual slots QEMU arms and the member skip bits it
    counts when it builds that region (0 for legacy regions); the decoded
    directory; and (member index, absolute field offset in the file) of every
    armed residual slot."""
    sc = read_sc(cache, versions=(7,))
    rows = decode_directory(read_sc7(cache))
    by_tb = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    out = []
    armed = [0] * len(rows["regions"])
    skipped = [0] * len(rows["regions"])
    with open(cache, "rb") as stream:
        for ri, region in enumerate(rows["regions"]):
            if not region["flags"] & SC7_REGION_MEMBER_DESCRIPTORS:
                continue
            lo = region["first_member"]
            for gi in range(lo, lo + region["n_members"]):
                member = rows["members"][gi]
                record = by_tb[member["source_tb_offset"]]
                stream.seek(SC_CODE_OFFSET + member["tc_offset"])
                code = stream.read(member["tc_size"])
                internal = {e["field_offset"] for e in rows["edges"]
                            if e["source_member"] == gi}
                # Flagged slots are B3-registered: absent from the copy, never armed.
                count, problems = residual_goto_tb_slots(
                    record, code, member["tc_offset"], internal_fields=internal,
                    code_address=sc.header.code_address, skip_slots=member["flags"])
                assert not problems, (gi, problems)
                armed[ri] += count
                skipped[ri] += bin(member["flags"]).count("1")
                for n in (0, 1):
                    insn = record.jmp_insn_offset[n]
                    if (record.jmp_reset_offset[n] != 0xFFFF and insn not in internal and
                            not member["flags"] >> n & 1):
                        out.append((gi, SC_CODE_OFFSET + member["tc_offset"] + insn))
    return (armed, skipped), rows, out


def _entry_code(cache, rows, region):
    member = rows["members"][rows["regions"][region]["first_member"]]
    with open(cache, "rb") as stream:
        stream.seek(SC_CODE_OFFSET + member["tc_offset"])
        return SC_CODE_OFFSET + member["tc_offset"], stream.read(member["tc_size"])


def poll_stub_patch(cache, rows, region):
    """(file offset, bytes) corrupting the rax operand of region's entry-member
    poll exit stub, so it no longer names the entry descriptor."""
    at, code = _entry_code(cache, rows, region)
    assert code[:7] == _POLL, code[:7].hex()
    stub = 11 + int.from_bytes(code[7:11], "little", signed=True)
    if code[stub:stub + 3] == _LEA_RAX:
        return at + stub + 3, b"\x00\x00\x00\x80"
    assert code[stub:stub + 2] == _MOVABS_RAX, code[stub:stub + 10].hex()
    return at + stub + 9, bytes([code[stub + 9] ^ 0x80])


def can_do_io_patch(cache, rows, region):
    """(file offset, bytes) making region's entry-member can_do_io store 0."""
    at, code = _entry_code(cache, rows, region)
    assert code[11:15] == bytes.fromhex("c645fc01"), code[11:15].hex()
    return at + 14, b"\x00"


def run(launch, rd, source, env_extra, patches=()):
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    with cache.open("r+b") as stream:
        for offset, data in patches:
            stream.seek(offset)
            stream.write(data)
    argv, env, _ = _launch(launch, rd, cache)
    env.update(env_extra)
    env["QEMU_SC_V7_STATS"] = str(rd / "stats.json")
    env["QEMU_SC_V6_STATS"] = str(rd / "v6-stats.json")
    with (rd / "qemu.log").open("w") as stream:
        result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                                cwd=launch["cwd"], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=120)
    return cache, result, (rd / "qemu.log").read_text()


def may_rebuild(rd):
    """Whether a trusted region of run ``rd`` may have been built twice.

    Only tb_flush rebuilds a trusted region.  The sc6 side of a composed
    load counts flushes (``flush_resets``); without it, assume it may."""
    try:
        v6 = json.loads((rd / "v6-stats.json").read_text())
    except (OSError, ValueError):
        return True
    return not (v6.get("loaded") is True and v6.get("flush_resets") == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    accel = launch["argv"][launch["argv"].index("-accel") + 1]
    source = next(part.split("=", 1)[1] for part in accel.split(",")
                  if part.startswith("supercache="))
    source = Path(launch["cwd"]) / source
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (armed_static, skips), rows, slots = residual_slots(source)
    assert slots, "launch has no member-descriptor region with residual slots"

    expected, stats = None, {}
    for arm, env_extra in ARMS:
        _, result, log = run(launch, out / arm, source, env_extra)
        assert result.returncode == 0, (arm, log[-2000:])
        observed = _guest_lines(launch, log)
        expected = observed if expected is None else expected
        assert observed == expected, arm
        s = stats[arm] = json.loads((out / arm / "stats.json").read_text())
        assert s["loaded"] is True and s["mode"] == "trusted", arm
        assert counter_problems(s) == [], (arm, counter_problems(s))

    chain, no_t2t = stats["chain"], stats["no-t2t"]
    unchained, skip = stats["unchained"], stats["skip-irq"]
    # Trusted->trusted chains (counters revision 4): every entry prefix of a
    # sealed capture verifies, so nothing is refused for a reason.
    assert chain["counters_revision"] >= T2T_REVISION
    assert chain["trusted_poll_entries_unverified"] == 0
    assert chain["trusted_poll_entries_verified"] == chain["regions_loaded"]
    assert chain["chain_skipped_trusted_to_trusted"] == 0
    assert no_t2t["experiment"]["no_trusted_to_trusted_chain"] is True
    assert no_t2t["chain_allowed_trusted_to_trusted"] == 0
    t2t = no_t2t["chain_skipped_trusted_to_trusted"]
    if t2t:
        # Same bytes: chaining into region entries removes those round trips.
        assert chain["chain_installs_trusted_to_trusted"] > 0
        assert chain["trusted_residual_exits"] < no_t2t["trusted_residual_exits"]
        assert chain["loop_returns_nonnull"] < no_t2t["loop_returns_nonnull"]
        # Exits that never reached a chain decision (an interrupt delivered
        # or a page-spanning destination nulled last_tb) stay unchained.
        escaped = (chain["trusted_residual_exits_chainable"] -
                   chain["chain_allowed_trusted_source"] -
                   chain["chain_skipped_trusted_source"])
        assert 0 <= escaped <= max(50, t2t // 100), escaped
        assert chain["trusted_residual_exits"] <= (
            chain["trusted_residual_slots_armed"] +
            chain["trusted_residual_exits_unchainable"] + escaped)
    assert chain["trusted_residual_slots_armed"] > 0
    assert chain["chain_installs_trusted_source"] > 0
    assert chain["trusted_residual_exits"] < unchained["trusted_residual_exits"]
    # Shadows: one per non-entry member of every activated region.
    activated = {r["region"] for r in unchained["trusted_residual_exit_regions"]}
    assert chain["trusted_member_shadow_descriptors_instantiated"] >= sum(
        rows["regions"][ri]["n_members"] - 1 for ri in activated
        if rows["regions"][ri]["flags"] & SC7_REGION_MEMBER_DESCRIPTORS)
    for s in (unchained, skip):
        assert s["trusted_residual_slots_armed"] == 0
        assert s["chain_installs_trusted_source"] == 0
        assert s["trusted_residual_exits_chainable"] == 0
        assert s["chain_allowed_trusted_to_trusted"] == 0
    # Armed and skipped slots (skips: member skip flags, revision 4) are
    # counted while arming, once per region build; each arm is bounded by its
    # own builds.  Regions with residual exits were built.
    bounds = {}
    for arm in ("chain", "no-t2t"):
        s = stats[arm]
        built = {r["region"] for r in s["trusted_residual_exit_regions"]}
        for key, per_region in (("trusted_residual_slots_armed", armed_static),
                                ("trusted_residual_slots_skipped", skips)):
            low, high = bounds[f"{arm}:{key}"] = per_build_bounds(
                per_region, s["trusted_region_activations"], built=built,
                rebuilt=may_rebuild(out / arm))
            assert low <= s[key] <= high, (arm, key, s[key], low, high)
    if sum(skips):
        assert chain["trusted_residual_slots_skipped"] > 0
        for s in (unchained, skip):
            assert s["trusted_residual_slots_skipped"] == 0

    # A corrupted residual rel32 must be refused before anything runs.
    rd = out / "corrupt"
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    member, field = slots[0]
    with cache.open("r+b") as stream:
        stream.seek(field)
        stream.write(b"\x01\0\0\0")
    argv, env, _ = _launch(launch, rd, cache)
    result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                            cwd=launch["cwd"], env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode != 0
    assert f"trusted member {member} residual slot" in result.stdout + result.stderr

    # Corrupted entry prefixes are refused per region at load and never run:
    # every region is unverified, every trusted->trusted chain is refused as
    # unverified and everything else is unchanged (fail closed, same output).
    # The load verdicts need no trusted->trusted traffic; a refusal is
    # required only when the no-t2t arm refused one.  File offsets come from
    # the pristine source.
    regions = range(len(rows["regions"]))
    bad = {"bad-poll": [poll_stub_patch(source, rows, ri) for ri in regions],
           "bad-io": [can_do_io_patch(source, rows, ri) for ri in regions]}
    for arm, patches in bad.items():
        _, result, log = run(launch, out / arm, source, {}, patches)
        assert result.returncode == 0, (arm, log[-2000:])
        assert _guest_lines(launch, log) == expected, arm
        s = stats[arm] = json.loads((out / arm / "stats.json").read_text())
        assert counter_problems(s) == [], (arm, counter_problems(s))
        assert s["trusted_poll_entries_verified"] == 0, arm
        assert s["trusted_poll_entries_unverified"] == s["regions_loaded"], arm
        assert s["chain_allowed_trusted_to_trusted"] == 0, arm
        assert s["chain_installs_trusted_to_trusted"] == 0, arm
        assert s["trusted_poll_entry_exits"] == 0, arm
        assert (s["chain_skipped_trusted_poll_unverified"] ==
                s["chain_skipped_trusted_to_trusted"]), arm
        assert s["chain_skipped_trusted_to_trusted"] > 0 or not t2t, arm

    doc = {arm: summary(s) for arm, s in stats.items()}
    doc["slot_bounds"] = bounds
    (out / "trusted-exit-chain.json").write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    main()
