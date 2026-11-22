"""What each workload reports, and where each value lives in guest memory.

A workload reports through marker functions that take four values (r0-r3).  FastDyn
prints those registers; the reference transcript of a run is that list.  Stock QEMU has
no report hook, so its runner reads a result structure from guest memory at the end of
the work instead.  The tables say which structure word each reported value is, so both
arms are checked against the same expected values, field by field.

`compute` (the packet-processing application) reports sixteen values out of its
statistics; the four fixed-work workloads report a checksum and an iteration count.
`tests/test_results.py` derives the mappings from the firmware sources and compares.
"""

# Stats_t, in declaration order (index = word offset in the structure).
STATS_FIELDS = (
    "frames_ok", "err_crc", "err_len", "err_seq", "garbage_bytes", "err_type", "err_payload",
    "err_text", "kv_miss", "kv_evict", "processed", "sensor_digest", "blob_digest",
    "route_digest", "vm_digest", "digest", "heartbeats", "monitor_scans", "monitor_kv_sum",
    "input_status",
)
# Never reported by the firmware: they depend on when the timer and monitor tasks ran.
TIMING_DEPENDENT = ("heartbeats", "monitor_scans", "monitor_kv_sum")

# The sixteen reported values in transcript order.  A tuple of fields is their XOR.
# None: the value mixes in a local that is not in the structure (the key-value checksum).
REPORTED = (
    ("digest",), ("frames_ok",), ("processed",), ("input_status",),
    ("err_crc",), ("err_len",), ("err_seq",), ("garbage_bytes",),
    ("err_type",), ("err_payload",), ("err_text",), ("kv_miss",),
    ("sensor_digest", "blob_digest"), ("route_digest",), ("vm_digest",), None,
)

# The fixed-work workloads (fw_main.c): FwResult_t fw_result, reported once as
# vComputeReport1(checksum, iterations, 0, 0).
FW_FIELDS = ("checksum", "iterations")
FW_REPORTED = (("checksum",), ("iterations",), (), ())

WORKLOADS = {"compute": 0, "fwbench": 1, "kernbench": 2, "libbench": 3, "algobench": 4}   # FW_WORKLOAD


def schema(workload):
    """(guest symbol of the result structure, its fields, the reported values) of a workload."""
    if workload == "compute":
        return "xStats", STATS_FIELDS, REPORTED
    if workload in WORKLOADS:
        return "fw_result", FW_FIELDS, FW_REPORTED
    raise ValueError(f"unknown workload {workload!r}")


def expected_values(reference_lines, workload="compute"):
    """The reported values of a reference transcript (`Reg N:0x...` lines)."""
    count = len(schema(workload)[2])
    values = [int(line.split(":", 1)[1], 16) for line in reference_lines if line.startswith("Reg ")]
    if len(values) != count:
        raise ValueError(f"expected {count} reported values, found {len(values)}")
    return values


def stats_mismatches(stats, expected, workload="compute"):
    """Why a result structure read from guest memory disagrees with the expected values.

    `stats` is the list of structure words, `expected` the reported values.  Every reported
    value that is a function of structure fields is compared at its own field (an empty
    tuple is the constant 0); an empty list means they all agree.
    """
    _, fields, reported = schema(workload)
    if len(stats) < len(fields):
        return [f"result structure has {len(stats)} words, expected {len(fields)}"]
    problems = []
    for position, (names, want) in enumerate(zip(reported, expected)):
        if names is None:
            continue
        got = 0
        for name in names:
            got ^= stats[fields.index(name)]
        if got != want:
            problems.append(f"{'^'.join(names) or 'constant 0'} = 0x{got:08x}, expected 0x{want:08x} (report value {position})")
    return problems
