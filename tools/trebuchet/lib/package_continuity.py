"""Build one direct-slot carrier variant for an exactly profiled translation."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import shutil

from scopt.canon import canonical_json, fnv1a64, sha256_id
from scopt.formats import artifact as artifact_format
from scopt.formats import scm_v1
from scopt.formats.profile_v1 import ProfileView, read_profile
from scopt.formats.sc_v5 import ScRecord
from scopt.formats.sc_v6 import (EXIT_DIRECT, KIND_SPEC, ExtTable, LinkEntry,
                                SlotEntry, VariantEntry, read_sc6, sc6_snapshot_id, sc6parse_compare, write_sc6)
from scopt.importer.import_cache import ImportCache, open_snapshot
from scopt.run.deploy import artifact_refusals, resolve_artifact
from scopt.link import TB_ALIGN, arena_limit, relocate_unit, round_up

from continuity_witness import check_execution, prove_edge, require


def make_variant(snap, profile, source_uid, slot, dest_uid, phase):
    view = ProfileView.open(profile, snap, phase=phase)
    pid = profile.phase_id(phase)
    require(view.same_snapshot and pid is not None and profile.phase_reached(pid),
            "training profile is not bound to this snapshot and phase")
    counts = [edge["count"] for edge in view.edges(phase) if edge["src"] == source_uid
              and edge["dst"] == dest_uid and edge["kind"] == f"chain{slot}"
              and edge["status"] == "exact" and not edge["flags"]]
    require(len(counts) == 1 and counts[0] > 0, "missing exact unflagged training edge")
    units = snap.units()
    require((snap.header.version, snap.header.reserved) == (7, 0)
            and len(units) == snap.header.record_count,
            "requires a canonical public-only v7 cache with all records present")
    source, dest = snap.unit(source_uid), snap.unit(dest_uid)
    require(source != dest and len(dest.vaddrs) == 1 and dest.reloc.relocatable
            and not any(u.native_fragments or u.stub_start is not None for u in (source, dest)),
            "source or private continuation is not eligible")
    suffix, prefix, old, new, report = prove_edge(snap, source_uid, slot, dest_uid)
    native_trials, asm_digest = check_execution(suffix, prefix, old)
    clone = relocate_unit(dest, round_up(max(u.end_offset for u in units), TB_ALIGN))
    rec = ScRecord.from_bytes(clone.record)
    off = report["reload_offset"]
    require(rec.end_offset < arena_limit(snap.header), "private variant exceeds cache arena")
    require(all(pos + width <= off or pos >= off + len(old) for pos, width in clone.patched)
            and clone.body[off:off + len(old)] == old, "replacement overlaps a relocation")
    body = clone.body[:off] + new + clone.body[off + len(old):]
    require(body[dest.tc_size:] == dest.search, "private search/recovery data changed")
    start = report["store_offset"]
    end = report["jump_offset"] + 6  # includes QEMU's reset entry
    source_slot = SlotEntry(source.tb_offset, slot, KIND_SPEC, EXIT_DIRECT, dest.vaddrs[0],
                            start, end - start, fnv1a64(source.code[start:end]))
    variant = VariantEntry(dest.tb_offset, rec.tb_offset, rec.tc_offset, rec.tc_size,
                           rec.end_offset, fnv1a64(body), 0)
    report.update(snapshot=snap.snapshot_id, profile=profile.profile_id, phase=phase,
                  observed_unflagged_slot_exits=counts[0], native_differential_trials=native_trials,
                  assembly_sha256=asm_digest, private_body_with_search_sha256=hashlib.sha256(body).hexdigest(),
                  private_tb_offset=rec.tb_offset, private_tc_offset=rec.tc_offset,
                  private_tc_size=rec.tc_size, private_end_offset=rec.end_offset,
                  private_fixups=len(clone.patched), public_record_count=len(units),
                  source_slot=slot, target_vaddr=dest.vaddrs[0], old=old.hex(), new=new.hex(),
                  runtime_requirement="qemu-sc6-dispatch-resolved-direct-slot")
    identity = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).digest()[:8]
    return ExtTable((variant,), (source_slot,), (LinkEntry(0, 0),), identity), body, report


def write_capture_evidence(path, snap, ext, report, guest_elf):
    """Preserve ordinary capture metadata and bind the one relocated variant."""
    source_dir = Path(snap.path)
    meta = json.loads((source_dir / "snapshot.json").read_bytes())
    capture_names = meta["files"]["captures"]
    require(capture_names, "missing source captures")
    v, = ext.variants
    patch = scm_v1.VariantPatch(report["reload_offset"], bytes.fromhex(report["old"]),
                                bytes.fromhex(report["new"]))
    key = hashlib.sha256(canonical_json(report)).digest()
    chunk = scm_v1.VariantChunk(
        v.variant_id, snap.unit(report["destination"]).slot, v.tb_offset, v.tc_offset,
        v.tc_size, v.end_offset, v.code_fnv64, snap.unit(report["source"]).slot,
        report["slot"], EXIT_DIRECT, key, (patch,))
    files = []
    for index, name in enumerate(capture_names):
        require(Path(name).name == name, "unsafe capture basename")
        target = path / ("cache.scm" if index == 0 else f"cache.{index}.scm")
        files.append(target)
        if index:
            shutil.copyfile(source_dir / name, target)
            continue
        scm = scm_v1.read_scm(source_dir / name)
        require(not scm.variants and not scm.rejected_chunks, "not an ordinary capture")
        bodies = [(c.type, c.body) for c in scm.chunks]
        insert = next((i for i, (_, b) in enumerate(bodies) if isinstance(b, scm_v1.EndChunk)),
                      len(bodies))
        bodies.insert(insert, (int(chunk.TYPE), chunk))
        scm_v1.write_scm(target, scm_v1.make_scm(bodies, scm.header))
    shutil.copyfile(source_dir / "manifest.json", path / "manifest.json")
    imported = ImportCache(path / "cache.sc", files, manifest=path / "manifest.json",
                           bundle=path / "bundle.json", runtime_identity=path / "runtime_identity.json",
                           firmware=guest_elf, plugin=meta["options"].get("plugin"),
                           use_manifest_firmware=False, verify_files=False).run()
    require(len(imported.variants) == 1 and imported.variants[0]["ok"],
            f"private capture failed re-import: {imported.variants}")
    for before, after in zip(snap.units(), imported.snapshot.units(), strict=True):
        require((before.uid, before.origin, before.meta_status, before.reloc, before.insns, before.sites) ==
                (after.uid, after.origin, after.meta_status, after.reloc, after.insns, after.sites),
                f"public origin, recovery or relocation metadata changed: {before.uid}")
    return imported.variants[0]


def publish(path, snap, ext, body, report, guest_elf):
    cache = path / "cache.sc"
    write_sc6(cache, snap.header, snap.units(), ext, [body])
    output = read_sc6(cache)
    require(output.problem is None and output.ext is not None and
            (output.ext.variants, output.ext.slots, output.ext.links, output.ext.artifact_id8) ==
            (ext.variants, ext.slots, ext.links, ext.artifact_id8) and
            output.variant_bodies == (body,), "private variant readback failed")
    require(tuple(output.sc.record_bytes) == tuple(u.record for u in snap.units()) and
            tuple(output.sc.bodies) == tuple(u.code + u.search for u in snap.units()),
            "public records, code or search data changed")
    agreed, detail, _ = sc6parse_compare(cache, output)
    require(agreed, f"independent SC6 parser disagrees: {detail}")
    require(snap.bundle and snap.runtime, "missing deployable identity documents")
    artifact_format.write_json_file(path / "bundle.json", dict(snap.bundle))
    artifact_format.write_json_file(path / "runtime_identity.json", dict(snap.runtime))
    report["derived_capture_reimport"] = write_capture_evidence(path, snap, ext, report, guest_elf)
    validation = artifact_format.write_json_file(path / "continuity.json", report)
    params = {k: report[k] for k in ("source", "slot", "destination", "phase")}
    sidecar = {
        "schema": "fastdyn.artifact_sidecar", "version": 1,
        "input_snapshot_id": snap.snapshot_id, "output_snapshot_id": sc6_snapshot_id(output),
        "bundle_id": snap.bundle["bundle_id"], "runtime_identity_id": snap.runtime["runtime_identity_id"],
        "profile_ids": [{"profile_id": report["profile"], "verdict": "compatible", "x_same_snapshot": True}],
        "pipeline": [{"pass": "direct-slot-carrier", "version": 1, "params_sha256": sha256_id(canonical_json(params)),
                      "outcome": "candidate", "reason": "", "affected": [report["source"], report["destination"]],
                      "edit_digest": sha256_id(body), "duration_s": 0.0}],
        "move_map": {}, "pinned": [], "tool_versions": artifact_format.tool_versions(),
        "loader_requirement": "qemu-sc-v7", "x_direct_slot_required": True,
        "x_ext_artifact_id8": ext.artifact_id8.hex(), "validation_sha256": sha256_id(validation),
    }
    errors = artifact_format.sidecar_errors(sidecar)
    require(not errors, f"invalid deployment sidecar: {errors}")
    artifact_format.write_json_file(path / "cache.sc.scopt.json", sidecar)
    refusals, _ = artifact_refusals(resolve_artifact(path), output.sc.header)
    require(not refusals, f"deployment refused: {refusals}")
    (path / "MANIFEST.sha256").write_bytes(artifact_format.manifest_bytes(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--guest-elf", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--slot", type=int, choices=(0, 1), default=0)
    parser.add_argument("--phase", default="steady")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    require(not args.out.exists(), "output directory already exists")
    snap = open_snapshot(args.snapshot)
    require(snap.bundle and sha256_id(args.guest_elf.read_bytes()) == snap.bundle["image_hash"],
            "guest ELF does not match the captured bundle")
    ext, body, report = make_variant(snap, read_profile(args.profile), args.source, args.slot,
                                     args.destination, args.phase)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.out.parent) as tmp:
        staged = Path(tmp) / "package"
        staged.mkdir()
        publish(staged, snap, ext, body, report, args.guest_elf)
        staged.rename(args.out)


if __name__ == "__main__":
    main()
