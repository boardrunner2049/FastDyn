"""Read-only physical census input when original host ELF paths are unavailable."""
from dataclasses import replace
import json
from pathlib import Path

from scopt.canon import sha256_id
from scopt.ids import region_id
from scopt.importer.import_cache import ImportCache


def archived_ordinary(path, guest_elf):
    """Reimport exact v5 bytes and SCMs and compare all physical region fields.

    Only offline diagnostics use this interface. Host symbol resolution and
    relocation eligibility can change when the captured executable paths no
    longer exist. Never reuse this view for linking or validation of an artifact.
    """
    path = Path(path)
    stored = json.loads((path / "snapshot.json").read_bytes())
    bundle = json.loads((path / "bundle.json").read_bytes())
    if sha256_id(Path(guest_elf).read_bytes()) != bundle.get("image_hash"):
        raise ValueError("archived guest ELF differs from the captured bundle")
    files = stored["files"]
    captures = files["captures"]
    if not captures or any(Path(n).name != n for n in captures):
        raise ValueError("expected named, local SCM files in exported snapshot")
    firmware = guest_elf if stored["options"].get("firmware") else None
    # All bytes are hashed again by ImportCache. It also checks the original
    # manifest, content IDs, SCM field bindings and guest origins.
    result = ImportCache(path / "cache.sc", [path / n for n in captures],
                         manifest=path / "manifest.json", bundle=bundle,
                         runtime_identity=path / "runtime_identity.json", firmware=firmware,
                         use_manifest_firmware=False, verify_files=False).run()
    snap = result.snapshot
    if snap.header.version != 5 or any(u.native_fragments or u.stub_start is not None for u in snap.units()):
        raise ValueError("archived census requires an ordinary v5 input")
    for key in ("snapshot_id", "header", "records", "bundle_id", "contract_id", "runtime_identity_id"):
        if result.summary[key] != stored[key]:
            raise ValueError(f"archived snapshot differs in {key}")
    if result.summary["capture"]["files"] != stored["capture"]["files"]:
        raise ValueError("archived capture digests differ")
    old = [json.loads(line) for line in (path / "units.jsonl").read_bytes().splitlines()]
    if len(old) != len(result.regions):
        raise ValueError("archived census has different unit count")
    different = 0
    descriptive = {"region_id", "relocations", "bindings", "status", "pin_reasons", "unknowns"}
    for original, fresh in zip(old, result.regions):
        if region_id(original) != original["region_id"]:
            raise ValueError("archived region identity is invalid")
        # This compares native code+search SHA, partitions, guest origins,
        # metadata status, instructions, exits, events and recovery offsets.
        if {k: v for k, v in original.items() if k not in descriptive} != {
                k: v for k, v in fresh.items() if k not in descriptive}:
            raise ValueError(f"archived physical region differs: {original['members'][0]['unit']}")
        different += original != fresh
    return replace(snap, path=str(path)), {"mode": "archived_ordinary",
                                          "units_checked": len(old),
                                          "units_with_different_host_annotations": different}


def archive_function_footprints(snap, exe, calls):
    """Costs from hash-matched archived QEMU bytes. Never issue splice certificates."""
    import io
    from elftools.elf.elffile import ELFFile
    from scopt.analysis.native_callees import _verified_captures
    from scopt.decode.symbols import read_elf
    from scopt.decode.x86 import sweep
    from scopt.importer.classify import HostContext
    from scopt.importer.native_callee import CalleeResolutionError, NativeCalleeResolver

    data = Path(exe).read_bytes()
    digest = sha256_id(data)
    if not snap.runtime or digest != snap.runtime["qemu_sha256"]:
        raise ValueError("archived executable differs from the native runtime digest")
    source = io.BytesIO(data)
    elf = ELFFile(source)
    if elf.elfclass != 64 or not elf.little_endian or elf["e_machine"] != "EM_X86_64":
        raise ValueError("archived function disassembly requires x86-64 QEMU")
    source.seek(0)
    image = read_elf(source, str(exe))
    joined, ids = _verified_captures(snap)
    by_uid = {u.uid: joined.slots[u.slot].source for u in snap.units()
              if u.slot in joined.slots and joined.slots[u.slot].bound}
    contexts, addresses = {}, {}
    for row in calls:
        call = row.get("call") or {}
        cap = by_uid.get(row["body"])
        if cap is None or cap.run_info is None or not row["physical_call_verified"] or call.get("reason"):
            continue
        addr, name = call.get("target_abs"), call.get("helper_name")
        if addr is None or not name:
            continue
        if cap.path not in contexts:
            ctx = HostContext.from_capture(cap)
            resolver = NativeCalleeResolver(ctx, cap.run_info, snap.runtime, ids[cap.path])
            contexts[cap.path] = ctx, resolver
        ctx, resolver = contexts[cap.path]
        key = (cap.path, call.get("helper_id"), addr, name)
        if key not in addresses:
            addresses[key] = None
            mapping = ctx.maps.find(addr)
            helper = ctx.helpers.get(call["helper_id"])
            if (not mapping or not mapping.executable or mapping.perms[1:2] == "w" or not helper
                    or (helper.func, helper.name_str, helper.tcg_flags) != (addr, name, call.get("helper_flags"))):
                continue
            try:
                role, expected, _ = resolver._verify_binding(mapping)
            except CalleeResolutionError:
                continue
            if role != "qemu" or expected != digest:
                continue
            bias = ctx.maps.load_bias(mapping.path, image)
            if bias is None:
                continue
            va = addr - bias
            symbol = image.bounded_function("helper_" + name, va) or image.bounded_function(name, va)
            segment = image.segment_for(va)
            if not symbol or not segment or segment.flags & 3 != 1:
                continue
            off = segment.offset + va - segment.vaddr
            if (off != mapping.offset + addr - mapping.start or addr + symbol.size > mapping.end
                    or off + symbol.size > min(len(data), segment.offset + segment.filesz)):
                continue
            code = data[off:off + symbol.size]
            insns = list(sweep(code, addr))
            addresses[key] = {"symbol": symbol.name, "elf_address": va, "file_offset": off,
                              "static_bytes": len(code), "code_hash": sha256_id(code),
                              "linear_instructions": None if any(i.bad for i in insns) else len(insns)}
        if addresses[key] is not None:
            row["cost_proxy"]["archived_function"] = addresses[key]
    return digest
