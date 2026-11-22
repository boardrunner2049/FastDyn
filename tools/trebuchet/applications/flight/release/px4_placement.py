"""Check a candidate against the FMUv6C board and the source hook bindings."""
import hashlib
import json
from pathlib import Path
import sys

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM
from elftools.elf.elffile import ELFFile


FLASH_START = 0x08020000
FLASH_END = 0x08200000


def inspect(path):
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        loads = [(s["p_paddr"], s["p_vaddr"], s["p_filesz"], s["p_memsz"], s["p_flags"], s.data())
                 for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        stream.seek(0)
        return loads, hashlib.file_digest(stream, "sha256").hexdigest()


def read(loads, address, size):
    for _, start, n, _, _, data in loads:
        if start <= address and address + size <= start + n:
            offset = address - start
            return data[offset:offset + size]
    raise ValueError(f"no loadable bytes at {address:#x}")


def examine(original, candidate, original_hooks, candidate_hooks):
    baseline, baseline_sha = inspect(original)
    changed, candidate_sha = inspect(candidate)
    regions = []
    for physical, virtual, size, memory, flags, _ in changed:
        if (physical, virtual, size, memory, flags) in [r[:5] for r in baseline]:
            continue
        bounded = (flags == 5 and FLASH_START <= physical == virtual and physical + max(size, memory) <= FLASH_END
                   and all(physical >= p + n or physical + size <= p for p, _, n, _, _, _ in baseline))
        regions.append({"start": physical, "end": physical + max(size, memory), "within_free_flash": bounded})
    pcs = set()
    configuration = {}
    for name in ("virtuals.txt", "modifiers.txt"):
        expected = (original_hooks / name).read_text()
        actual = candidate_hooks / name
        configuration[name] = actual.is_file() and expected == actual.read_text()
        pcs.update(int(row.split()[0], 16) for row in expected.splitlines() if row.strip())
    byte_changes = [p for p in sorted(pcs) if read(baseline, p, 4) != read(changed, p, 4)]
    vectors = read(baseline, FLASH_START, 64) == read(changed, FLASH_START, 64)
    originals_present = all(r[:5] in [c[:5] for c in changed] for r in baseline)
    return {"original_sha256": baseline_sha, "candidate_sha256": candidate_sha, "flash_start": FLASH_START,
            "flash_end": FLASH_END, "new_loads": regions, "original_load_placements_preserved": originals_present,
            "sixteen_core_vector_words_identical": vectors, "hook_files_identical": configuration,
            "hook_pc_count": len(pcs), "changed_first_four_hook_bytes": byte_changes,
            "admissible": bool(regions) and all(r["within_free_flash"] for r in regions)
            and originals_present and vectors and all(configuration.values()) and not byte_changes}


def verify_layout(original, candidate, layout_path):
    source, _ = inspect(original)
    output, _ = inspect(candidate)
    report = json.loads(layout_path.read_text())
    moves = []
    redirects = set()
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    decoder.detail = True
    if report["target_cpu"] != "cortex-m7":
        raise ValueError("layout CPU differs from the PX4 board")
    for function in report["functions"]:
        if not function["firmware_modified"]:
            continue
        start = function["original_address"]
        target = function["output_address"]
        end = target + function["output_size"]
        if not FLASH_START <= target < end <= FLASH_END:
            raise ValueError("layout leaves the declared flash")
        insn = next(decoder.disasm(read(output, start, 4), start, 1), None)
        if (insn is None or insn.mnemonic != "b.w" or insn.size != 4
                or len(insn.operands) != 1 or insn.operands[0].type != ARM_OP_IMM
                or insn.operands[0].imm != target):
            raise ValueError(f"missing exact entry redirect for {function['name']}")
        for origin in function["instruction_origins"]:
            expected = bytes.fromhex(origin["linked_bytes_hex"])
            address = origin["output_address"]
            if (len(expected) != origin["output_size"] or not target <= address < end
                    or address + len(expected) > end or read(output, address, len(expected)) != expected):
                raise ValueError(f"linked instruction differs from layout at {address:#x}")
        redirects.update(range(start, start + 4))
        moves.append({"name": function["name"], "source": start, "start": target, "end": end})
    if not moves:
        raise ValueError("layout contains no verified transformed code")
    for _, start, size, _, _, original_bytes in source:
        if not size:
            continue
        updated = read(output, start, size)
        if any(start + i not in redirects for i, (a, b) in enumerate(zip(original_bytes, updated)) if a != b):
            raise ValueError("unexpected change to an original load outside the verified entry redirects")
    return moves


def executed(profile, moves):
    starts = {}
    with profile.open() as lines:
        for line in lines:
            row = line.split()
            if row and row[0] == "TB":
                pc = int(row[1], 16)
                if any(move["start"] <= pc < move["end"] for move in moves):
                    starts[pc] = starts.get(pc, 0) + int(row[3])
    if not sum(starts.values()):
        raise ValueError("training mission did not execute any transformed translation block")
    return {"distinct_transformed_tb_starts": len(starts), "transformed_tb_executions": sum(starts.values())}


if __name__ == "__main__":
    result = examine(*(Path(value) for value in sys.argv[1:]))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["admissible"] else 1)
