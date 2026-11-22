"""Exercise the pinned native simulator and validate the published drawing job."""
import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time

JOB_SHA = "b3aa0a1ac047b669fd6788ecd8283dc71fd3f885fb477eaa1a1272706e849ab3"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(sim, job, run_dir, timeout):
    args = ["stdbuf", "-oL", str(sim), "-n", "-t", "1",
            "-r", "0.5", "-s", "positions.txt", "-b", "blocks.txt"]
    deadline = time.monotonic() + timeout
    lines = job.splitlines()
    with (run_dir / "serial.txt").open("wb") as log, (run_dir / "stderr.txt").open("wb") as err:
        proc = subprocess.Popen(args, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        try:
            # Keep the sender connected until the final dwell has been acknowledged.
            # EOF is an input byte on the pinned simulator's Linux serial backend.
            proc.stdin.write(job.rstrip(b"\r\n") + b"\n")
            proc.stdin.flush()
            pending = b""
            started = False
            acknowledgments = 0
            while acknowledgments < len(lines):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                    raise TimeoutError(f"received {acknowledgments}/{len(lines)} acknowledgments")
                data = os.read(proc.stdout.fileno(), 4096)
                require(data, "simulator exited before job completion")
                log.write(data)
                log.flush()
                complete = (pending + data).split(b"\n")
                pending = complete.pop()
                for line in complete:
                    line = line.strip()
                    if line.startswith(b"GrblHAL "):
                        require(not started, "unexpected controller reset")
                        started = True
                    elif line == b"error:7" and not started:
                        continue  # Blank EEPROM. grblHAL restores defaults on first boot.
                    elif line.startswith((b"error:", b"ALARM:")):
                        raise ValueError(line.decode())
                    elif line == b"ok":
                        require(started, "acknowledgment before boot")
                        acknowledgments += 1
            require(acknowledgments == len(lines), "extra acknowledgments")
            proc.stdin.write(b"\x06")  # Simulator's documented clean shutdown command.
            proc.stdin.flush()
            require(proc.wait(timeout=max(0, deadline - time.monotonic())) == 0, "simulator failed at shutdown")
            log.write(proc.stdout.read())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdin.close()
            proc.stdout.close()
    return acknowledgments


def planned_targets(job):
    # This job uses only absolute linear moves in millimetres. Default resolution
    # is 250 steps/mm; an exact half-step permits either adjacent integer because
    # upstream parses decimal coordinates in single precision.
    position = [Decimal(0)] * 3
    for line in job.decode("ascii").splitlines():
        command, *words = line.split()
        if command in {"G21", "G90", "G4"}:
            continue
        require(command in {"G0", "G1"}, f"unsupported job command {command}")
        for word in words:
            if word[0] in "XYZ":
                position["XYZ".index(word[0])] = Decimal(word[1:]) * 250
            else:
                require(word[0] == "F", f"unexpected move word {word}")
        yield position.copy()


def validate(job, run_dir):
    blocks = [tuple(map(int, row.split(",")[:3])) for row in (run_dir / "blocks.txt").read_text().splitlines()]
    for index, (target, block) in enumerate(zip(planned_targets(job), blocks, strict=True)):
        for ideal, actual in zip(target, block):
            require(abs(ideal - actual) <= Decimal("0.5"), f"planner block {index}: {target} != {block}")

    requests = (run_dir / "step-requests.bin").read_bytes()
    position = [0, 0, 0]
    steps = [0, 0, 0]
    next_block = 0
    origin = tuple(position)
    for i, event in enumerate(requests):
        require(next_block < len(blocks), f"step after final target at request {i}")
        require((event & 7) > 0 and not (event & 0x88), f"invalid step request {i}")
        for axis in range(3):
            if event & (1 << axis):
                remaining = blocks[next_block][axis] - position[axis]
                sign = -1 if event & (16 << axis) else 1
                require(sign * remaining > 0, f"step away from block {next_block} at request {i}")
                position[axis] += sign
                steps[axis] += 1
        # For a linear move the digitized line may lead or lag the ideal line
        # by one microstep on a minor axis, including a simultaneous-axis event.
        target = blocks[next_block]
        lengths = [abs(x - y) for x, y in zip(target, origin)]
        progress = [abs(x - y) for x, y in zip(position, origin)]
        major = lengths.index(max(lengths))
        for axis in range(3):
            error = abs(progress[axis] * lengths[major] - progress[major] * lengths[axis])
            require(error <= lengths[major], f"off-path motion in block {next_block}, request {i}")
        if tuple(position) == target:
            origin = target
            next_block += 1
    require(next_block == len(blocks), f"reached {next_block}/{len(blocks)} targets")
    last = (run_dir / "positions.txt").read_text().splitlines()[-1].replace(",", "").split()
    require(list(map(int, last[1:4])) == position == [0, 0, 953], f"final position {last}")
    return {"planner_blocks": len(blocks), "logical_step_requests": len(requests),
            "axis_steps": steps, "final_position_steps": position,
            "step_request_sequence_sha256": hashlib.sha256(requests).hexdigest()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("output", type=Path, help="new directory for all observations")
    p.add_argument("--timeout", type=float, default=180)
    args = p.parse_args()
    work = args.work.resolve()
    job = (work / "OSHW_Logo.nc").read_bytes()
    require(hashlib.sha256(job).hexdigest() == JOB_SHA, "job hash mismatch")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sim = work / "simulator/trace-release/grblHAL_sim"
    result = {"runtime": "upstream host simulator, logical GPIO observer", "job_sha256": JOB_SHA,
              "executable_sha256": hashlib.sha256(sim.read_bytes()).hexdigest()}
    try:
        result["accepted_commands"] = run(sim, job, out, args.timeout)
        result.update(validate(job, out))
        result["state"] = "passed"
    except Exception as exc:
        result.update(state="failed", reason=str(exc))
        raise
    finally:
        (out / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
