"""Build a separate BTT test runner against a configured FastDyn QEMU build."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("build", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    build, out = args.build.resolve(), args.output.resolve()
    here = Path(__file__).resolve().parent
    out.mkdir(exist_ok=True, parents=True)
    commands = json.loads((build / "compile_commands.json").read_text())
    entries = []
    for name in ("cortexm.c", "cortexm_soc.c"):
        entry = next(x for x in commands if x["file"].endswith("/hw/arm/" + name))
        source = out / name
        shutil.copyfile(build / entry["file"], source)
        entries.append((entry, source))
    subprocess.run(["patch", "--fuzz=0", "-p1", "-i", str(here / "machine.patch")], cwd=out, check=True)
    replacements = {"qemu-system-arm": str(out / "qemu-system-arm")}
    for entry, source in entries:
        obj = entry["output"]
        dest = str(source.with_suffix(".o"))
        replacements[obj] = dest
        operands = {entry["file"]: str(source), obj: dest, obj + ".d": dest + ".d"}
        argv = [operands.get(a, a) for a in shlex.split(entry["command"])]
        argv.insert(1, "-I" + str(here))
        subprocess.run(argv, cwd=build, check=True)
    link = subprocess.check_output(["ninja", "-t", "commands", "qemu-system-arm"], cwd=build, text=True).splitlines()[-1]
    subprocess.run([replacements.get(a, a) for a in shlex.split(link)], cwd=build, check=True)


if __name__ == "__main__":
    main()
