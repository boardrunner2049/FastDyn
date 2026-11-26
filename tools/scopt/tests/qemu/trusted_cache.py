"""Exercise trusted input integrity opt-in on private copies of a compiled public sealed launch."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from scopt.formats.sc_records import HEADER_SIZE, RECORD_SIZE, ScHeader, ScRecord, sparse_copy
from scopt.run.helper_pgo import _guest_lines, _launch
from scopt.run.v7_stats import counter_problems


def change_records(cache, field):
    with cache.open("r+b") as stream:
        header = ScHeader.from_bytes(stream.read(HEADER_SIZE))
        assert header.record_count and not header.reserved
        stream.seek(header.records_offset)
        raw = bytearray(stream.read(header.record_count * RECORD_SIZE))
        for pos in range(0, len(raw), RECORD_SIZE):
            record = ScRecord.from_bytes(raw[pos:pos + RECORD_SIZE])
            if field == "checksum":
                record = record.replace(checksum=record.checksum ^ 1)
            elif field == "bounds":
                record = record.replace(tc_offset=record.end_offset)
            raw[pos:pos + RECORD_SIZE] = record.to_bytes()
        stream.seek(header.records_offset)
        stream.write(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    accel = launch["argv"][launch["argv"].index("-accel") + 1]
    source = next(part.split("=", 1)[1] for part in accel.split(",") if part.startswith("supercache="))
    source = Path(launch["cwd"]) / source
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    expected = None
    for arm, field, trust, success in (
        ("ordinary", None, False, True),
        ("trusted-checksum", "checksum", True, True),
        ("ordinary-checksum", "checksum", False, False),
        ("trusted-bounds", "bounds", True, False),
    ):
        rd = out / arm
        rd.mkdir()
        cache = rd / "cache.sc"
        sparse_copy(source, cache)
        if field:
            change_records(cache, field)
        argv, env, _ = _launch(launch, rd, cache)
        env.pop("QEMU_SC_TRUSTED", None)
        if trust:
            env["QEMU_SC_TRUSTED"] = "1"
        env["QEMU_SC_V7_STATS"] = str(rd / "stats.json")
        with (rd / "qemu.log").open("w") as stream:
            result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                                    cwd=launch["cwd"], env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=120)
        log = (rd / "qemu.log").read_text()
        if not success:
            assert result.returncode != 0 and "invalid record #0" in log, arm
            continue
        assert result.returncode == 0, arm
        observed = _guest_lines(launch, log)
        expected = observed if expected is None else expected
        assert observed == expected, arm
        stats = json.loads((rd / "stats.json").read_text())
        assert stats["user_trusted_input"] == trust
        assert (stats["public_record_checksum_checks"] == 0) == trust
        assert stats["guest_hash_checks"] == 0
        assert counter_problems(stats) == [], (arm, counter_problems(stats))


if __name__ == "__main__":
    main()
