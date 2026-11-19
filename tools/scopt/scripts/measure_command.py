"""Run one offline optimizer command and record its resource usage on Linux."""
import json
from pathlib import Path
import resource
import subprocess
import sys
import time


def main():
    out, *command = sys.argv[1:]
    start = time.monotonic_ns()
    status = subprocess.run(command, check=False).returncode
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    Path(out).write_text(json.dumps({
        "command": command,
        "wall_ns": time.monotonic_ns() - start,
        "child_user_seconds": usage.ru_utime,
        "child_system_seconds": usage.ru_stime,
        "max_rss_kib": usage.ru_maxrss,
        "status": status,
    }, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
