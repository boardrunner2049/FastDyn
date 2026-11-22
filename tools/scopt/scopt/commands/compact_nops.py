"""Build a training-only ordinary v5 cache with one compacted direct-exit TB."""
import json
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import click

from ..compact_nops import Ineligible, compact
from ..formats.sc_v5 import read_sc, write_sc
from ..formats.scm_v1 import TbChunk, encode_scm
from ..importer.classify import context_for
from ..importer.crosscheck import crosscheck_unit
from ..importer.join import join


def register(group):
    group.add_command(compact_nops)


def generate(snapshot, profile_path, out):
    sc = read_sc(snapshot / "cache.sc")
    if not sc.loadable or sc.problems or sc.skipped:
        raise ValueError("requires a clean ordinary v5 input with all record slots intact")
    meta = json.loads((snapshot / "snapshot.json").read_text())
    profile = json.loads(profile_path.read_text())
    sid = sc.snapshot_id()
    if (sid != meta["snapshot_id"] or sid != profile["snapshot_id"]
            or not profile["collection"]["complete"] or profile["collection"]["overflow"]):
        raise ValueError("requires a complete profile for this exact ordinary input")
    phases = [str(p["id"]) for p in profile["phases"] if p["name"] == "steady" and p["reached"]]
    if len(phases) != 1:
        raise ValueError("requires one reached steady phase")
    counts = {r["unit"]: r["phase"][phases[0]]["entries"] for r in profile["units"] if r["status"] == "exact"}
    captures = [snapshot / name for name in meta["files"]["captures"]]
    bindings = join(sc, captures)
    units = list(sc.units())
    identifiers = sc.unit_ids()
    slots = sorted(range(len(units)), key=lambda i: (-counts.get(identifiers[i], 0), identifiers[i]))
    ctxs, skips = {}, Counter()
    selected = None
    for slot in slots:
        uid = identifiers[slot]
        if counts.get(uid, 0) <= 0:
            break
        src = units[slot]
        binding = bindings.slots[slot]
        if not binding.bound:
            skips["unbound"] += 1
            continue
        try:
            changed, chunk, plan = compact(src, binding.chunk, sc.header.code_address)
        except Ineligible as exc:
            skips[str(exc)] += 1
            continue
        ctx = context_for(binding.source, ctxs)
        checks = [crosscheck_unit(item, capture, sc.header, ctx,
                                  icache_linesize=binding.source.run_info.icache_linesize)
                  for item, capture in [(src, binding.chunk), (changed, chunk)]]
        if any(c.meta_status != "ok" or c.reasons or c.recovery for c in checks):
            skips["input or output crosscheck failed or has exceptional recovery points"] += 1
            continue
        lo = sc.header.code_address + src.record.tc_offset
        hi = sc.header.code_address + src.record.end_offset
        if any(not b.bound or any(lo < ref.value < hi for ref in b.chunk.sites)
               or any(lo < entry.data0 < hi for entry in b.chunk.pool)
               for other, b in bindings.slots.items() if other != slot):
            skips["unresolved or incoming references from other records"] += 1
            continue
        selected = (slot, uid, changed, chunk, plan)
        break
    if selected is None:
        raise ValueError(f"no eligible observed unit: {dict(skips)}")
    slot, uid, changed, chunk, plan = selected
    source = units[slot]
    units[slot] = changed
    if out.exists():
        raise ValueError("output path exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp) / "candidate"
        stage.mkdir()
        write_sc(stage / "cache.sc", sc.header, units)
        paths = []
        for index, file in enumerate(bindings.captures):
            rows = tuple(replace(row, body=chunk) if file is bindings.slots[slot].source
                         and isinstance(row.body, TbChunk) and row.body is bindings.slots[slot].chunk
                         else row for row in file.chunks)
            dest = stage / ("cache.scm" if index == 0 else f"cache.{index}.scm")
            dest.write_bytes(encode_scm((file.header, rows)))
            paths.append(dest)
        result = read_sc(stage / "cache.sc")
        rebound = join(result, paths)
        if (result.problems or result.unit_ids() != identifiers
                or not rebound.slots[slot].bound or rebound.slots[slot].chunk.record != changed.record_bytes
                or any(rebound.slots[i].bound != b.bound for i, b in bindings.slots.items())):
            raise ValueError("written records or derived captures failed reimport")
        report = dict(schema="scopt.nop-compaction/1", status="built", input_snapshot=sid,
                      output_snapshot=result.snapshot_id(), profile_id=profile["profile_id"], unit=uid,
                      slot=slot, guest_pc=hex(chunk.vaddr), phase_entries=counts[uid],
                      branch_off=plan.branch, store_off=plan.store, removed_nop_off=plan.store + 3,
                      old_code=source.code.hex(), new_code=changed.code.hex(),
                      old_search=source.search.hex(), new_search=changed.search.hex(),
                      code_bytes_saved=source.record.tc_size - changed.record.tc_size,
                      old_slot_fields=source.record.jmp_insn_offset, new_slot_fields=changed.record.jmp_insn_offset,
                      search_rows=len(chunk.insns), exceptional_recovery_points=0, higher_ranked_skips=dict(skips))
        (stage / "compaction.json").write_text(json.dumps(report, indent=2) + "\n")
        stage.rename(out)
    return report


@click.command("compact-nops")
@click.argument("snapshot", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--profile", "profile_path", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path(path_type=Path, file_okay=False))
def compact_nops(snapshot, profile_path, out):
    """Shrink one training-observed direct exit and emit a cache with derived captures."""
    try:
        doc = generate(snapshot, profile_path, out)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(doc))
