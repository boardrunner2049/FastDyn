"""Recheck the physical original Pye TB of the recorded historical BH refusal."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lib"))
from archived_snapshot import archived_ordinary
from scopt.analysis.obs_contract import build_contract
from scopt.analysis.syncdead import nop_bytes, syncdead_of
from scopt.decode.x86 import sweep
from scopt.validate_sym import semantics
from scopt.formats.envlayout_v1 import find_envlayout
from scopt.passes.eliminate_state_sync import strict_contract
from scopt.validate_sym.equiv import check_equiv
from scopt.validate_sym.program import program_of, world_of

UID = "tb:607159ac1695f640757962753fa2a1ce#0"


def witness(offline):
    image = offline / "product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
    root = offline / "campaign"
    os.environ.update(SCOPT_ENVLAYOUT_PATH=str(root / "envlayout"), SCOPT_HELPER_EFFECTS_PATH=str(root / "helper-effects"))
    runtime = json.loads((root / "original/admission-runtime.json").read_text())
    store, = (root / "original/store/snapshots").iterdir()
    snapshot, archive = archived_ordinary(store, image)
    unit = snapshot.unit(UID)
    lay = find_envlayout(runtime["qemu_sha256"])
    world = world_of(snapshot, lay)
    contract = build_contract(lay, runtime)
    proof = syncdead_of(unit, lay, world.code_address, contract)
    if not proof.ok or not proof.dead:
        raise ValueError(f"no applicable B4 patch on archived TB: {proof.reason}")
    original = program_of(unit, world)
    body = bytearray(unit.code)
    for row in proof.dead:
        if body[row.off:row.off+row.size] != row.old:
            raise ValueError("patch differs from physical host instruction")
        body[row.off:row.off+row.size] = nop_bytes(row.size)
    updated = program_of(replace(unit, code=bytes(body)), world)
    start = time.monotonic()
    result = check_equiv(original, updated, contract=strict_contract(contract), layout=lay)
    elapsed = time.monotonic() - start
    insn, = [op for op in sweep(unit.code, 0) if op.address == 0x90]
    if insn.raw != bytes.fromhex("0fb6df"):
        raise ValueError("expected MOVZX EBX,BH at the historical refusal")
    mutant = bytearray(body)
    mutant[0x92] = 0xdb  # MOVZX EBX,BL reads bits 7:0 rather than 15:8.
    negative = check_equiv(original, program_of(replace(unit, code=bytes(mutant)), world),
                           contract=strict_contract(contract), layout=lay)
    return {"scope": "isolated decoded physical TB only",
            "semantics_sha256": hashlib.sha256(Path(semantics.__file__).read_bytes()).hexdigest(),
            "instruction": {"offset": insn.address, "bytes": insn.raw.hex(), "mnemonic": insn.mnemonic,
                            "operands": insn.op_str}, "snapshot_id": snapshot.snapshot_id,
            "archive": archive, "unit": UID, "old_code_sha256": hashlib.sha256(unit.code).hexdigest(),
            "new_code_sha256": hashlib.sha256(body).hexdigest(), "patches": [v.to_json() for v in proof.dead],
            "verdict": result.to_json(), "low_byte_mutant": negative.to_json(), "check_seconds": elapsed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("offline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = witness(args.offline.resolve())
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(report["verdict"])
