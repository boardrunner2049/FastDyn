"""Trace-based placement heuristic for ordinary x86 SuperCache v5 restores."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tomllib

from ..canon import is_sha256_id
from ..decode.search import is_pcrel
from ..formats.sc_v5 import ScRecord, sha256_sparse
from ..run.launcher import file_sha256

_PAGES = (4096, 16384)
_DISPATCH = re.compile(r"^Trace (\d+): (0x[0-9a-f]+) \[[^/]+/([0-9a-f]+)/", re.I)
_LINK = re.compile(r"^Linking TBs (0x[0-9a-f]+) index ([01]) -> (0x[0-9a-f]+)$", re.I)
_TRACE_KIND = "untimed training only, outer dispatch/link-write trace"
_LINK_MEMBERS = ("jmp_target_addr[0]", "jmp_list_next[0]", "jmp_dest[0]",
                 "jmp_lock", "jmp_list_head")


class ActivationLayoutError(ValueError):
    """The runtime, trace, marker evidence or unit layout cannot be bound."""


def _require(condition, detail: str) -> None:
    if not condition:
        raise ActivationLayoutError(detail)


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"expected a JSON object in {path}")
    return value


@dataclass(frozen=True)
class _Geometry:
    size: int
    link_fields: tuple[int, ...]
    link_widths: tuple[int, ...]

    def link_offsets(self, slot: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
        data = tuple(off + width * slot for off, width
                     in zip(self.link_fields[:3], self.link_widths[:3], strict=True))
        return data, self.link_fields[3:]


@dataclass(frozen=True)
class _Unit:
    uid: str
    slot: int
    entry: int
    gap: int
    span: int
    end: int
    guest_pcs: tuple[int, ...]
    guest_offsets: tuple[int, ...]
    reset_fields: tuple[int, int]


@dataclass(frozen=True)
class _Trace:
    events: tuple[tuple[int, str, tuple[int, ...]], ...]
    bounds: tuple[int, int]
    end_kind: str


def _geometry(qemu: Path, expected_size: int) -> _Geometry:
    from elftools.elf.elffile import ELFFile
    with qemu.open("rb") as binary:
        _require(ELFFile(binary).get_machine_arch() == "x64", "requires an x86-64 QEMU ELF")

    fields = [f"((TranslationBlock *)0)->{field}" for field in _LINK_MEMBERS]
    exprs = ["sizeof(TranslationBlock)", *(f"(unsigned long)&({m})" for m in fields),
             *(f"sizeof({m})" for m in fields)]
    cmd = ["gdb", "-nx", "-nh", "-batch", str(qemu)]
    for expr in exprs:
        cmd.extend(("-ex", f"p/x {expr}"))
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise ActivationLayoutError("cannot read QEMU TranslationBlock debug information with gdb") from error
    values = [int(n, 16) for n in re.findall(r"^\$\d+ = (0x[0-9a-f]+)$", result.stdout, re.M)]
    _require(len(values) == 11 and values[0] == expected_size,
             "QEMU TranslationBlock size does not match the snapshot")
    geom = _Geometry(values[0], tuple(values[1:6]), tuple(values[6:]))
    _require(geom.link_widths[:3] == (8, 8, 8) and all(
        0 <= offset < offset + width * (2 if i < 3 else 1) <= geom.size
        for i, (offset, width) in enumerate(zip(geom.link_fields, geom.link_widths, strict=True))
    ), "QEMU jump-link fields do not fit the recorded ABI")
    return geom


def _units(snapshot, geometry: _Geometry) -> tuple[_Unit, ...]:
    result = []
    base = snapshot.header.code_address
    for slot, u in enumerate(sorted(snapshot.units(), key=lambda row: row.slot)):
        _require(u.slot == slot and u.meta_status == "ok" and u.reloc.relocatable
                 and not u.native_fragments and not u.splice_applied
                 and u.stub_start is None and u.inline_start is None,
                 "requires dense, bound, ordinary relocatable records")
        record = ScRecord.from_bytes(u.record)
        gap = u.tc_offset - u.tb_offset
        _require(gap >= geometry.size, f"TB header overlaps code in slot {slot}")
        for where, reset in zip(record.jmp_insn_offset, record.jmp_reset_offset, strict=True):
            if where == reset == 0xFFFF:
                continue
            _require(0 < where < reset <= len(u.code) and reset == where + 4
                     and (base + u.tc_offset + where) % 4 == 0
                     and u.code[where - 1:reset] == b"\xe9\0\0\0\0",
                     f"slot {slot} does not have an ordinary reset x86 jump")
        pcs = tuple(u.vaddrs)
        _require(bool(pcs) and len(u.insns) == record.icount,
                 f"slot {slot} lacks captured guest instruction addresses")
        first_pc = pcs[0]
        if is_pcrel(record.cflags):
            first_pc &= (1 << snapshot.header.target_page_bits) - 1
        offsets = tuple(i.words[0] - first_pc for i in u.insns)
        _require(all(0 <= off < u.guest_size for off in offsets),
                 f"slot {slot} has guest instruction addresses outside its range")
        result.append(_Unit(u.uid, slot, base + u.tc_offset, gap,
                            len(u.code) + len(u.search), base + u.end_offset,
                            pcs, offsets, record.jmp_insn_offset))
    _require(bool(result), "snapshot has no native records")
    return tuple(result)


def _markers(config: Path, virtuals: Path) -> dict[str, int]:
    cfg = tomllib.loads(config.read_text(encoding="utf-8"))
    (cpu,), = cfg["CPU"].values()
    keys = {"benchmark_start", "benchmark_end"}
    entries = [(row["instruction"], int(str(row["at"]), 0))
               for row in cpu.get("virtuals", []) if row["instruction"] in keys]
    _require(len(entries) == 2 and {name for name, _ in entries} == keys
             and len({pc for _, pc in entries}) == 2, "requires two distinct benchmark markers")
    emitted = [row.split() for row in virtuals.read_text(encoding="utf-8").splitlines()]
    observed = [(row[1], int(row[0], 0)) for row in emitted if len(row) >= 2 and row[1] in keys]
    _require(sorted(observed) == sorted(entries), "plugin marker file differs from frozen configuration")
    return dict(entries)


def _load_trace(path: Path, units: tuple[_Unit, ...], marks: dict[str, int],
                digest: str, terminal: dict | None, plugin_sha: str,
                manifest: dict | None = None) -> _Trace:
    host_to_slot = {u.entry: u.slot for u in units}
    events = []
    start = end = None
    last_dispatch = None
    marker_rows: dict[str, dict] = {}
    cpu_indices = set()
    trace_hash = sha256()
    with path.open("rb") as source:
        for number, raw in enumerate(source, 1):
            trace_hash.update(raw)
            text = raw.decode("utf-8").rstrip("\r\n")
            if match := _DISPATCH.match(text):
                cpu, host, guest = match.groups()
                cpu_indices.add(int(cpu))
                host, guest = int(host, 16), int(guest, 16)
                _require(host in host_to_slot, f"unknown host code address at trace line {number}")
                slot = host_to_slot[host]
                _require(guest in units[slot].guest_pcs,
                         f"guest PC does not match snapshot at trace line {number}")
                events.append((number, "dispatch", (slot,)))
                last_dispatch = (number, host, guest)
                if manifest is not None:
                    unit = units[slot]
                    for name, marker in marks.items():
                        offset = marker - guest
                        if offset in unit.guest_offsets:
                            _require(name not in marker_rows, f"trace has repeated {name} markers")
                            marker_rows[name] = {
                                "line": number, "guest_start": hex(guest), "uid": unit.uid,
                                "slot": slot, "native_entry": hex(host), "marker_pc": hex(marker),
                                "marker_insn_index": unit.guest_offsets.index(offset),
                            }
                elif guest == marks["benchmark_start"]:
                    _require(start is None, "trace has repeated start markers")
                    start = number
                elif guest == marks["benchmark_end"]:
                    _require(start is not None and end is None, "trace has misplaced end markers")
                    end = number
            elif match := _LINK.fullmatch(text):
                src, slot, dst = match.groups()
                src, dst, n = int(src, 16), int(dst, 16), int(slot)
                _require(src in host_to_slot and dst in host_to_slot,
                         f"unknown native link endpoint at trace line {number}")
                events.append((number, "link", (host_to_slot[src], n, host_to_slot[dst])))
            elif text.startswith(("Linking TBs ", "Trace ")):
                raise ActivationLayoutError(f"unsupported QEMU trace event at line {number}")
    _require("sha256:" + trace_hash.hexdigest() == digest,
             "trace bytes changed while reading evidence")
    if manifest is not None:
        _require(len(cpu_indices) == 1 and set(marker_rows) == marks.keys(),
                 "manifest trace needs both unique markers and one vCPU")
        first, last = marker_rows["benchmark_start"], marker_rows["benchmark_end"]
        start, end = first["line"], last["line"]
        _require(manifest.get("start_marker") == first and manifest.get("end_marker") == last
                 and manifest.get("trace_bounds") == [start, end],
                 "manifest markers do not match captured instructions in the trace")
        _require(start < end == events[-1][0] and events[-1][1] == "dispatch",
                 "manifest end marker must be the final dispatch or link event")
        return _Trace(tuple(events), (start, end), "verified_marker_dispatch")
    _require(start is not None and len(cpu_indices) == 1, "trace needs a start marker and one vCPU")
    end_kind = "explicit_dispatch"
    if end is None:
        _require(terminal is not None and last_dispatch is not None,
                 "a chained end marker requires terminal-dispatch evidence")
        line, host, guest = last_dispatch
        u = units[host_to_slot[host]]
        marker = terminal.get("terminal_marker_pc", terminal.get("benchmark_end_guest_pc"))
        plugin = terminal.get("plugin_sha256", terminal.get("exact_plugin_sha256"))
        terminal_exit = terminal.get("synchronous_exit_after_timer_and_flush") is True or (
            terminal.get("synchronous_timer_print_flush_exit_call_order")
            == ["clock_gettime", "printf", "fflush", "exit"]
        )
        _require(terminal.get("trace_sha256") == digest and plugin == plugin_sha
                 and terminal.get("last_dispatch") == list(last_dispatch)
                 and marker == marks["benchmark_end"] and marker - guest in u.guest_offsets
                 and terminal_exit and terminal.get(
                     "exact_guest_oracle", terminal.get("exact_benchmark_oracle")) is True,
                 "terminal-dispatch evidence does not bind this trace and marker")
        end = line + 1
        end_kind = "verified_terminal_callback"
    _require(start < end, "trace marker interval is empty")
    return _Trace(tuple(events), (start, end), end_kind)


def _operations(geometry: _Geometry, units: tuple[_Unit, ...], trace: _Trace):
    ops = []
    activated = set()
    pending = set()
    writes = Counter()

    def enter(slot):
        if slot not in pending:
            ops.append(("X", slot))
            pending.add(slot)

    def write(refs, kind):
        ops.append(("W", tuple(refs)))
        writes[kind] += 1
        pending.clear()

    def activate(slot):
        if slot in activated:
            return
        activated.add(slot)
        u = units[slot]
        write([(slot, -u.gap), (slot, -u.gap + geometry.size - 1)]
              + [(slot, f) for f in u.reset_fields if f != 0xFFFF], "activation")

    start, end = trace.bounds
    for line, kind, fields in trace.events:
        if line == start:
            ops.append(("M", None))
            pending.clear()
        if line > end:
            break
        if kind == "dispatch":
            slot, = fields
            activate(slot)
            enter(slot)
        else:
            src, n, dst = fields
            jump = units[src].reset_fields[n]
            _require(jump != 0xFFFF, f"trace links absent outgoing slot {src}:{n}")
            activate(src)
            enter(src)
            activate(dst)
            local, remote = geometry.link_offsets(n)
            write([(src, jump)] + [(src, off - units[src].gap) for off in local]
                  + [(dst, off - units[dst].gap) for off in remote], "link")
    _require(bool(writes["link"]) and bool(activated), "trace has no usable native links")
    return ops, activated, dict(writes)


def _scores(positions, operations):
    def score(page):
        entry_pages = [v // page for v in positions]
        regions = Counter()
        last = {}
        active = False
        cost = 0
        for kind, data in operations:
            if kind == "M":
                active = True
            elif kind == "W":
                for p in {(positions[i] + off) // page for i, off in data}:
                    regions[p] += 1
            else:
                v = regions[entry_pages[data]]
                if active and data in last and v != last[data]:
                    cost += 1
                last[data] = v
        return cost
    return tuple(score(page) for page in _PAGES)


def _choose(units, operations, active, arena_limit, max_moves):
    positions = [u.entry for u in units]
    baseline = previous = _scores(positions, operations)
    align = max(_PAGES)
    ceiling = ((max(u.end for u in units) + align - 1) // align + 1) * align
    moves = []
    for j in range(max_moves):
        dest = ceiling + j * align * 2
        options = []
        for i in sorted(active.difference(moves)):
            if units[i].span > align or dest + units[i].span > arena_limit:
                continue
            proposed = positions.copy()
            proposed[i] = dest
            scores = _scores(proposed, operations)
            if all(a <= b for a, b in zip(scores, previous, strict=True)) and sum(scores) < sum(previous):
                options.append((sum(scores), max(scores), i, scores))
        if not options:
            break
        _, _, winner, previous = min(options)
        positions[winner] = dest
        moves.append(winner)
    return moves, positions, baseline, previous


def _single_arg(args: list[str], names: tuple[str, ...]) -> str:
    locations = [n for n, arg in enumerate(args) if arg in names]
    _require(len(locations) == 1 and locations[0] < len(args) - 1,
             f"expected one launch argument {names[0]}")
    return args[locations[0] + 1]


def _bind(snapshot, qemu: Path, config: Path, receipt: Path, protocol: Path,
          manifest: dict | None = None):
    raw, control = _json(receipt), _json(protocol)
    _require(snapshot.header.version == 5 and snapshot.runtime and snapshot.path,
             "activation layout requires a stored ordinary v5 snapshot")
    runtime = snapshot.runtime
    _require(runtime.get("supercache_flat") is False, "requires non-flat SuperCache")
    sha = file_sha256(qemu)
    _require(sha == raw["qemu_sha256"] == control["qemu_sha256"] == runtime["qemu_sha256"],
             "QEMU does not match the training trace and snapshot")
    source = Path(snapshot.path).resolve()
    _require(Path(raw["ordinary_warm"]).resolve() == Path(control["ordinary_warm"]).resolve() == source,
             "training trace is for a different snapshot")
    _require(control["purpose"] == _TRACE_KIND, "requires an untimed training trace")
    _require(raw.get("source_revision") == control.get("source_revision"),
             "trace launch source revision differs from the frozen protocol")
    cache_sha = "sha256:" + sha256_sparse(source / "cache.sc")
    _require(cache_sha == control["warm_cache_sha256"], "training snapshot cache bytes changed")
    proof = raw["proof"]
    _require(raw["exact_guest_oracle"] is True and proof["verdict"] == "PASS"
             and proof["file_version"] == 5 and proof["appended"] == 0
             and proof["n"] == proof["record_count_after"] == snapshot.header.record_count,
             "training trace must preserve the original cache and exact guest output")
    args = raw["argv"]
    _require(isinstance(args, list) and all(isinstance(x, str) for x in args)
             and str(qemu.resolve()) in args, "trace argv does not name the supplied executable")
    _require(_single_arg(args, ("-d",)) == "exec" and "-dfilter" not in args,
             "requires an unfiltered QEMU -d exec trace")
    trace = Path(raw["trace"])
    _require(Path(_single_arg(args, ("-D",))).resolve() == trace.resolve(),
             "trace log does not match the original launch")
    digest = file_sha256(trace)
    _require(digest == raw["trace_sha256"], "trace SHA-256 mismatch")
    accel = _single_arg(args, ("-accel",))
    prefix = "tcg,supercache="
    _require(accel.startswith(prefix) and "," not in accel[len(prefix):],
             "trace used a nonordinary accelerator configuration")
    _require("sha256:" + sha256_sparse(Path(accel[len(prefix):])) == cache_sha,
             "trace launch used different cache bytes")
    plugin_spec = _single_arg(args, ("--plugin", "-plugin")).split(",")
    plugin = Path(plugin_spec[0])
    plugin_sha = file_sha256(plugin)
    _require(plugin_sha == raw["plugin_sha256"] == control["plugin_sha256"] == runtime["plugin_sha256"],
             "plugin does not match the training trace and snapshot")
    options = dict(x.split("=", 1) for x in plugin_spec[1:])
    marks = _markers(config, Path(options["virtual"]))
    frozen = {Path(path).resolve(): digest for path, digest in control["frozen"].items()}
    config_sha = file_sha256(config)
    expected = Path(control["expected_guest"])
    inputs = control.get("input_banks")
    if inputs is None:
        bank = control["input_bank"]
        inputs = {} if bank is None else {"input": bank}
    seen = raw["input"]
    _require(isinstance(inputs, dict) and set(inputs) == set(seen),
             "trace input banks do not match the frozen training protocol")
    input_shas = {}
    for name, path in inputs.items():
        bank = Path(path)
        bank_sha = file_sha256(bank)
        _require(Path(seen[name]["path"]).resolve() == bank.resolve()
                 and seen[name]["sha256"] == bank_sha == frozen.get(bank.resolve()),
                 "trace does not use the frozen training bank")
        input_shas[name] = bank_sha
    expected_sha = file_sha256(expected)
    _require(all(frozen.get(path.resolve()) == digest for path, digest in (
        (config, config_sha), (plugin, plugin_sha), (qemu, sha),
        (expected, expected_sha))), "training protocol fingerprints changed")
    bound = {
        "snapshot_id": snapshot.snapshot_id, "cache_sha256": cache_sha,
        "runtime_identity_id": runtime["runtime_identity_id"], "qemu_sha256": sha,
        "plugin_sha256": plugin_sha, "config_sha256": config_sha,
        "training_input_sha256": input_shas, "guest_oracle_sha256": expected_sha,
        "trace_receipt_sha256": file_sha256(receipt), "training_protocol_sha256": file_sha256(protocol),
    }
    if manifest is not None:
        hashes = {k: bound[k] for k in ("qemu_sha256", "plugin_sha256", "config_sha256",
                                       "trace_receipt_sha256")}
        hashes.update(snapshot=snapshot.snapshot_id, trace_sha256=digest,
                      expected_guest_sha256=expected_sha)
        _require(all(manifest.get(k) == v for k, v in hashes.items()),
                 "manifest digests do not match the verified training evidence")
        paths = {"snapshot_path": source, "config": config, "trace": trace,
                 "expected_guest": expected, "trace_receipt": receipt}
        _require(all(Path(manifest[k]).resolve() == path.resolve() for k, path in paths.items())
                 and manifest.get("source_revision") == raw.get("source_revision")
                 and manifest.get("training_input") == seen
                 and manifest.get("exact_guest_oracle") is True
                 and manifest.get("zero_appends") is True
                 and manifest.get("snapshot_tbs") == snapshot.header.record_count,
                 "manifest does not bind the snapshot, oracle and training launch")
    return trace, digest, marks, bound


def plan_activation_layout(snapshot, *, qemu: Path, config: Path, receipt: Path,
                           protocol: Path, terminal: Path | None = None,
                           trace_manifest: Path | None = None,
                           max_moves: int = 8) -> tuple[dict, dict]:
    """Return move-units parameters and provenance for the trace heuristic."""
    _require(type(max_moves) is int and 1 <= max_moves <= 8, "max_moves must be between 1 and 8")
    _require(is_sha256_id(snapshot.snapshot_id), "stored snapshot has no stable identity")
    _require(terminal is None or trace_manifest is None, "choose one marker evidence file")
    try:
        manifest = None
        manifest_sha = None
        if trace_manifest is not None:
            data = trace_manifest.read_bytes()
            manifest, manifest_sha = json.loads(data), "sha256:" + sha256(data).hexdigest()
            _require(isinstance(manifest, dict), "expected a trace manifest JSON object")
        trace_path, digest, marks, bound = _bind(snapshot, qemu, config, receipt, protocol, manifest)
        geometry = _geometry(qemu, snapshot.header.tb_size)
        units = _units(snapshot, geometry)
        trace = _load_trace(trace_path, units, marks, digest,
                            None if terminal is None else _json(terminal), bound["plugin_sha256"],
                            manifest)
        ops, active, writes = _operations(geometry, units, trace)
        limit = snapshot.header.code_address + snapshot.header.code_size
        chosen, positions, before, after = _choose(units, ops, active, limit, max_moves)
    except ActivationLayoutError:
        raise
    except (KeyError, TypeError, OSError, ValueError) as error:
        raise ActivationLayoutError(f"invalid activation layout evidence: {error}") from error

    params = {"move-units": {"deltas": {
        units[i].uid: positions[i] - units[i].entry for i in chosen
    }}}
    report = {
        "schema": "scopt.activation_layout/1", **bound,
        "trace_sha256": digest, "trace_bounds": list(trace.bounds),
        "terminal_evidence_sha256": None if terminal is None else file_sha256(terminal),
        "marker_pcs": {name: hex(pc) for name, pc in marks.items()},
        "end_kind": trace.end_kind, "page_sizes": list(_PAGES),
        "pre_and_marked_write_events": writes,
        "original_projection": list(before), "isolated_projection": list(after),
        "moved_slots": chosen, "tc_offsets": {
            str(i): positions[i] - snapshot.header.code_address for i in chosen
        },
        "model": "repeat observed executions after a write to their entry page",
    }
    if manifest_sha is not None:
        report["trace_manifest_sha256"] = manifest_sha
    return params, report
