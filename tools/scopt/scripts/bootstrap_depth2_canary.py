#!/usr/bin/env python3
"""Build and qualify an isolated two-frame FastDyn benchmark canary."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import shlex

from bootstrap_host_inline import ROOT, command, digest, output, qualify_images, write_json

_BASE_SOURCE = """void benchmark_start(unsigned int cpu_index, void *udata) {
    (void)cpu_index;
    (void)udata;
    /* No I/O here: printfs would land inside the measured region and pollute
     * the sample. Just capture the monotonic timestamp. */
    g_bench_tick_count = 0;
    clock_gettime(CLOCK_MONOTONIC, &g_bench_start_ts);
    g_bench_started = true;
}"""
_CANARY_SOURCE = """static __attribute__((noinline, noclone))
void scopt_depth2_benchmark_worker(void) {
    g_bench_tick_count = 0;
    clock_gettime(CLOCK_MONOTONIC, &g_bench_start_ts);
    g_bench_started = true;
}

void benchmark_start(unsigned int cpu_index, void *udata) {
    (void)cpu_index;
    (void)udata;
    scopt_depth2_benchmark_worker();
}"""
_WORKER = "scopt_depth2_benchmark_worker"
_AUDITED_SOURCE_ROOT = "/opt/host-inline-depth2"


def replace_arg(argv: list[str], flag: str, value: str) -> None:
    pos = argv.index(flag) + 1
    argv[pos] = value


def compile_and_link(work: Path, log: Path) -> Path:
    """Reuse the existing plugin's compiler/linker flags and untouched objects."""
    build = ROOT / "build"
    source = ROOT / "virtuals" / "virtuals.c"
    text = source.read_text()
    if text.count(_BASE_SOURCE) != 1 or _WORKER in text:
        raise ValueError(f"{source} does not match the benchmark canary's source precondition")
    canary = text.replace(_BASE_SOURCE, _CANARY_SOURCE)
    copied = work / "virtuals.c"
    copied.write_text(canary)
    (work / "virtuals.patch").write_text("".join(difflib.unified_diff(
        text.splitlines(keepends=True), canary.splitlines(keepends=True),
        fromfile=str(source), tofile=str(copied),
    )))

    compile_db = json.loads((build / "compile_commands.json").read_text())
    row, = (row for row in compile_db if Path(row["file"]).as_posix().endswith("/virtuals/virtuals.c"))
    cwd = Path(row["directory"]).resolve()
    argv = shlex.split(row["command"])
    old_object = argv[argv.index("-o") + 1]
    new_object = work / "virtuals_virtuals.c.o"
    replace_arg(argv, "-o", str(new_object))
    replace_arg(argv, "-c", str(copied))
    if "-MQ" in argv:
        replace_arg(argv, "-MQ", str(new_object))
    if "-MF" in argv:
        replace_arg(argv, "-MF", str(new_object) + ".d")
    # Keep the plugin's byte identity when only the evidence directory moves.
    argv.extend([
        f"-fdebug-prefix-map={work}={_AUDITED_SOURCE_ROOT}",
        f"-fmacro-prefix-map={work}={_AUDITED_SOURCE_ROOT}",
    ])
    command(log, argv, cwd=cwd)

    links = [shlex.split(line) for line in output(["ninja", "-t", "commands", "libfastdyn.so"],
                                                   cwd=build).splitlines()]
    link, = (row for row in links if "-o" in row and row[row.index("-o") + 1] == "libfastdyn.so")
    if link.count(old_object) != 1 or (build / old_object).resolve() != (cwd / old_object).resolve():
        raise ValueError("canary compile and link disagree on the original virtuals object")
    link[link.index(old_object)] = str(new_object)
    link_dir = work / "build"
    link_dir.mkdir()
    plugin = link_dir / "libfastdyn.so"
    replace_arg(link, "-o", str(plugin))
    command(log, link, cwd=build)
    return plugin


def audit(plugin: Path) -> dict:
    """Record exact canary symbol bytes and direct/nested call geometry."""
    from elftools.elf.elffile import ELFFile
    from scopt.decode.x86 import sweep

    symbols = {}
    with plugin.open("rb") as stream:
        image = ELFFile(stream)
        table = image.get_section_by_name(".symtab")
        for name in ("benchmark_start", _WORKER):
            sym, = table.get_symbol_by_name(name)
            section = image.get_section(sym["st_shndx"])
            va = sym["st_value"]
            offset = va - section["sh_addr"]
            body = section.data()[offset:offset + sym["st_size"]]
            if not body.startswith(bytes.fromhex("f30f1efa 55 4889e5")):
                raise ValueError(f"{name} did not build with the audited SysV frame")
            if body[-2:] not in (b"\xc9\xc3", b"\x5d\xc3"):
                raise ValueError(f"{name} did not build with a normal frame return")
            local = sym["st_info"]["bind"] == "STB_LOCAL"
            if local != (name == _WORKER):
                raise ValueError(f"{name} did not retain its intended linkage")
            insns = sweep(body, va)
            if any(i.is_jump for i in insns) or len([i for i in insns if i.is_ret]) != 1:
                raise ValueError(f"{name} did not build as a straight-line wrapper")
            calls = [i for i in insns if i.is_call]
            if len(calls) != 1 or calls[0].branch_target is None:
                raise ValueError(f"{name} did not retain exactly one direct call")
            refs = [
                {"field_off": i.address - va + (i.disp_offset if i.rip_target is not None else i.imm_offset),
                 "next_off": i.end - va,
                 "target_elf": i.rip_target if i.rip_target is not None else i.branch_target}
                for i in insns if i.rip_target is not None or i.branch_target is not None
            ]
            symbols[name] = {"elf_va": va, "file_offset": section["sh_offset"] + offset,
                             "bytes": body.hex(), "size": len(body), "local": local,
                             "relocations": refs, "call_target": calls[0].branch_target}

        outer, worker = symbols["benchmark_start"], symbols[_WORKER]
        if outer["call_target"] != worker["elf_va"]:
            raise ValueError("benchmark_start did not retain a direct local call to the worker")
        plt = image.get_section_by_name(".plt.sec")
        stub_va = worker["call_target"]
        if not plt["sh_addr"] <= stub_va <= plt["sh_addr"] + plt["sh_size"] - 16:
            raise ValueError("worker clock call did not target the original-style PLT")
        stub_offset = stub_va - plt["sh_addr"]
        stub = plt.data()[stub_offset:stub_offset + 16]
        got = stub_va + 10 + int.from_bytes(stub[6:10], "little", signed=True)
        if stub[:6] != bytes.fromhex("f30f1efa ff25"):
            raise ValueError("worker clock PLT uses an unexpected binding mechanism")
        relocs = image.get_section_by_name(".rela.plt")
        dynsym = image.get_section(relocs["sh_link"])
        slot, = (r for r in relocs.iter_relocations() if r["r_offset"] == got)
        if slot["r_info_type"] != 7 or dynsym.get_symbol(slot["r_info_sym"]).name != "clock_gettime":
            raise ValueError("worker clock PLT does not dispatch to clock_gettime")

    return {
        "plugin_sha256": digest(plugin),
        "plugin": str(plugin),
        "functions": symbols,
        "clock_gettime_plt": {"elf_va": stub_va, "code": stub.hex(), "got_slot": got},
    }


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--work-dir", type=Path, required=True, help="fresh canary build and capture directory")
    cli.add_argument("--qemu", type=Path,
                     default=ROOT / "third_party/common/qemu/build/qemu-system-arm",
                     help="the existing source-locked QEMU executable")
    args = cli.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    log = work / "bootstrap.log"
    qemu = args.qemu.resolve()
    baseline_plugin = ROOT / "build" / "libfastdyn.so"
    baseline_hash = digest(baseline_plugin)
    source = ROOT / "virtuals" / "virtuals.c"
    source_hash = digest(source)
    plugin = compile_and_link(work, log)
    write_json(work / "depth2-build.json", {
        **audit(plugin),
        "baseline_plugin_sha256": baseline_hash,
        "baseline_source_sha256": source_hash,
        "canary_source_sha256": digest(work / "virtuals.c"),
        "compiler": output(["cc", "--version"]).splitlines()[0],
        "linker": output(["ld", "-v"]),
        "qemu_sha256": digest(qemu),
        "commands": str(log),
    })
    qualify_images(work, qemu, plugin, log)
    if digest(baseline_plugin) != baseline_hash or digest(source) != source_hash:
        raise ValueError("baseline source or plugin changed during canary preparation")


if __name__ == "__main__":
    main()
