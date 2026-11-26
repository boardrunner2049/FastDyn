"""Emit and execute a training-only witness for one direct Thumb TB chain."""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from scopt.analysis.envlayout import envlayout_for_runtime
from scopt.analysis.hcfg import build_hcfg
from scopt.analysis.hostops import OpKind, build_hostops
from scopt.analysis.obs_contract import D1_FIELDS
from scopt.formats.enums import SiteKind
from scopt.formats.profile_v1 import ProfileView, read_profile
from scopt.formats.sc_records import ScRecord
from scopt.importer.import_cache import open_snapshot


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def prove_window(suffix, prefix, reload):
    """Admit plain CPUArchState accesses with one known PC update and one IRQ check."""
    require(len(reload) == 3 and reload[:2] == bytes.fromhex("8b5d"),
            "requires a 32-bit reload to ebx with disp8")
    disp = reload[2]
    require(disp <= 56 and disp % 4 == 0, "requires a general ARM register")
    store = bytes.fromhex("44896d") + bytes([disp])
    require(suffix.startswith(store + bytes.fromhex("c645fc01 8b5d3c 83c3")),
            "predecessor store or post-store sequence changed")
    require(suffix[14:17] == bytes.fromhex("895d3c") and
            suffix[17:] in (b"", b"\x90", bytes.fromhex("6690"), bytes.fromhex("666690")),
            "post-store path can clobber the value or access another field")
    require(len(prefix) == 22 and prefix[:5] == bytes.fromhex("8b5df8 83c3") and
            prefix[6:10] == bytes.fromhex("85db 0f8c") and
            prefix[14:] == bytes.fromhex("66895df8 c645fc00"),
            "unknown successor prefix, observation or carrier clobber")
    return bytes.fromhex("4489eb")  # mov ebx, r13d; flags preserved, zero extends.


def prove_edge(snapshot, source_uid, slot, dest_uid):
    layout = envlayout_for_runtime(snapshot.runtime)
    require(layout["bound"], "requires runtime-matched env layout")
    source, dest = snapshot.unit(source_uid), snapshot.unit(dest_uid)
    require(all(u.meta_status == "ok" and u.origin == "elf_verified" for u in (source, dest)),
            "requires captured host metadata and guest instruction provenance")
    sh, dh = (build_hostops(u, layout["layout"], snapshot.header.code_address)
              for u in (source, dest))
    sg, dg = build_hcfg(sh), build_hcfg(dh)
    require(sh.ok and dh.ok and sg.ok and dg.ok, "unknown host CFG")
    spaths = sg.paths()
    require(spaths and dg.paths(), "unbounded or empty atomic paths")
    spaths = [p for p in spaths if sg.exit_kind(p[-1]) == "goto_tb:" + str(slot)]
    require(spaths, "no paths to the selected chain exit")
    reload = next((op for op in dh.ops if op.off == 22), None)
    require(reload and reload.kind == OpKind.ENV_LOAD and reload.dst_reg == "ebx"
            and len(reload.env) == 1 and reload.env[0].exact and reload.env[0].width == 4
            and reload.env[0].field in D1_FIELDS, "unqualified successor reload")
    arrivals = []
    for path in dg.paths():
        offsets = [dh.ops[index].off for index in path]
        if reload.off in offsets:
            arrivals.append(offsets[:offsets.index(reload.off)])
    require(arrivals and all(p == [0, 3, 6, 8, 14, 18] for p in arrivals),
            "private reload reachable without the certified prefix")
    ends = {sh.ops[path[-1]].off for path in spaths}
    require(len(ends) == 1, "ambiguous selected exit")
    jump = ends.pop()
    witnesses = []
    for path in spaths:
        stores = [sh.ops[i] for i in path if sh.ops[i].kind == OpKind.ENV_STORE_REG
                  and sh.ops[i].src_reg == "r13d" and len(sh.ops[i].env) == 1
                  and sh.ops[i].env[0].exact and sh.ops[i].env[0].width == 4
                  and sh.ops[i].env[0].field == reload.env[0].field]
        require(stores, "a path reaches the chain without establishing the carrier")
        witnesses.append(stores[-1].off)
    require(len(set(witnesses)) == 1, "paths disagree on the reaching store")
    store = witnesses[0]
    record = ScRecord.from_bytes(source.record)
    require(record.jmp_insn_offset[slot] == jump + 1 and record.jmp_reset_offset[slot] == jump + 5
            and (source.tc_offset + jump + 1) % 4 == 0 and
            source.code[jump:jump + 5] == bytes.fromhex("e900000000") and
            any(s.kind == SiteKind.GOTO_TB and s.insn_off == jump and s.aux == slot
                for s in source.sites), "missing mutable chain/reset record")
    suffix, prefix, original = source.code[store:jump], dest.code[:reload.off], dest.code[reload.off:reload.end]
    replacement = prove_window(suffix, prefix, original)
    bailout = 14 + struct.unpack_from("<i", prefix, 10)[0]
    require(bailout >= reload.end and any(s.kind == SiteKind.MOVI_EXIT_TB_PTR and
            s.insn_off == bailout for s in dest.sites), "missing original icount bailout")
    return suffix, prefix, original, replacement, {
        "source": source_uid, "slot": slot, "destination": dest_uid, "store_offset": store,
        "jump_offset": jump, "reload_offset": reload.off, "bailout_offset": bailout,
        "source_path_count": len(spaths), "field": reload.env[0].field,
        "source_body_sha256": hashlib.sha256(source.code).hexdigest(),
        "public_body_sha256": hashlib.sha256(dest.code).hexdigest(),
        "private_body_at_input_address_sha256": hashlib.sha256(
            dest.code[:reload.off] + replacement + dest.code[reload.end:]).hexdigest(),
    }


def emit(suffix, prefix, reload):
    """Assemble closed execution windows. The external jump is represented by fall-through."""
    def db(data):
        return ".byte " + ",".join(str(b) for b in data)

    bodies = []
    for name, lead, load in (("reference", suffix, reload), ("private_chain", suffix, prove_window(suffix, prefix, reload)),
                             ("public_entry", b"", reload)):
        bodies.append(f"""
.globl {name}
.type {name},@function
{name}:
    push %rbp
    push %rbx
    push %r13
    mov %rdi,%rbp
    mov %esi,%r13d
    mov %ecx,%ebx
    {db(lead) if lead else ""}
    {db(prefix[:10])}
    .long .Learly_{name} - (. + 4)
    {db(prefix[14:])}
    {db(load)}
    mov $1,%eax
    jmp .Ldone_{name}
.Learly_{name}:
    mov $0,%eax
.Ldone_{name}:
    mov %rbx,(%rdx)
    pushfq
    pop %rcx
    mov %rcx,8(%rdx)
    mov %r13,16(%rdx)
    pop %r13
    pop %rbx
    pop %rbp
    ret
.size {name},.-{name}
""")
    return ".text\n" + "".join(bodies) + '.section .note.GNU-stack,"",@progbits\n'


def check_execution(suffix, prefix, reload):
    assembly = emit(suffix, prefix, reload)
    with tempfile.TemporaryDirectory() as tmp:
        src, lib = Path(tmp) / "pair.S", Path(tmp) / "pair.so"
        src.write_text(assembly)
        subprocess.run(["cc", "-shared", "-fPIC", "-Wl,-z,noexecstack", "-o", lib, src], check=True)
        dll = ctypes.CDLL(str(lib))

        def run(name, ticks, value):
            state = bytearray(range(96))
            struct.pack_into("<I", state, 16 - 8, ticks)
            initial = value ^ 0xffffffff
            struct.pack_into("<I", state, 16 + reload[2], initial)
            buf = (ctypes.c_ubyte * len(state)).from_buffer(state)
            output = (ctypes.c_uint64 * 3)()
            fn = getattr(dll, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
            fn.restype = ctypes.c_uint
            normal = fn(ctypes.byref(buf, 16), value, output, 0x12345678)
            return normal, bytes(state), tuple(output)

        trials = 0
        paths = set()
        for ticks in (0, 1, 2, 3, 65535, 0x80000000, 0xffffffff):
            for value in (0, 1, 0x7fffffff, 0x80000000, 0xdeadbeef, 0xffffffff):
                ref = run("reference", ticks, value)
                private = run("private_chain", ticks, value)
                require(private == ref, "private entry differs from original chain window")
                normal, state, regs = private
                require(int.from_bytes(state[16+reload[2]:20+reload[2]], "little") == value
                        and regs[2] == value, "state materialization lost at the boundary")
                if normal:
                    require(regs[0] == value and run("public_entry", ticks, value)[2][0] ==
                            value ^ 0xffffffff, "independent public entry used stale carrier")
                paths.add(normal)
                trials += 1
        require(paths == {0, 1}, "missing normal or early-exit differential")
    return trials, hashlib.sha256(assembly.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("profile", type=Path)
    ap.add_argument("--guest-elf", type=Path, help="reimport physical captures for an archived diagnostic")
    ap.add_argument("--source", required=True)
    ap.add_argument("--destination", required=True)
    ap.add_argument("--slot", required=True, type=int, choices=(0, 1))
    ap.add_argument("--phase", default="steady")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.guest_elf:
        from archived_snapshot import archived_ordinary
        snap, binding = archived_ordinary(args.snapshot, args.guest_elf)
    else:
        snap, binding = open_snapshot(args.snapshot), {"mode": "strict"}
    profile = read_profile(args.profile)
    view = ProfileView.open(profile, snap, phase=args.phase)
    require(view.same_snapshot and profile.phase_id(args.phase) is not None and
            profile.phase_reached(profile.phase_id(args.phase)), "unmatched training profile")
    counted = [e for e in view.edges(args.phase) if e["src"] == args.source and
               e["dst"] == args.destination and e["kind"] == "chain" + str(args.slot)
               and e["status"] == "exact" and not e["flags"]]
    require(len(counted) == 1 and counted[0]["count"] > 0, "no exact unflagged edge record")
    suffix, prefix, initial, changed, report = prove_edge(snap, args.source, args.slot, args.destination)
    trials, digest = check_execution(suffix, prefix, initial)
    report.update(snapshot=snap.snapshot_id, profile=profile.profile_id, phase=args.phase,
                  observed_unflagged_slot_exits=counted[0]["count"], binding=binding,
                  original_reload=initial.hex(), private_copy=changed.hex(), host_bytes_delta=0,
                  predecessor_suffix=suffix.hex(), successor_prefix=prefix.hex(),
                  native_differential_trials=trials, assembly_sha256=digest,
                  scope="closed direct-chain execution window; public and icount-bailout entries retained")
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
