"""Build a training-only ordinary v5 cache with bounded direct-exit TB compaction."""
import json
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import click

from ..compact_nops import Ineligible, compact
from ..formats.profile_v1 import check_flow, read_profile
from ..formats.sc_v5 import read_sc, write_sc
from ..formats.scm_v1 import encode_scm
from ..importer.classify import context_for
from ..importer.crosscheck import crosscheck_unit
from ..importer.join import join


def register(group):
    group.add_command(compact_nops)


def generate(snapshot, profile_path, out, *, max_units=1):
    if max_units < 1:
        raise ValueError("max_units must be positive")
    sc = read_sc(snapshot / "cache.sc")
    if not sc.loadable or sc.problems or sc.skipped:
        raise ValueError("requires a clean ordinary v5 input with all record slots intact")
    meta = json.loads((snapshot / "snapshot.json").read_text())
    verified = read_profile(profile_path)
    profile = verified.doc
    sid = sc.snapshot_id()
    if (sid != meta["snapshot_id"] or sid != profile["snapshot_id"]
            or any(profile[k] != meta[k] for k in ("bundle_id", "runtime_identity_id", "contract_id"))
            or not profile["collection"]["complete"] or profile["collection"]["overflow"]
            or not check_flow(verified).ok):
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
    selected = {}
    rows_by_slot = []
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
        record = dict(unit=uid,
                      slot=slot, guest_pc=hex(chunk.vaddr), phase_entries=counts[uid],
                      branch_off=plan.branch, store_off=plan.store, removed_nop_off=plan.store + 3,
                      old_code=src.code.hex(), new_code=changed.code.hex(),
                      old_search=src.search.hex(), new_search=changed.search.hex(),
                      code_bytes_saved=src.record.tc_size - changed.record.tc_size,
                      old_slot_fields=src.record.jmp_insn_offset, new_slot_fields=changed.record.jmp_insn_offset,
                      search_rows=len(chunk.insns), exceptional_recovery_points=0, higher_ranked_skips=dict(skips))
        selected[slot] = chunk
        rows_by_slot.append(record)
        units[slot] = changed
        if len(selected) == max_units:
            break
    if not selected:
        raise ValueError(f"no eligible observed unit: {dict(skips)}")
    if out.exists():
        raise ValueError("output path exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp) / "candidate"
        stage.mkdir()
        write_sc(stage / "cache.sc", sc.header, units)
        paths = []
        replacements = {id(bindings.slots[i].chunk): chunk for i, chunk in selected.items()}
        for index, file in enumerate(bindings.captures):
            rows = tuple(replace(row, body=replacements[id(row.body)]) if id(row.body) in replacements
                         else row for row in file.chunks)
            dest = stage / ("cache.scm" if index == 0 else f"cache.{index}.scm")
            dest.write_bytes(encode_scm((file.header, rows)))
            paths.append(dest)
        result = read_sc(stage / "cache.sc")
        rebound = join(result, paths)
        if (result.problems or result.unit_ids() != identifiers
                or any(not rebound.slots[i].bound or rebound.slots[i].chunk != expected
                       for i, expected in selected.items())
                or any(rebound.slots[i].bound != b.bound for i, b in bindings.slots.items())):
            raise ValueError("written records or derived captures failed reimport")
        report = dict(schema="scopt.nop-compaction/1" if max_units == 1 else "scopt.nop-compaction/2",
                      status="built", input_snapshot=sid, output_snapshot=result.snapshot_id(),
                      profile_id=profile["profile_id"])
        if max_units == 1:
            report.update(rows_by_slot[0])
        else:
            report.update(max_units=max_units, units=rows_by_slot,
                          code_bytes_saved=sum(row["code_bytes_saved"] for row in rows_by_slot))
        (stage / "compaction.json").write_text(json.dumps(report, indent=2) + "\n")
        stage.rename(out)
    return report


@click.command("compact-nops")
@click.argument("snapshot", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--profile", "profile_path", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path(path_type=Path, file_okay=False))
@click.option("--max-units", type=click.IntRange(min=1), default=1, show_default=True)
def compact_nops(snapshot, profile_path, out, max_units):
    """Shrink eligible training-observed direct exits and emit the corresponding captures."""
    try:
        doc = generate(snapshot, profile_path, out, max_units=max_units)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(doc))
