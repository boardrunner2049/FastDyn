"""Generate the stock FastDyn launch command and its plugin inputs for BTT."""
import json
from pathlib import Path
import subprocess


def command(qemu, firmware, plugin, out, socket, cache=None):
    root = Path(__file__).resolve().parents[5]
    cfg = out / "fastdyn.toml"
    cache_line = f'supercache = {json.dumps(str(cache.resolve()))}\nsupercache_flat = false' if cache else ""
    cfg.write_text(f'''[Machine]
platform = "btt-skr-e3-v2"
qemu_path = {json.dumps(str(qemu))}
icount = {{shift = 3, sleep = false, align = false}}
serial = "stdio"
monitor = "none"
display = "none"
log_options = "none"
qmp_socket = {json.dumps(str(socket))}
semihosting = false
coverage = false
{cache_line}
[Memory.main]
id = "ram0"
memory_size = "48K"
base_address = "0x20000000"
memory_type = "SRAM"
backend = "ram"
share = false
prealloc = false
[Device.Models]
[[CPU.cpu0]]
arch = "arm"
machine = "btt-skr-e3-v2"
cpu = "cortex-m3"
binary = {json.dumps(str(firmware))}
init_nsvtor = 134246400
plugin_library = {json.dumps(str(plugin.resolve()))}
[CPU.cpu0.plugins.introspection]
enabled = false
''')
    record = out / "fastdyn-launch.json"
    subprocess.run([root / "fastdyn-env/bin/python", root / "tools/scopt/scopt/run/fdshim",
                    "--scopt-out", record, "-q", "run", "-c", cfg, "-o", out / "fastdyn"],
                   cwd=root, check=True)
    result = json.loads(record.read_text())
    if result["fastdyn_exit"] or not result["reached_start_execution"]:
        raise RuntimeError("FastDyn did not configure BTT")
    return result
