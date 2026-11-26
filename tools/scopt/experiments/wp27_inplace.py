"""WP27 slice 0b: the in-place specialization copy (B3/B4 design §12, plan ``## WP27``).

Labelled ``experimental_unsound_in_place_specialization``. **Never published.**

A public-only SuperCache copy of a base snapshot in which

* the return unit R gets one **GENERAL** guarded slot ``n`` towards ``Vc``
  (canonical guard ``guard-v1``, §4.1). The slot is **registered** in R's record
  (``jmp_insn_offset[n]``/``jmp_reset_offset[n]``) and its reset path is
  ``exit_tb(R|n)``, so the stock (``qint``) main loop links it after its own lookup
  (cpu-exec.c ``tb_add_jump``), as in WP16's GENERAL-slot probe. This fixture uses the ordinary record offsets
  and omits the semantic extension table;
* the slice-0a ``C'`` patches (CP/BF/DHO, same-length, relative to C's ``tc``) are
  written **in place into the general C**.

This is behaviour-preserving only for F-B3-1: C has one predecessor (R), the FastDyn
modifier ``r0 <- 0`` on ``f``'s ``bx lr`` makes r0 = 0 at every entry of C, and no
handler touches r0. It is unsound in general (every other entry of C would run the
specialized bytes) and bypasses semantic slot disarm on TLB flush (``qint``), which is why the
plan's m-CE7 diag twin is required for each run.

NativeLinker v2 (WP18) does the work: :class:`scopt.link.B3Plan` with
``grow={R: GENERAL spec}``, ``rewrites={C: patches}``, ``register_slots=True``. Its
result includes a registered slot. This experiment emits the linked public records
and bodies with the public-only writer. Everything else (R moved
behind the arena end, C rewritten in place, every other unit byte-identical) is
NativeLinker v2's.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

LABEL = "experimental_unsound_in_place_specialization"
LABEL_FILE = "EXPERIMENTAL_UNSOUND.json"
SCHEMA = "scopt.wp27.inplace_copy/1"


class InPlaceError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class SimplePatch:
    """A same-length patch relative to a unit's ``tc`` (what ``apply_pure_rewrite`` reads)."""

    host_off: int
    old_bytes: bytes
    new_bytes: bytes
    template_id: str = "wp27-from-slice-0a"


def _sha_file(p: str | Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def find_units(snap: Any, r_vaddr: int, vc: int) -> tuple[Any, Any, Any]:
    """``(R, its lookup tail, C)``: R is the unit whose guest range holds ``r_vaddr``
    (the return insn) and that has exactly one lookup tail of exit class ``return``;
    C is the unit whose first vaddr is ``vc``. Refuses ambiguity (fail closed)."""
    from scopt.link import lookup_tails

    rs = [u for u in snap.units()
          if u.vaddrs and u.vaddrs[0] <= r_vaddr < u.vaddrs[0] + max(u.guest_size, 1)
          and lookup_tails(u)]
    if len(rs) != 1:
        raise InPlaceError("ambiguous_r", f"{len(rs)} units hold {r_vaddr:#x} with a lookup tail")
    cs = [u for u in snap.units() if u.vaddrs and u.vaddrs[0] == vc]
    if len(cs) != 1:
        raise InPlaceError("ambiguous_c", f"{len(cs)} units start at {vc:#x}")
    tails = lookup_tails(rs[0])
    if len(tails) != 1:
        raise InPlaceError("ambiguous_tail", f"R has {len(tails)} lookup tails")
    return rs[0], tails[0], cs[0]


def build_inplace_copy(snap: Any, snap_dir: str | Path, *, r_vaddr: int, vc: int, patches: Sequence[Any],
                       out_dir: str | Path, layout: Any, slot_n: int | None = None,
                       provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the public-only ``out_dir/cache.sc`` and ``out_dir/EXPERIMENTAL_UNSOUND.json``; copy the
    base snapshot's ``bundle.json``/``runtime_identity.json`` next to it so deploy runs its
    identity checks. Returns the description (also in the label file), including the
    host probe offsets (relative to the code arena) for the gdb diag runs."""
    from scopt.formats.layouts import SC_VERSION
    from scopt.formats.sc_records import read_sc
    from scopt.formats.supercache import write_public_cache
    from scopt.guard import GuardOffsets, GuardTarget, decode_guard
    from scopt.link import B3Plan, GuardedExitSpec, NativeLinkerV2, free_slots

    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise InPlaceError("out_dir_not_fresh", str(out))
    out.mkdir(parents=True, exist_ok=True)
    if snap.header.version != SC_VERSION or snap.header.reserved:
        raise InPlaceError("requires_public_cache", "fixture expects public records without extensions")
    R, tail, C = find_units(snap, r_vaddr, vc)
    free = free_slots(R)
    n = slot_n if slot_n is not None else (free[0] if free else None)
    if n is None or n not in free:
        raise InPlaceError("no_free_slot", f"R free slots {free}, wanted {slot_n}")
    if vc & 1:
        raise InPlaceError("vc_thumb_bit", hex(vc))
    spec = GuardedExitSpec(tail.off, "return", (GuardTarget(vc, n, "GENERAL", True),),
                           C.key.flags, C.key.cs_base)
    plan = B3Plan(grow={R.uid: (spec,)}, rewrites={C.uid: tuple(patches)},
                  register_slots=True)
    off = GuardOffsets.from_layout(layout)
    link = NativeLinkerV2(snap, plan, offsets=off, derive_captures=False).link()
    if not link.ok:
        f = link.failures[0]
        raise InPlaceError(f"link:{f.reason}", f.detail)
    # Write and independently decode the public-only fixture.
    parts = [(u.record, u.body) for u in link.units]
    write_public_cache(out / "cache.sc", snap.header, parts)
    sc = read_sc(out / "cache.sc")
    hdr_problems = list(sc.header_problems)
    # -- static self-check of the written file, independent of the link object -----
    by_uid = {lu.uid: lu for lu in link.units}
    lr = by_uid[R.uid]
    checks: dict[str, Any] = {"header_version": sc.header.version,
                              "header_features": sc.header.reserved,
                              "header_problems": hdr_problems,
                              "records": len(sc.valid_slots), "base_records": len(snap.units())}
    rec_of = {sc.records[i].tb_offset: (i, sc.records[i]) for i in sc.valid_slots}
    # every unit other than R and C: same record bytes and body at its own offset
    same = 0
    diff = []
    for u in snap.units():
        if u.uid in (R.uid, C.uid):
            continue
        i, r = rec_of.get(u.tb_offset, (None, None))
        if i is not None and sc.record_bytes[i] == bytes(u.record) and \
                sc.bodies[i] == u.code + u.search:
            same += 1
        else:
            diff.append(u.uid)
    checks["other_units_identical"] = {"n": same, "different": diff}
    # C: same offset, code == base code with exactly the patches applied
    ci, crec = rec_of[C.tb_offset]
    cbody = sc.bodies[ci]
    want = bytearray(C.code)
    for p in patches:
        want[p.host_off:p.host_off + len(p.old_bytes)] = p.new_bytes
    checks["c_in_place"] = {
        "tb_offset": hex(C.tb_offset), "tc_offset": hex(C.tc_offset),
        "same_offsets": (crec.tb_offset, crec.tc_offset, crec.tc_size) ==
        (C.tb_offset, C.tc_offset, C.tc_size),
        "code_equals_base_plus_patches": bytes(cbody[:C.tc_size]) == bytes(want),
        "search_unchanged": bytes(cbody[C.tc_size:]) == C.search,
        "bytes_changed": sum(1 for a, b in zip(C.code, cbody[:C.tc_size]) if a != b),
        "record_unchanged": sc.record_bytes[ci] == bytes(C.record),
    }
    # R: moved, slot n registered, guard decodes as guard-v1 with a GENERAL exit_tb reset
    ri, rrec = rec_of[lr.new_tb_offset]
    rbody = sc.bodies[ri]
    g = link.grown[R.uid]
    st = g.stubs[0][1]
    reg = {n: (rrec.jmp_insn_offset[n], rrec.jmp_reset_offset[n])}
    dg = decode_guard(rbody, st.off, tc_abs=snap.header.code_address + rrec.tc_offset,
                      struct_off=rrec.tb_offset - rrec.tc_offset, offsets=off, registered=reg)
    si = dg.slots[0]
    checks["r_grown"] = {
        "from_tb_offset": hex(R.tb_offset), "to_tb_offset": hex(rrec.tb_offset),
        "tc_offset": hex(rrec.tc_offset), "tc_size": rrec.tc_size,
        "stub_off": hex(st.off), "guard_len": st.guard_len,
        "slot_n": n, "jmp_insn_offset": rrec.jmp_insn_offset[n],
        "jmp_reset_offset": rrec.jmp_reset_offset[n],
        "decoded": {"atoms": [a[0] for a in dg.atoms], "kf": hex(dg.kf), "kf2": hex(dg.kf2),
                    "targets": [hex(v) for v in dg.vaddrs], "slot_kind": si.kind,
                    "registered": si.registered, "reset_kind": si.reset_kind,
                    "f_off": hex(dg.f_off)},
        "body_before_tail_unchanged": bytes(rbody[:tail.off]) == bytes(
            _relocated_prefix(R, rrec.tb_offset)[:tail.off]),
        "old_slot_free_in_base": n in free,
    }
    ok = ((sc.header.version, sc.header.reserved) == (SC_VERSION, 0) and not hdr_problems and
          checks["records"] == checks["base_records"] and not diff and
          checks["c_in_place"]["same_offsets"] and
          checks["c_in_place"]["code_equals_base_plus_patches"] and
          checks["c_in_place"]["search_unchanged"] and
          si.kind == "GENERAL" and si.registered and si.reset_kind == "exit_tb" and
          dg.vaddrs == (vc,) and rrec.jmp_insn_offset[n] == si.field_off and
          rrec.jmp_reset_offset[n] == si.reset_off)
    checks["ok"] = bool(ok and checks["c_in_place"]["record_unchanged"] and
                        checks["r_grown"]["body_before_tail_unchanged"])
    ca = snap.header.code_address
    probes = [
        {"what": "C tc.ptr (patched in place)", "kind": "c", "off": C.tc_offset},
        {"what": "R stub S", "kind": "stub", "off": rrec.tc_offset + st.off},
        {"what": "R stub F (lookup fallback)", "kind": "fallback",
         "off": rrec.tc_offset + dg.f_off},
        {"what": "R slot reset exit_tb(R|n)", "kind": "reset",
         "off": rrec.tc_offset + si.reset_off},
    ]
    for p in probes:
        p["addr"] = ca + p["off"]
    desc = {
        "schema": SCHEMA, "label": LABEL, "published": False,
        "valid_for": "F-B3-1 only (C has one predecessor R; the modifier r0 <- 0 makes r0 = 0 "
                     "at every entry of C; no handler touches r0)",
        "unsound_because": ["every entry of the general C runs the specialized bytes",
                            "public-only fixture omits semantic slot disarm on TLB flush (m-CE7 "
                            "diag twin required)"],
        "base_snapshot_id": snap.snapshot_id,
        "base_cache_sc": str(Path(snap_dir) / "cache.sc"),
        "cache_sc_sha256": _sha_file(out / "cache.sc"),
        "R": {"uid": R.uid, "vaddrs": [hex(v) for v in R.vaddrs], "tail_off": hex(tail.off),
              "slot_n": n, "slot_kind": "GENERAL", "target_vc": hex(vc)},
        "C": {"uid": C.uid, "vaddrs": [hex(v) for v in C.vaddrs]},
        "patches": [{"host_off": hex(p.host_off), "old": bytes(p.old_bytes).hex(),
                     "new": bytes(p.new_bytes).hex(),
                     "template_id": getattr(p, "template_id", None)} for p in patches],
        "envlayout_id": getattr(layout, "envlayout_id", None) or getattr(layout, "id", None),
        "code_address": hex(ca),
        "probes": [{**p, "off": hex(p["off"]), "addr": hex(p["addr"])} for p in probes],
        "checks": checks, "provenance": provenance or {},
    }
    for name in ("bundle.json", "runtime_identity.json"):
        src = Path(snap_dir) / name
        if src.is_file():
            shutil.copyfile(src, out / name)
    (out / LABEL_FILE).write_text(json.dumps(desc, indent=1, sort_keys=True, default=str) + "\n")
    return desc


def _relocated_prefix(u: Any, new_tb: int) -> bytes:
    from scopt.link import relocate_unit

    return relocate_unit(u, new_tb).body


def patches_from_variant(vd_variant: dict[str, Any]) -> list[SimplePatch]:
    """The ``patches`` rows of :func:`wp21_integ.variant_diff` (offsets relative to C's
    tc; relocations excluded) as :class:`SimplePatch`."""
    out = []
    for p in vd_variant["patches"]:
        out.append(SimplePatch(int(p["off"], 16), bytes.fromhex(p["old"]), bytes.fromhex(p["new"])))
    return out


def window_twin_script(probe_addrs: Iterable[int]) -> list[str]:
    """gdb commands of the WP27 **ordering twin** (diagnostic, never timed).

    As :func:`scopt.run.diag.twin_script` (breakpoint 1 ``benchmark_start``, 2
    ``benchmark_end``, 3.. every ``tlb_flush*``/``tcg_commit*`` entry, enabled at
    ``benchmark_start``) but the flush breakpoints **stop** (no ignore count) and print
    ``info breakpoints`` each time, and hardware breakpoints (ignore count 1e9) count the
    executions of the probe addresses (C, R's stub). The hit counts printed at a flush are
    those *before* the flush, so "every flush happened after the last execution of R and
    C" is ``hits at the first flush == final hits``."""
    from scopt.run.diag import FLUSH_FUNCS

    n = len(FLUSH_FUNCS)
    head = ("cpus_queue.tqh_first->breakpoints.tqh_first, "
            "cpus_queue.tqh_first->watchpoints.tqh_first, "
            "cpus_queue.tqh_first->singlestep_enabled")
    lines = ["set pagination off", "set confirm off", "set breakpoint pending on",
             "set print pretty off", "set width 0",
             "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH "
             "nostop noprint pass",
             "break benchmark_start", "break benchmark_end"]
    lines += [f"break {f}" for f in FLUSH_FUNCS]
    lines.append(f"disable 3-{2 + n}")
    for k in range(3, 3 + n):
        lines += [f"commands {k}", "silent", f'printf "SCOPT flush bp={k}\\n"',
                  'printf "SCOPT info-breakpoints-begin\\n"', "info breakpoints",
                  'printf "SCOPT info-breakpoints-end\\n"', "continue", "end"]
    hb = [f"hbreak *0x{a:x}" for a in probe_addrs]
    # the code arena is mapped by benchmark_start (the cache was loaded at startup)
    lines += ["commands 1", "silent",
              f'printf "SCOPT benchmark_start bp_head=%p wp_head=%p singlestep=%d\\n", {head}',
              f"enable 3-{2 + n}"]
    for k, h in enumerate(hb):
        lines += [h, f"ignore {3 + n + k} 1000000000"]
    lines += ["continue", "end",
              "commands 2", "silent",
              f'printf "SCOPT benchmark_end bp_head=%p wp_head=%p singlestep=%d\\n", {head}',
              'printf "SCOPT info-breakpoints-begin\\n"', "info breakpoints",
              'printf "SCOPT info-breakpoints-end\\n"', "continue", "end", "run"]
    return lines


def parse_window_twin(text: str, probe_addrs: Sequence[int]) -> dict[str, Any]:
    """Per flush stop: the function and the probe hit counts before it; at
    ``benchmark_end``: the final counts and the flush totals."""
    import re

    from scopt.run.diag import FLUSH_FUNCS, parse_hits

    blocks = []
    lines = text.splitlines()
    i = 0
    cur_flush = None
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"SCOPT flush bp=(\d+)", ln)
        if m:
            cur_flush = FLUSH_FUNCS[int(m.group(1)) - 3]
        if ln.startswith("SCOPT benchmark_end"):
            cur_flush = "<benchmark_end>"
        if ln == "SCOPT info-breakpoints-begin":
            try:
                j = lines.index("SCOPT info-breakpoints-end", i)
            except ValueError:  # truncated output (gdb or QEMU died): read to the end
                j = len(lines)
            blk = "\n".join(lines[i + 1:j])
            h = parse_hits(blk)
            blocks.append({"at": cur_flush, "probe_hits": {hex(a): h.get(a) for a in probe_addrs}})
            i = j
        i += 1
    flushes = [b for b in blocks if b["at"] != "<benchmark_end>"]
    end = next((b for b in blocks if b["at"] == "<benchmark_end>"), None)
    final = end["probe_hits"] if end else None
    after = [b for b in flushes if final is None or b["probe_hits"] != final]
    return {"flush_stops": len(flushes), "flushes": flushes, "final_probe_hits": final,
            "benchmark_start_seen": "SCOPT benchmark_start" in text,
            "benchmark_end_seen": end is not None,
            "flushes_before_last_probe_hit": len(after)}
