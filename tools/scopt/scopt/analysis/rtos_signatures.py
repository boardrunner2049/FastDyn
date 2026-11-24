"""Recover Thumb names and covered spans from a user-supplied symbolized ELF.

A debug companion and a cross-build function fingerprint are distinct evidence.
The latter is a selection hint. It cannot authorize an ABI, scheduler fact or
machine-code edit. All guest bytes and runtime identity belong to the target.
"""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import replace
import json
from pathlib import Path

from capstone import arm as A

from ..decode.guest_symbols import GuestFunc, GuestImage, load_guest_image
from ..decode.thumb_sem import _md
from .rtos_cfg import _edges, decode_function, direct_target


def _text(image):
    # An RWX program segment may also contain vectors, initializers and strings.
    return [
        (lo, data)
        for lo, hi in image.text_sections
        if hi > lo and (data := image.read(lo, hi - lo)) is not None
    ]


def _locations(spans, raw):
    for lo, data in spans:
        start = data.find(raw)
        while start != -1:
            if not (lo + start) % 2:
                yield lo + start
            start = data.find(raw, start + 1)


def _entry_evidence(image):
    """Follow ordinary Thumb control flow rooted in target ELF entries.

    A direct BL target is an independent possible entry. Dynamic jumps, handler
    returns and unrecognized PC writes stop that path. No linear scan of an
    executable segment looking for something that decodes as a call.
    """
    entries = set(image.thumb_entries) | {f.start for f in image.funcs}
    todo = list(entries)
    visited = set()
    md = _md()
    code = image.code_ranges()
    while todo:
        pc = todo.pop()
        if pc in visited or pc is None or pc & 1:
            continue
        visited.add(pc)
        if not (
            any(lo <= pc < hi for lo, hi in image.text_sections)
            and any(lo <= pc < hi for lo, hi in code)
        ):
            continue
        insn = next(
            md.disasm(image.read(pc, 4) or image.read(pc, 2) or b"", pc, count=1), None
        )
        if insn is None:
            continue
        if insn.id == A.ARM_INS_BL:
            dst = direct_target(insn)
            if dst is not None:
                entries.add(dst)
                todo.append(dst)
        todo.extend(dst for dst in _edges(insn, (), {}) if dst is not None)
    return entries


def _signature_calls(image, func, raw, ref_starts, code, md):
    insns = decode_function(image, func, code, md)
    calls = [
        (pc - func.start, direct_target(insn))
        for pc, insn in sorted(insns.items())
        if insn.id == A.ARM_INS_BL
        and insn.size == 4
        and direct_target(insn) in ref_starts
        and not func.start <= direct_target(insn) < func.end
    ]
    parts = []
    pos = 0
    for offset, _ in calls:
        if pos < offset:
            parts.append((pos, raw[pos:offset]))
        pos = offset + 4
    if pos < len(raw):
        parts.append((pos, raw[pos:]))
    return calls, parts


def _relocated_matches(spans, size, parts, calls, md):
    """Check every fixed byte and decode every varying field as a Thumb BL."""
    if not parts:
        return []
    offset, anchor = max(parts, key=lambda part: len(part[1]))
    if len(anchor) < 12:
        return []
    matches = []
    for lo, data in spans:
        for anchor_pc in _locations([(lo, data)], anchor):
            pc = anchor_pc - offset
            i = pc - lo
            if i < 0 or i + size > len(data):
                continue
            if not all(data.startswith(seq, i + off) for off, seq in parts):
                continue
            destinations = []
            for off, _ in calls:
                insn = next(
                    md.disasm(data[i + off : i + off + 4], pc + off, count=1), None
                )
                if insn is None or insn.id != A.ARM_INS_BL or insn.size != 4:
                    break
                destinations.append(direct_target(insn))
            else:
                matches.append((pc, destinations))
    return matches


def _find_signature(
    image, func, raw, syms, entries, ref_starts, ref_text, tgt_text, ref_code, md
):
    # Short return stubs and veneers rarely carry a distinctive implementation.
    if len(raw) < 12:
        return None, (), "short_signature"
    if len(syms) != 1 or sum(1 for _ in _locations(ref_text, raw)) != 1:
        return None, (), "ambiguous_reference"
    locations = list(_locations(tgt_text, raw))
    dependencies = ()
    if not locations:
        calls, parts = _signature_calls(image, func, raw, ref_starts, ref_code, md)
        relocated = (
            _relocated_matches(tgt_text, len(raw), parts, calls, md) if calls else []
        )
        if not relocated:
            return None, (), "body_absent"
        if len(relocated) != 1:
            return None, (), "ambiguous_target"
        if len(_relocated_matches(ref_text, len(raw), parts, calls, md)) != 1:
            return None, (), "ambiguous_reference"
        ((pc, destinations),) = relocated
        dependencies = tuple(zip((dst for _, dst in calls), destinations))
    elif len(locations) != 1:
        return None, (), "ambiguous_target"
    else:
        pc = locations[0]
    if pc not in entries:
        return None, (), "no_independent_entry"
    return pc, dependencies, None


def recover(target: GuestImage, reference: GuestImage):
    """Return an annotated target and an address-by-address evidence receipt."""
    if not target.little_endian or not reference.little_endian:
        raise ValueError("signature recovery currently requires little-endian Thumb")
    if not reference.funcs or not reference.mapping:
        raise ValueError(
            "signature reference requires Thumb functions and ARM code/data mapping symbols"
        )
    matched, refused = [], []
    funcs = {(f.start, f.end, f.name): f for f in target.funcs}
    mapping = dict(target.mapping)
    ref_text, tgt_text = _text(reference), _text(target)
    if not ref_text or not tgt_text:
        raise ValueError(
            "signature recovery requires file-backed executable ELF sections"
        )
    same_image = (
        target.segments == reference.segments
        and target.exec_ranges == reference.exec_ranges
        and target.text_sections == reference.text_sections
        and target.thumb_entries == reference.thumb_entries
        and target.readonly_ranges == reference.readonly_ranges
    )
    ref_code = reference.code_ranges()
    method = "load_image_companion" if same_image else "exact_thumb_span"
    by_bytes = defaultdict(list)

    def initial(f):
        return {"name": f.name, "reference": f.start, "size": f.size}

    for f in reference.funcs:
        if not any(lo <= f.start < hi for lo, hi in ref_code):
            continue
        if not any(lo <= f.start and f.end <= lo + len(data) for lo, data in ref_text):
            continue
        by_bytes[reference.read(f.start, f.size)].append(f)

    proposals = []
    methods = {}
    if same_image:
        for syms in by_bytes.values():
            proposals.extend((f, f.start) for f in syms)
        mapping.update(reference.mapping)
    else:
        entries = _entry_evidence(target)
        md = _md()
        ref_starts = {f.start for f in reference.funcs}
        for raw, syms in by_bytes.items():
            f = syms[0]
            pc, deps, reason = _find_signature(
                reference,
                f,
                raw,
                syms,
                entries,
                ref_starts,
                ref_text,
                tgt_text,
                ref_code,
                md,
            )
            if pc is not None:
                proposals.append((f, pc))
                if deps:
                    methods[f] = ("linked_thumb_calls", deps)
            if reason:
                refused.extend(dict(initial(f), reason=reason) for f in syms)

    # Resolve overlaps symmetrically. An ambiguous match never wins by
    # iteration order, and a surviving target symbol always takes precedence.
    proposals.sort(key=lambda pair: pair[1])
    ref_map = dict(reference.mapping)
    ref_offsets = sorted(ref_map)
    previous_end = -1
    target_mapping = dict(target.mapping)
    eligible = []
    for i, (f, pc) in enumerate(proposals):
        end = pc + f.size
        overlaps_previous = pc < previous_end
        previous_end = max(previous_end, end)
        key = pc, end, f.name
        if not same_image and (
            overlaps_previous
            or (i + 1 < len(proposals) and proposals[i + 1][1] < end)
            or any(
                pc < known.end
                and known.start < end
                and (known.start, known.end, known.name) != key
                for known in target.funcs
            )
        ):
            refused.append(dict(initial(f), reason="conflicting_span"))
            continue
        if not same_image:
            changes = [(pc, "t")]
            changes.extend(
                (pc + off - f.start, ref_map[off])
                for off in ref_offsets[
                    bisect_right(ref_offsets, f.start) : bisect_right(
                        ref_offsets, f.end - 1
                    )
                ]
            )
            # Preserve authoritative mapping symbols, including internal data.
            reference_kind = "t"
            change_map = dict(changes)
            target_kind = None
            for off, kind in target.mapping:
                if off <= pc:
                    target_kind = kind
            conflict = False
            for addr in sorted(
                set(change_map) | {a for a, _ in target.mapping if pc <= a < end}
            ):
                reference_kind = change_map.get(addr, reference_kind)
                target_kind = target_mapping.get(addr, target_kind)
                if target_kind and target_kind != reference_kind:
                    conflict = True
                    break
            if conflict:
                refused.append(dict(initial(f), reason="conflicting_mapping"))
                continue
        eligible.append(
            (f, pc, changes if not same_image and not target.mapping else [])
        )

    # A masked displacement requires independent evidence for the identity
    # of every BL destination. Cycles with no independent seed stay unresolved.
    known = {f.start: pc for f, pc, _ in eligible if f not in methods}
    pending = list(eligible)
    while pending:
        next_pending = []
        for f, pc, changes in pending:
            source, deps = methods.get(f, (method, ()))
            if not all(known.get(ref) == dst for ref, dst in deps):
                next_pending.append((f, pc, changes))
                continue
            known[f.start] = pc
            end = pc + f.size
            if changes:
                mapping.update(changes)
                mapping.setdefault(end, "d")
            funcs.setdefault(
                (pc, end, f.name), GuestFunc(f.name, pc, end, source=source)
            )
            matched.append(
                dict(
                    initial(f),
                    target=pc,
                    method=source,
                    extent="companion_symbol" if same_image else "covered_span_only",
                )
            )
        if len(next_pending) == len(pending):
            refused.extend(
                dict(initial(f), reason="unconfirmed_call_destination")
                for f, _, _ in next_pending
            )
            break
        pending = next_pending
    image = replace(
        target,
        funcs=tuple(sorted(funcs.values(), key=lambda f: (f.start, -f.end, f.name))),
        mapping=tuple(sorted(mapping.items())),
        _starts=[],
    )
    return image, {
        "reference_sha256": reference.sha256,
        "target_sha256": target.sha256,
        "method": "load_image_companion" if same_image else "function_signatures",
        "matched": matched,
        "refused": refused,
        "refusal_counts": dict(sorted(Counter(r["reason"] for r in refused).items())),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    from .task_loops import discover_task_loops

    image, evidence = recover(
        load_guest_image(args.target), load_guest_image(args.reference)
    )
    print(
        json.dumps(
            {
                **evidence,
                "task_analysis": discover_task_loops(
                    image, include_terminal=True
                ).report(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
