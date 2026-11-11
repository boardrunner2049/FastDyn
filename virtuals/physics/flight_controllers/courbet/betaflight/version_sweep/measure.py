#!/usr/bin/env python3
"""Run a sweep step and retain its real wall duration, command and output."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--phase', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required after --')
    args.output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    log = args.output / f'{args.version}-{args.phase}-{stamp}.log'
    record = dict(version=args.version, phase=args.phase, command=command,
                  cwd=str(Path.cwd()), started_utc=stamp, log=str(log))
    start = time.monotonic()
    with log.open('w') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
    record.update(elapsed_seconds=round(time.monotonic() - start, 3),
                  exit_code=result.returncode,
                  ended_utc=datetime.now(timezone.utc).isoformat())
    with (args.output / 'steps.jsonl').open('a') as stream:
        stream.write(json.dumps(record, sort_keys=True) + '\n')
    print(json.dumps(record, sort_keys=True), flush=True)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
