"""
One run of one firmware against one backend. Prints either a
single float (wall-clock seconds), "TIMEOUT", or "ERROR:<msg>".

Assumes two long-running Docker containers exist:
    halucinator   — unmodified HALucinator image
    halucinator-fd    — FastDyn (with embedded Python interpreter) image

Use setup_containers.sh (once) to create them.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tomllib
except ImportError:        # python < 3.11
    try:
        import tomli as tomllib  # pip install tomli
    except ImportError:
        sys.stderr.write(
            "need tomllib (python 3.11+) or tomli (`pip install tomli`)\n"
        )
        sys.exit(1)

HERE = Path(__file__).resolve().parent
PEER_SRC = HERE / "peers" / "bench_uart_peer.py"
PEER_DST = "/tmp/bench_uart_peer.py"

CONTAINERS = {"halucinator": "halucinator", "halucinator-fd": "halucinator-fd"}


def docker(*args, **kw):
    return subprocess.run(["docker", *args], **kw)


def container_running(name):
    r = docker("inspect", "-f", "{{.State.Status}}", name,
               capture_output=True, text=True)
    return r.returncode == 0 and "running" in r.stdout


def ensure_container(name):
    if container_running(name):
        return
    # Try to start if it exists stopped.
    r = docker("start", name, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"container {name} not available — run setup_containers.sh")


def kill_halucinator(container):
    # Kill anything a previous run may have left in the container: the
    # halucinator process, the QEMU it spawned (which outlives it), and a
    # stale UART peer. An interrupted sweep leaves all three behind, and a
    # leftover pair holds the ZMQ endpoints so the next run's peer waits
    # out its full timeout.
    # The [x] bracket trick keeps each pattern from matching this very
    # `bash -c` command line (pkill -f would otherwise kill the shell
    # before it reaches the next pkill).
    docker("exec", container, "bash", "-c",
           "pkill -9 -f '[/]usr/local/bin/halucinator'; pkill -9 -x qemu-system-arm; "
           "pkill -9 -f '[b]ench_uart_peer'; true",
           capture_output=True)
    # Give it a moment to release ports.
    time.sleep(0.3)


def reset_plugin_cache(container):
    # Halucinator writes a single tmp/build/plugins/{virtuals,modifiers}.txt
    # that's keyed on the intercept YAML's hash, not the firmware. Different
    # firmwares can hit the same hash and silently reuse each other's
    # plugin callbacks, which produces a completely broken run. Wipe the
    # cache + hash file before every run so each firmware regenerates.
    docker(
        "exec", container, "bash", "-lc",
        "rm -f /root/halucinator/tmp/build/plugins/virtuals.txt "
        "       /root/halucinator/tmp/build/plugins/modifiers.txt "
        "       /root/halucinator/tmp/config/hash.yaml",
        capture_output=True,
    )


def install_peer(container):
    # `docker cp` tries to preserve host ownership and fails when the host
    # UID isn't in the container's subuid map. Stream the file in over
    # stdin instead — container-root ends up owning the result, which is
    # what we want anyway.
    with open(PEER_SRC, "rb") as f:
        subprocess.run(
            ["docker", "exec", "-i", container,
             "sh", "-c", f"cat > {PEER_DST} && chmod +r {PEER_DST}"],
            stdin=f,
            check=True,
        )


def run_uart(spec, container, label):
    kill_halucinator(container)
    install_peer(container)
    reset_plugin_cache(container)

    # --- Start the peer FIRST.  HALucinator's IOServer-over-ZMQ design
    # drops PUB messages with no SUB subscribed yet (classic slow-joiner).
    # If the emulator is launched first and the firmware boots faster than
    # the peer can subscribe, the firmware's banner / prompt is lost and
    # the peer waits forever for a cue that's already been published.
    peer_args = [
        "docker", "exec", container,
        "python3", PEER_DST,
        "--id", str(spec["uart_id"]),
        "--success", spec["success"],
        "--timeout", str(spec["timeout"]),
        "--label", label,
    ]
    if spec.get("newline"):
        peer_args.append("--newline")
    for step in spec["steps"]:
        peer_args += ["--step", f"{step['wait']}|{step['send']}"]

    peer = subprocess.Popen(
        peer_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # Collect peer output in background so its pipes never fill up.
    peer_stderr_lines = []
    peer_stdout_lines = []
    ready = threading.Event()

    def _pump_stderr():
        for line in peer.stderr:
            peer_stderr_lines.append(line)
            if "[BENCH] READY" in line:
                ready.set()

    def _pump_stdout():
        for line in peer.stdout:
            peer_stdout_lines.append(line)

    threading.Thread(target=_pump_stderr, daemon=True).start()
    threading.Thread(target=_pump_stdout, daemon=True).start()

    if not ready.wait(timeout=10):
        peer.kill()
        sys.stderr.write("ERROR: peer never signalled READY within 10s\n")
        return None

    # --- Peer is listening on the ZMQ topics. Now fire the emulator.
    emu_args = [
        "docker", "exec", "-w", spec["emu_cwd"], container,
        "bash", "-lc", spec["emu_cmd"],
    ]
    emu_log = []
    emu = subprocess.Popen(
        emu_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        preexec_fn=os.setsid,
    )

    def _drain_emu():
        for line in emu.stdout:
            emu_log.append(line)

    threading.Thread(target=_drain_emu, daemon=True).start()

    # Wait for the peer, but stop early if the emulator dies first (e.g. a
    # stale QEMU still holds the GDB port): the peer would otherwise sit
    # out its whole timeout for nothing.
    emu_died_early = False
    try:
        deadline = time.time() + spec["timeout"] + 30
        while peer.poll() is None:
            if emu.poll() is not None:
                # give the peer a moment in case success just arrived
                try:
                    peer.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    emu_died_early = True
                    peer.kill()
                    peer.wait(timeout=5)
                break
            if time.time() > deadline:
                peer.kill()
                peer.wait(timeout=5)
                break
            time.sleep(0.2)
    finally:
        kill_halucinator(container)
        try:
            os.killpg(emu.pid, signal.SIGTERM)
            emu.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(emu.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # Guard: on the halucinator-fd backend, the FastDyn plugin must actually
    # have loaded. Halucinator keeps going with plain QEMU if the plugin
    # path doesn't resolve, which would produce a completely meaningless
    # "halucinator-fd" number.
    emu_text = "".join(emu_log)
    if emu_died_early:
        sys.stderr.write(f"ERROR: emulator exited (rc={emu.returncode}) before the peer "
                         "saw success. Emu log tail:\n")
        sys.stderr.write("\n".join(emu_text.splitlines()[-10:]) + "\n")
    if os.environ.get("_DUMP_EMU_LOG"):
        with open(os.environ["_DUMP_EMU_LOG"], "w") as _f:
            _f.write(emu_text)
    if container == CONTAINERS["halucinator-fd"]:
        if "Could not load plugin" in emu_text or "libfastdyn.so: cannot open" in emu_text:
            sys.stderr.write(
                "ERROR: FastDyn plugin failed to load on halucinator-fd — "
                "run invalidated. Emu log tail:\n"
            )
            sys.stderr.write("\n".join(emu_text.splitlines()[-10:]) + "\n")
            return None

    # Peer prints "[BENCH] <label> SUCCESS <seconds>" on stderr.
    for line in peer_stderr_lines:
        if "[BENCH]" in line and "SUCCESS" in line:
            return float(line.strip().split()[-1])
    return None


def run_stdout(spec, container, label):
    kill_halucinator(container)
    reset_plugin_cache(container)
    cmd = [
        "docker", "exec", "-w", spec["emu_cwd"], container,
        "bash", "-lc", spec["emu_cmd"],
    ]
    t0 = time.perf_counter()
    emu = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        preexec_fn=os.setsid,
    )

    marker = spec["success"]
    timeout = spec["timeout"]
    elapsed = [None]
    saw_plugin_fail = [False]
    emu_lines = []

    def reader():
        for line in emu.stdout:
            emu_lines.append(line)
            # Emu log goes to stderr so it doesn't pollute the CSV line
            # that --csv writes to stdout (sweep.sh captures stdout).
            sys.stderr.write(line)
            sys.stderr.flush()
            if ("Could not load plugin" in line
                    or "libfastdyn.so: cannot open" in line):
                saw_plugin_fail[0] = True
            if marker in line and elapsed[0] is None:
                elapsed[0] = time.perf_counter() - t0
                return

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    deadline = t0 + timeout
    while elapsed[0] is None and time.perf_counter() < deadline:
        time.sleep(0.05)
        if emu.poll() is not None:
            break

    kill_halucinator(container)
    try:
        os.killpg(emu.pid, signal.SIGTERM)
        emu.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(emu.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if os.environ.get("_DUMP_EMU_LOG"):
        with open(os.environ["_DUMP_EMU_LOG"], "w") as _f:
            _f.write("".join(emu_lines))

    if container == CONTAINERS["halucinator-fd"] and saw_plugin_fail[0]:
        sys.stderr.write(
            "ERROR: FastDyn plugin failed to load on halucinator-fd — run invalidated.\n"
        )
        return None
    return elapsed[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fw", required=True)
    ap.add_argument("--backend", required=True, choices=list(CONTAINERS))
    ap.add_argument("--csv", action="store_true",
                    help="print 'fw,backend,wall' instead of just wall")
    ap.add_argument("--dump-emu-log",
                    help="diagnostic: write full emulator stdout+stderr to path")
    args = ap.parse_args()
    os.environ.setdefault("_DUMP_EMU_LOG", args.dump_emu_log or "")

    with open(HERE / "firmwares.toml", "rb") as f:
        firmwares = tomllib.load(f)

    if args.fw not in firmwares:
        sys.exit(f"unknown firmware '{args.fw}'")

    spec = firmwares[args.fw]
    container = CONTAINERS[args.backend]
    ensure_container(container)

    label = f"{args.fw}_{args.backend}"
    try:
        if spec["mode"] == "uart":
            wall = run_uart(spec, container, label)
        elif spec["mode"] == "stdout":
            wall = run_stdout(spec, container, label)
        else:
            sys.exit(f"unknown mode '{spec['mode']}'")
    except subprocess.TimeoutExpired:
        wall = None

    if args.csv:
        out = f"{args.fw},{args.backend},{wall if wall is not None else 'NaN'}"
        print(out)
    else:
        print("TIMEOUT" if wall is None else f"{wall:.4f}")
    sys.exit(0 if wall is not None else 2)


if __name__ == "__main__":
    main()
