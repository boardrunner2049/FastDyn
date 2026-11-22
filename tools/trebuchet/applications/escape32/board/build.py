#!/usr/bin/env python3
"""Link the board against a configured FastDyn QEMU tree without modifying the shared runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("build", type=Path, help="existing pinned QEMU ARM build directory")
p.add_argument("out", type=Path, help="new or existing private build directory")
args = p.parse_args()
build, out = args.build.resolve(), args.out.resolve()
out.mkdir(exist_ok=True, parents=True)
source = Path(__file__).with_name("escape1-g071.c").resolve()
row = next(r for r in json.loads((build / "compile_commands.json").read_bytes()) if r["file"].endswith("/cortexm.c"))
obj = str(out / "escape1-g071.o")
substitutions = {row["file"]: str(source), row["output"]: obj, row["output"] + ".d": obj + ".d"}
compile_argv = [substitutions.get(x, x) for x in shlex.split(row["command"])]
subprocess.run(compile_argv, cwd=build, check=True)
# Base UART omits the character backend wakeup after the guest drains RDR.
# Reinstate backpressure so a queued configuration request can continue.
uart = next(r for r in json.loads((build / "compile_commands.json").read_bytes())
            if r["file"].endswith("/stm32l4x5_usart.c"))
original = (build / uart["file"]).read_text()
needle = "s->isr &= ~R_ISR_RXNE_MASK;\n        stm32l4x5_update_irq(s);"
if original.count(needle) != 1:
    raise ValueError("unexpected USART receive-register implementation")
patched = out / "stm32l4x5_usart.c"
patched.write_text(original.replace(needle, needle + "\n        qemu_chr_fe_accept_input(&s->chr);"))
uart_obj = str(out / "stm32l4x5_usart.o")
replacements = {uart["file"]: str(patched), uart["output"]: uart_obj, uart["output"] + ".d": uart_obj + ".d"}
subprocess.run([replacements.get(x, x) for x in shlex.split(uart["command"])] + ["-I" + str((build / uart["file"]).parent)], cwd=build, check=True)
line = subprocess.check_output(["ninja", "-t", "commands", "qemu-system-arm"], cwd=build, text=True).splitlines()[-1]
link_argv = shlex.split(line)
link_argv.insert(link_argv.index(row["output"]), obj)
link_argv[link_argv.index(uart["output"])] = uart_obj
binary = out / "qemu-system-arm"
link_argv[link_argv.index("-o") + 1] = str(binary)
subprocess.run(link_argv, cwd=build, check=True)
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


receipt = {
    "qemu_revision": subprocess.check_output(["git", "-c", f"safe.directory={build.parent}", "rev-parse", "HEAD"], cwd=build.parent, text=True).strip(),
    "source_sha256": sha(source),
    "original_uart_sha256": sha(build / uart["file"]),
    "patched_uart_sha256": sha(patched),
    "qemu_sha256": sha(binary),
}
(out / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
