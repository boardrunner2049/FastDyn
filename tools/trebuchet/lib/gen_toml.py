#!/usr/bin/env python3
"""gen_toml.py ELF OUT.toml NAME [--no-cache]: FastDyn configuration for one compute-firmware image.

Virtual addresses are read from the ELF so the configuration always matches the image.
Environment: TREBUCHET_FASTDYN (the FastDyn checkout), ARM_PREFIX (default arm-none-eabi-).
"""
import os, re, subprocess, sys
TC = os.environ.get("ARM_PREFIX", "arm-none-eabi-")
F = os.environ["TREBUCHET_FASTDYN"]
elf, out, name = sys.argv[1:4]
cache = "--no-cache" not in sys.argv

def nm():
    r = {}
    for line in subprocess.run([TC + "nm", elf], capture_output=True, text=True, check=True).stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            r[p[2]] = int(p[0], 16) & ~1
    return r

def insns(addr, count):
    txt = subprocess.run([TC + "objdump", "-d", f"--start-address=0x{addr:x}", f"--stop-address=0x{addr + 64:x}", elf],
                         capture_output=True, text=True, check=True).stdout
    return [int(m.group(1), 16) for m in re.finditer(r"^\s+([0-9a-f]+):\s", txt, re.M)][:count]

s = nm(); v = []
ws = s["vComputeWindowStart"]
v.append((ws, "benchmark_start", []))
v.append((ws + 2, "debug_log", ["window-start"]))
for i in range(1, 5):
    for r in range(4):
        v.append((s[f"vComputeReport{i}"] + 2 * r, "printreg", [str(r)]))
done = s["vComputeDone"]
v.append((done, "debug_log", ["done"]))
v.append((done + 2, "benchmark_end", [name]))
for sym, tag in (("vAssertCalled", "ASSERT"), ("vApplicationStackOverflowHook", "STACK-OVERFLOW"),
                 ("vApplicationMallocFailedHook", "MALLOC-FAILED"), ("prvGetRegistersFromStack", "HARDFAULT"),
                 ("Default_Handler", "DEFAULT-HANDLER")):
    if sym in s:
        a = insns(s[sym], 2)
        v.append((a[0], "debug_log", [f"FAILURE-{tag}"]))
        v.append((a[1], "benchmark_end", [f"{name}-FAILURE-{tag}"]))
L = ["[Memory]", "", "[Memory.main]", 'id = "ram0"', 'base_address = "0x20000000"', 'memory_size  = "8M"',
     'memory_type  = "SRAM"', 'backend      = "file"', 'memory_file  = "ram0.ram"', "share        = false",
     "prealloc     = false", "", "[[Memory.input]]", 'id = "input"', "index = 1", 'base_address = "0x21000000"',
     'memory_size  = "1M"', 'memory_type  = "SRAM"', 'backend      = "file"', 'memory_file  = "input.bin"',
     "share        = false", "prealloc     = false", "", "[Machine]", 'platform = "generic-cortexm"',
     f'qemu_path = "{F}/third_party/common/qemu/build/qemu-system-arm"']
if cache:
    L += [f'supercache = "cache/{name}.sc"', "supercache_flat = false"]
L += ["icount = { shift = 0, sleep = false, align = false }", 'display = "none"', 'serial = "none"',
      "monitor_port = 0", 'qmp_socket = "qmp.sock"', 'log_file = "qemu.log"', 'log_options = "none"',
      "coverage = false", "finline = 'None'", "", "[Device]", "[Device.Models]", "", "[CPU]", "[[CPU.cpu0]]",
      'arch = "arm"', 'machine = "cortexm"', 'cpu = "cortex-m3"', f'binary = "{elf}"',
      f'plugin_library = "{F}/build/libfastdyn.so"', 'init_nsvtor = "0x0"', "",
      "[CPU.cpu0.plugins.introspection]", "enabled = false"]
for addr, instr, args in v:
    L += ["", "[[CPU.cpu0.virtuals]]", f'at = "0x{addr:08x}"', f'instruction = "{instr}"']
    if args:
        L.append("args = [" + ", ".join(f'"{a}"' for a in args) + "]")
open(out, "w").write("\n".join(L) + "\n")
print(out, f"window_start=0x{ws:x} done=0x{done:x}")
