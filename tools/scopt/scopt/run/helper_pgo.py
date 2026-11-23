"""Measure captured native helper calls in a fixed-input, sealed x86-64 replay.

The diagnostic cache redirects each annotated CALL to an individual counter
and tail-jumps to the original target. No call-site or continuation moves.
The public-only loader copies the array inside the last record's extended
code extent. Its original search bytes follow the extent as usual. GDB only
reads the private counters at process exit; measured execution runs separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
from pathlib import Path

from ..analysis.host_calls import _crosscheck
from ..decode.x86 import decode_one
from ..formats.enums import SiteKind
from ..formats.qprof_v1 import read_qprof
from ..formats.sc_v5 import ScRecord, sparse_copy
from ..formats.sc_v7 import write_sc7_public
from ..importer.import_cache import open_compiler_snapshot

_COUNTER_MAGIC = b"SCHPROF1"


def _align(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def _diagnostic_cache(snapshot, path):
    header = snapshot.header
    units = snapshot.units()
    if (header.reserved or header.version != 7 or not units
            or len(units) != header.record_count):
        raise ValueError("helper PGO requires an ordinary public v7 capture with all records")
    owner = max(units, key=lambda u: u.end_offset)
    # Actual execution never falls into this generated block: each original
    # TB already exits before its slow paths, literal pool and search data.
    start = _align(owner.tc_offset + owner.tc_size, 8)
    rows, sites = [], []
    for unit in units:
        if unit.meta_status != "ok":
            raise ValueError(f"helper PGO requires complete site annotations for {unit.uid}")
        base = header.code_address + unit.tc_offset
        seen = set()
        for site in unit.sites:
            if site.kind not in (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL):
                continue
            off = site.insn_off
            insn = decode_one(unit.code[off:off + 15], base + off)
            if (off in seen or insn is None or not insn.is_call
                    or not _crosscheck(unit, site, base, insn)[0]):
                raise ValueError(f"unsupported or ambiguous captured call at {unit.uid}+{off}")
            seen.add(off)
            rows.append({"unit": unit.uid, "call_off": off})
            sites.append((unit.slot, off, unit.tc_offset + off, insn.size, site.value))

    # Distinct cache lines/pages for writable data and executable code avoid
    # self-modifying instruction fetch. FastDyn SuperCache requires single TCG.
    stub_size = 23
    data = _align(start + len(rows) * stub_size, header.host_page_size) + len(_COUNTER_MAGIC)
    end = data + 8 * len(rows)
    if end + len(owner.search) >= header.code_size:
        raise ValueError("sealed arena has insufficient space for helper PGO counters")
    payload = bytearray(end - owner.tc_offset - owner.tc_size)
    magic_end = data - owner.tc_offset - owner.tc_size
    payload[magic_end-len(_COUNTER_MAGIC):magic_end] = _COUNTER_MAGIC
    patched = {}
    for i, (slot, off, source, size, target) in enumerate(sites):
        stub = start + i * stub_size
        # pushfq; incq [rip + count]; popfq; jmp qword ptr [rip]; .quad target
        # All argument registers, flags, stack layout and the return PC at
        # helper entry match the original CALL. The stub has no helper call.
        code = (b"\x9c\x48\xff\x05" + struct.pack("<i", data + 8 * i - (stub + 8))
                + b"\x9d\xff\x25\0\0\0\0" + struct.pack("<Q", target))
        pos = stub - owner.tc_offset - owner.tc_size
        payload[pos:pos + stub_size] = code
        if slot not in patched:
            patched[slot] = bytearray(units[slot].code)
        # Prefix the 5-byte near call with a NOP at six-byte indirect sites.
        patched[slot][off:off + size] = (b"\x90" * (size - 5) + b"\xe8"
                                        + struct.pack("<i", stub - source - size))
    parts = []
    for unit in units:
        body = bytes(patched.get(unit.slot, unit.code))
        record = ScRecord.from_bytes(unit.record)
        if unit is owner:
            body += payload
            record = record.replace(tc_size=len(body), end_offset=end + len(unit.search)).with_checksum()
        parts.append((record, body + unit.search))
    write_sc7_public(path, header, parts)
    return rows, header.code_address + data


def _launch(launch, rd, cache, *, qprof=None, trusted=False):
    argv = launch["argv"].copy()
    accel = argv.index("-accel") + 1
    fields = argv[accel].split(",")
    if len([f for f in fields if f.startswith("supercache=")]) != 1:
        raise ValueError("compiled launch must select one SuperCache")
    argv[accel] = ",".join(f"supercache={cache}" if f.startswith("supercache=") else f
                           for f in fields)
    if "-qmp" in argv:
        argv[argv.index("-qmp") + 1] = f"unix:{rd}/qmp,server,nowait"
    cwd = Path(launch["cwd"])
    resets = {(cwd / r["path"]).resolve(): r["size"] for r in launch.get("reset_files", [])}
    copies = {}
    inputs = {}
    for index, arg in enumerate(argv[:-1]):
        if arg != "-object" or not argv[index + 1].startswith("memory-backend-file,"):
            continue
        opts = argv[index + 1].split(",")
        pos = next(i for i, v in enumerate(opts) if v.startswith("mem-path="))
        source = (cwd / opts[pos].split("=", 1)[1]).resolve()
        if source not in copies:
            copy = rd / f"bank-{index}.ram"
            if source in resets:
                with copy.open("wb") as f:
                    f.truncate(resets.pop(source))
            else:
                shutil.copyfile(source, copy)
            with copy.open("rb") as stream:
                inputs[str(source)] = hashlib.file_digest(stream, "sha256").hexdigest()
            copies[source] = copy
        copy = copies[source]
        opts[pos] = f"mem-path={copy}"
        argv[index + 1] = ",".join(opts)
    if resets:
        raise ValueError("compiled launch resets files outside declared RAM banks")
    env = {k: v for k, v in (os.environ | launch["env_set"]).items()
           if not k.startswith("QEMU_SC_PROFILE")}
    env.update({"QEMU_SC_SEALED": "1", "FASTDYN_TIMING_FILE": str(rd / "timing.jsonl"),
                "FASTDYN_WORK_DIR": str(rd)})
    if trusted:
        env["QEMU_SC_TRUSTED"] = "1"
    if qprof is not None:
        env["QEMU_SC_PROFILE"] = str(qprof)
    return argv, env, inputs


def _run(argv, env, cwd, timeout):
    with subprocess.Popen(argv, env=env, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True,
                          text=True, errors="replace") as child:
        try:
            stdout, _ = child.communicate(timeout=timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            # Kill the launched debugger and its QEMU child together.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise
        if child.returncode:
            raise ValueError(f"helper PGO replay failed ({child.returncode}): {stdout[-2500:]}")
        return stdout


def _inputs(snapshot_path, launch_path):
    launch_bytes = launch_path.read_bytes()
    launch = json.loads(launch_bytes)
    if (launch.get("schema") != "fastdyn.compiled-launch/1" or not launch.get("disable_aslr")
            or not Path(launch["argv"][0]).name.startswith("qemu-system-")):
        raise ValueError("expected a fixed-address compiled FastDyn QEMU launch")
    snap = open_compiler_snapshot(snapshot_path)
    argv = launch["argv"]
    exe = Path(argv[0])
    plugins = [Path(argv[i + 1].split(",")[0].removeprefix("file="))
               for i, arg in enumerate(argv[:-1]) if arg in ("--plugin", "-plugin")]
    for key, paths in (("qemu_exe_stat", [exe]), ("plugin_stat", plugins)):
        expected = (snap.runtime or {}).get(key)
        if expected is None and not paths:
            continue
        if not expected or len(paths) != 1:
            raise ValueError(f"helper PGO launch lacks matching {key}")
        st = (Path(launch["cwd"]) / paths[0]).stat()
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != tuple(
                expected[k] for k in ("dev", "ino", "size", "mtime_ns")):
            raise ValueError(f"helper PGO launch {key} differs from captured runtime")
    image_hash = (snap.bundle or {}).get("image_hash")
    if image_hash is None or "-kernel" not in argv:
        raise ValueError("helper PGO needs a firmware-bound snapshot and launch kernel")
    with (Path(launch["cwd"]) / argv[argv.index("-kernel") + 1]).open("rb") as f:
        if "sha256:" + hashlib.file_digest(f, "sha256").hexdigest() != image_hash:
            raise ValueError("helper PGO launch firmware differs from snapshot")
    manifest = snapshot_path / "manifest.json"
    if manifest.is_file():
        captured = json.loads(manifest.read_bytes())["inputs"]
        _check_execution_settings(launch, captured)
    return launch_bytes, launch, snap


def _check_execution_settings(launch, captured):
    """Check the captured device/analysis settings for the training replay."""
    argv = launch["argv"]
    settings = captured["settings"]
    icount = argv[argv.index("-icount") + 1] if "-icount" in argv else ""
    if icount != settings.get("icount", ""):
        raise ValueError("training launch icount differs from captured runtime")
    cpus = settings.get("cpus", [])
    if len(cpus) == 1 and "-cpu" in argv and argv[argv.index("-cpu") + 1] != cpus[0]["cpu"]:
        raise ValueError("training launch CPU differs from capture")
    args = [x for i, flag in enumerate(argv[:-1]) if flag in ("--plugin", "-plugin")
            for x in argv[i + 1].split(",")[1:]]
    names = {"dev": "device_config", "virtual": "virtuals", "modifier": "modifiers",
             "finline": "finline", "fuzzing_schema": "fuzzing_schema",
             "savestate_extra_ranges": "savestate_extra_ranges"}
    keys = set(names) | {"fmu", "fmu_resources", "virtual_libraries", "twintrace_binary"}
    scalars = [arg for arg in args if arg.partition("=")[0] not in keys]
    if scalars != settings.get("plugin_options", []):
        raise ValueError("training launch plugin options differ from capture")
    paths = dict(arg.split("=", 1) for arg in args if "=" in arg)
    for name, content_key in names.items():
        expected = captured.get("content_files", {}).get(content_key)
        if not expected or "sha256" not in expected:
            if paths.get(name) not in (None, "", "None"):
                raise ValueError(f"unexpected training launch {name} file")
            continue
        path = paths.get(name)
        if path is None:
            raise ValueError(f"missing training launch {name} file")
        with (Path(launch["cwd"]) / path).open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != expected["sha256"]:
                raise ValueError(f"training launch {name} differs from captured configuration")


def _guest_lines(launch, log):
    oracle = launch.get("helper_pgo_oracle", {})
    outputs = re.compile(oracle.get("output_pattern", r"^Reg \d+:0x[0-9a-fA-F]+$"))
    done = re.compile(oracle.get("completion_pattern", r"^\[BENCH\]\[fw\]"))
    lines = log.splitlines()
    guest = [line for line in lines if outputs.search(line)]
    if not guest or sum(bool(done.search(line)) for line in lines) != 1:
        raise ValueError("helper PGO requires deterministic guest output and one completion marker")
    return guest


def _publish(src, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as f:
        tmp = Path(f.name)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _collect(snapshot_path, launch_path, output_path, timeout, edge_profile_path, trusted):
    snapshot_path, launch_path = Path(snapshot_path), Path(launch_path)
    launch_bytes, launch, snap = _inputs(snapshot_path, launch_path)
    with tempfile.TemporaryDirectory(prefix="scopt-pgo-") as tmp:
        root = Path(tmp)
        ordinary = root / "control.sc"
        sparse_copy(snapshot_path / "cache.sc", ordinary, fsync=False)
        arms = [("control", ordinary)]
        rows = []
        if output_path is not None:
            diag = root / "profile.sc"
            rows, address = _diagnostic_cache(snap, diag)
            arms.append(("counted", diag))
        oracle = None
        if edge_profile_path:
            arms.append(("edges", ordinary))
        for name, cache in arms:
            rd = root / name
            rd.mkdir()
            qprof = root / "control.qprof" if name == "edges" else None
            argv, env, inputs = _launch(launch, rd, cache, qprof=qprof, trusted=trusted)
            if name != "counted" or not rows:
                log = _run(["setarch", "x86_64", "-R", *argv], env, launch["cwd"], timeout)
            else:
                dump = root / "counts.bin"
                script = rd / "count.gdb"
                script.write_text(f"""set pagination off
set confirm off
set debuginfod enabled off
set disable-randomization on
catch syscall exit_group
run
python
with open({str(dump)!r}, "wb") as output:
    output.write(gdb.selected_inferior().read_memory({address-len(_COUNTER_MAGIC)}, {len(_COUNTER_MAGIC)+len(rows)*8}))
end
continue
printf "SCOPT_INFERIOR_EXIT %d\\n", $_exitcode
""")
                log = _run(["gdb", "-nx", "-q", "-batch", "-x", str(script), "--args", *argv],
                           env, launch["cwd"], timeout)
                if "SCOPT_INFERIOR_EXIT 0" not in log:
                    raise ValueError("counted sealed replay failed to complete: " + log[-1600:])
            guest = _guest_lines(launch, log)
            current = (guest, inputs)
            if oracle is not None and current != oracle:
                raise ValueError("ordinary and counted replay guest outputs or training inputs differ")
            oracle = current
        if edge_profile_path:
            edge = read_qprof(root / "control.qprof").header
            if (not edge.clean_exit or edge.overflow or not edge.warm
                    or edge.loaded_header_checksum != snap.header.checksum
                    or edge.loaded_record_count != snap.header.record_count):
                raise ValueError("incomplete or mismatched sealed edge profile")
            _publish(root / "control.qprof", edge_profile_path)
        if output_path is None:
            return None
        raw = dump.read_bytes() if rows else _COUNTER_MAGIC
        if not raw.startswith(_COUNTER_MAGIC):
            raise ValueError("sealed diagnostic counter array was not loaded")
        values = struct.unpack(f"<{len(rows)}Q", raw[len(_COUNTER_MAGIC):])
        for row, count in zip(rows, values, strict=True):
            row["count"] = count
        result = {"schema": "scopt.helper-call-profile/1", "snapshot_id": snap.snapshot_id,
                  "scope": "whole_run", "launch_sha256": hashlib.sha256(launch_bytes).hexdigest(),
                  "inputs_sha256": inputs, "guest_output": guest, "counts": rows,
                  "complete": True}
        profile = root / "helper.json"
        profile.write_text(json.dumps(result, separators=(",", ":")))
        _publish(profile, output_path)
        return result


def collect(snapshot_path, launch_path, output_path, *, timeout=300, edge_profile_path=None, trusted=False):
    """Publish exact per-site counts after matching fixed-input replays."""
    return _collect(snapshot_path, launch_path, output_path, timeout, edge_profile_path, trusted)


def collect_edges(snapshot_path, launch_path, edge_profile_path, *, timeout=300, trusted=False):
    """Collect the dynamic TB edges only, using the same sealed control and oracle."""
    _collect(snapshot_path, launch_path, None, timeout, edge_profile_path, trusted)
