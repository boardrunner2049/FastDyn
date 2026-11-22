"""Inventory Thumb dispatch instructions and allocated-section relocations in ELFs."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection


FAMILIES = ("tbb", "tbh", "indexed_pc_load", "adjacent_adr_indexed_pc_load",
            "thumb1_case_helper_call")
LABEL = re.compile(r"^[0-9a-f]+ <(.+)>:$")
INSTRUCTION = re.compile(r"^\s*([0-9a-f]+):\s+([a-z][a-z.0-9]*)\s+(.*)$")
INDEXED = re.compile(r"pc,\s*\[(\w+),\s*\w+,\s*lsl #2\]$")


def training_profile(directory, image_hash):
    receipt = json.loads((directory / "result.json").read_text())
    data = (directory / "session.tbprof").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if (
        receipt["verdict"] != "PASS"
        or receipt["exit_code"] != 0
        or receipt["application"]["verdict"] != "PASS"
        or receipt["application"]["role"] != "training"
        or receipt["elf_sha256"] != image_hash
        or receipt["profile_sha256"] != digest
    ):
        raise ValueError(f"{directory}: mismatched training receipt")
    counts = Counter()
    for line in data.splitlines():
        fields = line.split()
        if fields and fields[0] == b"TB":
            counts[int(fields[5], 16)] += int(fields[3])
    return counts, {"profile_sha256": digest, "case": receipt["case"],
                    "scope": receipt["diagnostic"], "total_tb_entries": sum(counts.values())}


def inspect_dispatch(image, objdump, profile_counts):
    counts = dict.fromkeys(FAMILIES, 0)
    examples = {name: [] for name in FAMILIES}
    observed = {name: [] for name in FAMILIES}
    owner, previous = None, None
    with subprocess.Popen([objdump, "-d", "-w", "--no-show-raw-insn", str(image)],
                          stdout=subprocess.PIPE, text=True) as process:
        for line in process.stdout:
            label = LABEL.match(line)
            if label:
                owner, previous = label[1], None
            instruction = INSTRUCTION.match(line)
            if not instruction:
                previous = None
                continue
            pc, mnemonic, raw = instruction.groups()
            operands = raw.split(";", 1)[0].strip()
            hits = []
            if mnemonic in ("tbb", "tbh"):
                hits.append(mnemonic)
            indexed = INDEXED.fullmatch(operands)
            if indexed and mnemonic in ("ldr", "ldr.w"):
                hits.append("indexed_pc_load")
                if previous:
                    op, args = previous
                    base = indexed[1]
                    if ((op in ("adr", "adr.w") and args.startswith(base + ","))
                        or (op in ("add", "addw", "add.w") and
                            re.match(rf"{base},\s*pc,\s*#", args))):
                        hits.append("adjacent_adr_indexed_pc_load")
            if mnemonic in ("bl", "bl.w") and "<__gnu_thumb1_case_" in operands:
                hits.append("thumb1_case_helper_call")
            for hit in hits:
                address = int(pc, 16)
                counts[hit] += 1
                if profile_counts is not None and address in profile_counts:
                    observed[hit].append(dict(pc=hex(address), symbol=owner,
                                               tb_entries=profile_counts[address]))
                if len(examples[hit]) < 3:
                    examples[hit].append(dict(pc=hex(address), symbol=owner,
                                              instruction=f"{mnemonic} {operands}"))
            previous = mnemonic, operands
        if process.wait():
            raise RuntimeError(f"{objdump} failed on {image}")
    result = dict(counts=counts, examples={k: v for k, v in examples.items() if v})
    if profile_counts is not None:
        result["observed_tb_terminators"] = {k: v for k, v in observed.items() if v}
    return result


def inspect_image(image, objdump, profile=None):
    result = {"sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        if elf.header.e_machine != "EM_ARM":
            raise ValueError(f"{image} is not an ARM ELF")
        result["entry"] = hex(elf.header.e_entry)
        sections = []
        relocs = Counter()
        for section in elf.iter_sections():
            if section["sh_flags"] & 6 == 6:
                sections.append(dict(name=section.name, address=hex(section["sh_addr"]),
                                     size=section.data_size,
                                     sha256=hashlib.sha256(section.data()).hexdigest()))
            if isinstance(section, RelocationSection):
                target = elf.get_section(section["sh_info"])
                if target["sh_flags"] & 2:
                    relocs[target.name] += section.num_relocations()
        result.update(executable_sections=sections, allocated_target_relocations=dict(relocs))
    profile_counts = None
    if profile:
        profile_counts, result["training"] = training_profile(profile, result["sha256"])
    result.update(inspect_dispatch(image, objdump, profile_counts))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objdump", default="arm-none-eabi-objdump")
    parser.add_argument("--profile", nargs=2, metavar=("NAME", "DIRECTORY"), action="append", default=[])
    parser.add_argument("images", nargs="+", metavar="NAME=ELF")
    args = parser.parse_args()
    profiles = {name: Path(directory) for name, directory in args.profile}
    if len(profiles) != len(args.profile):
        parser.error("provide a single training profile per image name")
    images = {}
    for item in args.images:
        name, sep, path = item.partition("=")
        if not sep or not name or not path or name in images:
            parser.error("provide each image as a unique NAME=ELF")
        images[name] = inspect_image(Path(path), args.objdump, profiles.pop(name, None))
    if profiles:
        parser.error("profile name does not identify an input ELF")
    tool = subprocess.check_output([args.objdump, "--version"], text=True).splitlines()[0]
    print(json.dumps(dict(disassembler=tool, images=images), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
