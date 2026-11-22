"""Observe the stock CubeBlack boot path on the generic Courbet machine."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baseline import COURBET, QEMU, ROOT, digest, fmu_build, fmu_runtime, replace_one

# Device-boundary hooks with exact native function entries in both release ELFs.
PERIPHERALS = """_crt0_entry start_advancing_sim
_ZN7ChibiOS7Storage10read_blockEPvtj storage_read_block *
_ZN7ChibiOS7Storage11write_blockEtPKvj storage_write_block *
hrt_micros64 hrt_micros64 *
_ZN6AP_HAL6microsEv micros32 *
stGetCounter chibiOS_tick_handler *
_ZN7ChibiOS8RCOutput5writeEht write_channel
"""
IMAGES = {
    "4bca82a3e14afb44c820fdc893582e8c4f2371de49b64a23752ed8ab80cddbf8": "normal",
    "df6b1ba76f1b6cad1a3818b34dbe332aebebed533ad36cf309cbb7181ab966af": "retained",
    "e420db42babb612dec4cfcd3311871b9e4b804036f927900664932bb6661ca9d": "published",
}


def request(stream, command, arguments=None):
    message = {"execute": command}
    if arguments:
        message["arguments"] = arguments
    stream.write(json.dumps(message).encode() + b"\n")
    while line := stream.readline():
        response = json.loads(line)
        if "error" in response:
            raise ValueError(response["error"])
        if "return" in response:
            return response["return"]
    raise ValueError("QMP closed before responding")


def summarize_mmio(path):
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 32768))
        rows = stream.read().decode(errors="replace").splitlines()
    unhandled = re.compile(r"IO (Read|Write) Access NOT Handled:.*address = (0x[0-9a-f]+), size = (\d+) bytes, pc=(0x[0-9a-f]+)")
    events = [m.groups() for row in rows if (m := unhandled.search(row))]
    return {"raw_sha256": digest(path), "raw_bytes": path.stat().st_size,
            "tail_unhandled": [{"operation": op, "address": addr, "bytes": int(size), "pc": pc}
                               for op, addr, size, pc in dict.fromkeys(events)]}


def run(image, plugin, work):
    sha = digest(image)
    if sha not in IMAGES:
        raise ValueError("unexpected CubeBlack release image")
    work.mkdir(parents=True, exist_ok=False)
    source = work / "labeled_conf"
    source.mkdir()
    (source / "virtuals.txt").write_text(PERIPHERALS)
    (source / "modifiers.txt").write_text("")
    profile = work / "profile.toml"
    profile.write_text(f'schema_version = 1\nfirmware = "ardupilot"\nelf = {json.dumps(str(image))}\n'
                       f'[validation]\nelf_sha256 = "{sha}"\n[validation.symbol_addresses]\n_vectors = 0x08004000\n')
    hooks = work / "hooks"
    subprocess.run([sys.executable, str(COURBET / "prepare_execution_configuration.py"),
                    "--profile", str(profile), "--output-dir", str(hooks)], cwd=ROOT, check=True)
    config_file = ROOT / "configs/copter462.toml"
    config = config_file.read_text()
    for old, new in [('virtuals/physics/flight_controllers/courbet/bin/arducopter_v462', str(image)),
                     ('virtuals/physics/flight_controllers/courbet/ardupilot/copter462/unlabeled_conf', str(hooks)),
                     ('build/libfastdyn.so', str(plugin)), ('../qemu/ws/my_m4_ram3', str(work / 'ram3')),
                     ('../qemu/ws/my_m4_ram', str(work / 'ram1'))]:
        config = replace_one(config, json.dumps(old), json.dumps(new))
    for helper in ("mission", "mavproxy"):
        config = replace_one(config, f"[Run.processes.{helper}]\nenabled = true",
                             f"[Run.processes.{helper}]\nenabled = false")
    # Each diagnostic reads registers through a private QMP socket, never a debug write.
    config = replace_one(config, "monitor_port = 5555", "# no TCP monitor required")
    build = fmu_build.resolve(config_file, repo_root=ROOT)
    plant = fmu_runtime.prepare_runtime(build, work / "plant")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FASTDYN_", "QEMU_SC_"))}
    env.update(FASTDYN_QEMU=str(QEMU), FASTDYN_FMI_BUILD="false",
               FASTDYN_FMI_OUTPUT=str(plant.library_path.parents[2]))
    pcs = []
    with tempfile.TemporaryDirectory() as short:
        qmp = Path(short) / "qmp.sock"
        runtime = work / "runtime.toml"
        runtime.write_text(replace_one(config, '"/tmp/qmp.sock"', json.dumps(str(qmp))))
        with (work / "console.log").open("w") as log:
            process = subprocess.Popen([str(ROOT / "fastdyn-env/bin/fastdyn"), "run", "-c", str(runtime),
                                        "--no-build-fmu", "-o", str(work / "run")], env=env, cwd=ROOT,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 25
                while not qmp.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(8)
                    sock.connect(str(qmp))
                    with sock.makefile("rwb", buffering=0) as stream:
                        if "QMP" not in json.loads(stream.readline()):
                            raise ValueError("invalid QMP greeting")
                        request(stream, "qmp_capabilities")
                        for _ in range(4):
                            time.sleep(0.25)
                            regs = request(stream, "human-monitor-command", {"command-line": "info registers"})
                            pcs.append(int(re.search(r"\bR15=([a-fA-F0-9]+)", regs)[1], 16))
                        request(stream, "quit")
                process.wait(timeout=15)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
    if process.returncode != 0:
        raise ValueError(f"boot observer exited {process.returncode}")
    symbols = [subprocess.check_output(["arm-none-eabi-addr2line", "-C", "-f", "-e", str(image), hex(pc)],
                                       text=True).strip().splitlines() for pc in pcs]
    io_log = work / "run/io.log"
    mmio = summarize_mmio(io_log)
    subprocess.run(["gzip", "-1", "--", str(io_log)], check=True)
    report = {"image": IMAGES[sha], "mmio": mmio, "elf_sha256": sha, "plugin_sha256": digest(plugin),
              "qemu_sha256": digest(QEMU), "plant_library_sha256": digest(plant.library_path),
              "generated_rules": {p.name: digest(p) for p in hooks.glob("*.txt")},
              "sampled_pcs": [hex(pc) for pc in pcs], "sampled_functions": symbols,
              "boot_observed": True, "operational_mission_admitted": False}
    (work / "boot.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("plugin", type=Path)
    parser.add_argument("new_directory", type=Path)
    args = parser.parse_args()
    run(args.elf.resolve(), args.plugin.resolve(), args.new_directory.resolve())
