"""
In-container macrobench runner (Figure 9). Runs N iterations of each
firmware on the backend this image is built for and writes rows
(iter,firmware,backend,wall_s) to /results/<firmware>.csv, replacing any
earlier rows of the same backend so both images can share one file.

Wall time is what the paper measured: from the moment the UART peer is
listening and the emulator is launched until the firmware's success marker
appears on its UART (peers/bench_uart_peer.py measures it). Every iteration
is a fresh HALucinator + QEMU process. Before the timed iterations of a
firmware, one untimed test run checks that the workload runs in this
container (and absorbs the first-use costs of a fresh container); it is not
recorded. An iteration that does not finish within the per-firmware timeout
is killed, logged to /results/<firmware>_<backend>_iter<i>.log, and retried once.

    macro_run.py --backend halucinator|halucinator-fd [--iters N] [--timeout S]
                 [--retries K] [--results DIR] [fw ...]
"""
import argparse
import csv
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

HERE = Path(__file__).resolve().parent
PEER = HERE / "peers" / "bench_uart_peer.py"
DEFAULT_FWS = ["stm32_uart_it", "zephyr_k64f", "zephyr_h103", "zephyr_fs"]


def sh(cmd):
    subprocess.run(["bash", "-lc", cmd], capture_output=True)


def kill_leftovers():
    # A previous iteration's halucinator, its QEMU, and the peer hold the
    # GDB port and the ZMQ endpoints; the [x] trick keeps pkill -f from
    # matching this very command line.
    sh("pkill -9 -f '[/]usr/local/bin/halucinator'; pkill -9 -x qemu-system-arm; "
       "pkill -9 -f '[b]ench_uart_peer'; rm -f /tmp/Halucinator2IoServer* /tmp/IoServer2Halucinator*; true")
    time.sleep(0.3)


def reset_plugin_cache():
    # HALucinator caches generated plugin callbacks keyed on the intercept
    # YAML hash; different firmwares can collide, so wipe it every run.
    sh("rm -f /root/halucinator/tmp/build/plugins/virtuals.txt "
       "/root/halucinator/tmp/build/plugins/modifiers.txt /root/halucinator/tmp/config/hash.yaml")
    # The FastDyn backend maps guest RAM from a shared file that survives
    # between runs; give every run zeroed RAM like the first one.
    sh("for f in /dev/shm/my_m4_ram*; do [ -e \"$f\" ] || continue; "
       "n=$(stat -c%s \"$f\"); : > \"$f\"; truncate -s \"$n\" \"$f\"; done; true")


def run_once(spec, backend, label, timeout, log_path=None):
    """One emulator run. Returns (wall_seconds, None) or (None, error)."""
    kill_leftovers()
    reset_plugin_cache()

    # Peer first: HALucinator's ZMQ PUB drops messages with no subscriber,
    # so the emulator is launched only after the peer reports READY.
    peer_args = ["python3", str(PEER), "--id", str(spec["uart_id"]),
                 "--success", spec["success"], "--timeout", str(timeout),
                 "--label", label]
    if spec.get("newline"):
        peer_args.append("--newline")
    for step in spec["steps"]:
        peer_args += ["--step", f"{step['wait']}|{step['send']}"]
    peer = subprocess.Popen(peer_args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    peer_err, peer_out, ready = [], [], threading.Event()

    def pump():
        for line in peer.stderr:
            peer_err.append(line)
            if "[BENCH] READY" in line:
                ready.set()
    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=lambda: peer_out.extend(peer.stdout), daemon=True).start()
    if not ready.wait(timeout=10):
        peer.kill()
        return None, "peer never signalled READY"

    emu = subprocess.Popen(["bash", "-lc", spec["emu_cmd"]], cwd=spec["emu_cwd"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, bufsize=1, preexec_fn=os.setsid)
    emu_log = []
    threading.Thread(target=lambda: emu_log.extend(emu.stdout), daemon=True).start()

    died_early = False
    deadline = time.time() + timeout + 5
    try:
        while peer.poll() is None:
            if emu.poll() is not None:
                try:
                    peer.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    died_early = True
                    peer.kill(); peer.wait(timeout=5)
                break
            if time.time() > deadline:
                peer.kill(); peer.wait(timeout=5)
                break
            time.sleep(0.2)
    finally:
        kill_leftovers()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(emu.pid, sig); emu.wait(timeout=5); break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    text = "".join(emu_log)
    for line in peer_err:
        if "[BENCH]" in line and "SUCCESS" in line:
            return float(line.split()[-1]), None
    if backend == "halucinator-fd" and ("Could not load plugin" in text
                                        or "libfastdyn.so: cannot open" in text):
        err = "FastDyn plugin failed to load"
    elif died_early:
        err = "emulator exited before success"
    else:
        err = f"no '{spec['success']}' within {timeout:.0f} s"
    if log_path is not None:
        log_path.write_text("== emulator ==\n" + text + "\n== firmware UART output seen by peer ==\n"
                            + "".join(peer_out) + "\n== peer ==\n" + "".join(peer_err))
        err += f" (log: {log_path.name})"
    return None, err


def write_rows(path, backend, fw, walls):
    rows = []
    if path.exists():
        with open(path) as f:
            rows = [r for r in csv.DictReader(f)
                    if not (r["firmware"] == fw and r["backend"] == backend)]
    rows += [{"iter": i + 1, "firmware": fw, "backend": backend,
              "wall_s": f"{w:.4f}" if w is not None else "NaN"} for i, w in enumerate(walls)]
    rows.sort(key=lambda r: (int(r["iter"]), r["backend"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["iter", "firmware", "backend", "wall_s"])
        w.writeheader(); w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=["halucinator", "halucinator-fd"])
    ap.add_argument("--iters", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=None,
                    help="per-iteration limit in seconds (default: the firmware's "
                         "`timeout` in firmwares.toml, ~3x the expected HALucinator time)")
    ap.add_argument("--retries", type=int, default=1,
                    help="re-run a failed iteration this many times (default 1)")
    ap.add_argument("--results", type=Path, default=Path("/results"))
    ap.add_argument("fw", nargs="*")
    args = ap.parse_args()
    with open(HERE / "firmwares.toml", "rb") as f:
        firmwares = tomllib.load(f)
    fws = args.fw or DEFAULT_FWS
    for fw in fws:
        if fw not in firmwares:
            sys.exit(f"unknown firmware '{fw}' (known: {', '.join(firmwares)})")
    args.results.mkdir(parents=True, exist_ok=True)

    failed = False
    for fw in fws:
        spec = firmwares[fw]
        timeout = args.timeout or spec["timeout"]
        label = f"{fw}_{args.backend}"
        # One untimed test run per firmware, not recorded: it checks that the
        # workload runs in this container and pays the first-use costs of a
        # fresh container (page cache) so that they do not land on iteration 1.
        print(f"{args.backend:15s} {fw:14s} test run", end=" ", flush=True)
        wall, err = run_once(spec, args.backend, label, timeout,
                             args.results / f"{label}_testrun.log")
        print("ok" if wall is not None else f"FAILED ({err})", flush=True)
        walls = []
        for i in range(1, args.iters + 1):
            for attempt in range(args.retries + 1):
                wall, err = run_once(spec, args.backend, label, timeout,
                                     args.results / f"{label}_iter{i}.log")
                if wall is not None:
                    break
                print(f"{args.backend:15s} {fw:14s} iter {i}: FAILED ({err})"
                      + (", retrying" if attempt < args.retries else ""), flush=True)
            walls.append(wall)
            if wall is None:
                failed = True
            else:
                print(f"{args.backend:15s} {fw:14s} iter {i}: {wall:.4f} s", flush=True)
        write_rows(args.results / f"{fw}.csv", args.backend, fw, walls)
    print("wrote " + ", ".join(str(args.results / f"{fw}.csv") for fw in fws))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
