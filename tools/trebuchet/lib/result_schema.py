"""What the compute firmware reports, and where each value lives in its statistics.

The firmware calls four report functions with four values each (r0-r3).  FastDyn
prints those sixteen registers; the reference transcript of a run is that list.  Stock
QEMU has no report hook, so its runner reads the statistics structure from guest memory
at the end of the work instead.  This table says which statistics word each reported
value is, so both arms are checked against the same expected values, field by field.

`tests/test_results.py` derives the same mapping from `main_compute.c` and compares.
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


def expected_values(reference_lines):
    """The sixteen reported values of a reference transcript (`Reg N:0x...` lines)."""
    values = [int(line.split(":", 1)[1], 16) for line in reference_lines if line.startswith("Reg ")]
    if len(values) != len(REPORTED):
        raise ValueError(f"expected {len(REPORTED)} reported values, found {len(values)}")
    return values


def stats_mismatches(stats, expected):
    """Why a statistics structure read from guest memory disagrees with the expected values.

    `stats` is the list of structure words, `expected` the sixteen reported values.  Every
    reported value that is a function of structure fields is compared at its own field;
    an empty list means they all agree.
    """
    if len(stats) < len(STATS_FIELDS):
        return [f"statistics structure has {len(stats)} words, expected {len(STATS_FIELDS)}"]
    problems = []
    for position, (fields, want) in enumerate(zip(REPORTED, expected)):
        if fields is None:
            continue
        got = 0
        for name in fields:
            got ^= stats[STATS_FIELDS.index(name)]
        if got != want:
            problems.append(f"{'^'.join(fields)} = 0x{got:08x}, expected 0x{want:08x} (report value {position})")
    return problems
