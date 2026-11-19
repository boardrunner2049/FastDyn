"""Package optional callback-replay hints from exact v5 captures."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
import tomllib

from ..canon import sha256_id
from ..formats import artifact as A
from ..formats.bundle_v1 import (admission_facts, check_bundle, identity_bundle,
                                 read_runtime_identity)
from ..formats.enums import TbFlag
from ..formats.sc_v5 import read_sc, sha256_sparse, sparse_copy, structural_findings
from ..importer.import_cache import ImportCache, open_snapshot
from ..importer.join import join
from ..paths import fastdyn_root
from .launcher import file_sha256
from .toml_rewrite import memory_banks, normalized_config_sha256

HINTS = "cache.sc.restore-hints"
MAGIC = "FD_SC_RESTORE_HINTS"
SCHEMA = "scopt.restore_hints/1"
INTERNAL_CALLBACKS = {0x20800050}
VIRTUALS = {"benchmark_start", "benchmark_end", "printreg"}
HEADER = re.compile(rb"FD_SC_RESTORE_HINTS 1 (0x[0-9a-f]+) (0x[0-9a-f]+) ([0-9]+)")
ROW = re.compile(rb"(0x[0-9a-f]+) (0x[0-9a-f]+)")
MODIFIER = re.compile(r"r(?:[0-9]|1[0-2])\s*<-\s*(?:0x[0-9a-fA-F]+|[0-9]+)")
MACHINE_KEYS = {
    "platform", "supercache", "supercache_flat", "log_options", "coverage", "monitor_port",
    "qmp_socket", "log_file", "enable_gdb", "stop_on_start", "launch_gdb", "semihosting",
    "semihosting_config", "finline", "print_command",
}
CPU_KEYS = {
    "arch", "machine", "cpu", "binary", "plugin_library", "init_nsvtor", "device", "virtuals",
    "modifiers", "plugins", "twintrace", "hardware_trace", "existing_config_path", "logger_content",
}
NONE_VALUES = (None, False, "", "none", "None", "off", "0")
PLUGIN_OPTIONS = {
    "coverage": {"0"}, "edge_coverage": {"0"}, "fuzzing": {"0"}, "exact_budget_stop": {"0"},
    "twintrace": {"off", "None", "none", "0"}, "twintrace_binary": {"None", "none", ""},
    "finline": {"None", "none", "off", ""}, "log_level": {"20", "30", "40", "50"},
}
SETTINGS_KEYS = {"cpus", "globals", "icount", "log_options", "platform", "plugin_options",
                 "supercache_flat"}
CONTENT_KEYS = {
    "device_config", "virtuals", "modifiers", "fastdyn_config", "fuzzing_schema",
    "savestate_extra_ranges",
}


class RestoreHintsError(ValueError):
    """Unbound or unsupported restore-hint inputs."""


def require(condition, message):
    if not condition:
        raise RestoreHintsError(message)


def _pc(row):
    at = row.get("at")
    require(type(at) in (str, int), "callback address must be an exact literal")
    pc = int(at, 0) if isinstance(at, str) else at
    require(0 <= pc < (1 << 32) and not pc % 2, "invalid Thumb callback address")
    return pc


def callback_addresses(config, manifest):
    """Restrict the offline proof to known, static single-core benchmark configuration."""
    require(set(config) <= {"Memory", "Machine", "CPU", "Device"},
            "unsupported FastDyn top-level configuration")
    mach = config.get("Machine", {})
    cpus = config.get("CPU", {})
    require(isinstance(mach, dict) and set(mach) <= MACHINE_KEYS
            and isinstance(cpus, dict) and set(cpus) == {"cpu0"}, "unsupported machine or CPU options")
    cpus = cpus["cpu0"] if isinstance(cpus["cpu0"], list) else [cpus["cpu0"]]
    require(len(cpus) == 1 and isinstance(cpus[0], dict), "exactly one CPU is required")
    cpu = cpus[0]
    require(set(cpu) <= CPU_KEYS and [cpu.get(k) for k in ("arch", "machine", "cpu")] ==
            ["arm", "cortexm", "cortex-m3"], "unsupported CPU configuration")
    require(mach.get("platform") == "generic-cortexm" and mach.get("supercache_flat") is False
            and bool(mach.get("supercache")), "requires non-flat generic Cortex-M capture")
    require(mach.get("log_options") == "none" and mach.get("coverage") is False,
            "restore hints require tracing and coverage disabled")
    require(all(mach.get(k, False) is False for k in ("enable_gdb", "stop_on_start", "launch_gdb"))
            and mach.get("finline") in NONE_VALUES, "debugging or inline instrumentation is active")
    banks = tuple(memory_banks(config))
    require(banks and all(bank.backend == "file" and bank.share is False for bank in banks),
            "restore hints require private file-backed memory")
    require(cpu.get("twintrace") in NONE_VALUES and cpu.get("existing_config_path") in NONE_VALUES,
            "dynamic tracing or external plugin configuration is active")
    logger = cpu.get("logger_content", "")
    require(isinstance(logger, str) and all(not line.strip() or line.lstrip().startswith("#")
                                           for line in logger.splitlines()),
            "nonempty plugin logger configuration")
    require(cpu.get("device", {"enabled": False}) == {"enabled": False},
            "CPU device configuration is active")
    devices = config.get("Device", {})
    require(isinstance(devices, dict) and set(devices) <= {"Models", "remaining_space"},
            "unknown device configuration")
    remaining = devices.get("remaining_space", {})
    require(isinstance(remaining, dict), "unknown device handler configuration")
    for handler in remaining.get("handlers", ()):
        require(isinstance(handler, dict) and handler.get("enabled") is False, "active device handler")
    plugins = cpu.get("plugins", {})
    require(plugins in ({}, {"introspection": {"enabled": False}}), "dynamic CPU plugin configured")

    inputs = manifest.get("inputs", {})
    settings = inputs.get("settings", {})
    natives = inputs.get("native_files", {})
    contents = inputs.get("content_files", {})
    require(set(natives) <= {"firmware", "plugin", "qemu", "fmu", "bios", "drive"}
            and not any(natives.get(key) for key in ("fmu", "bios", "drive")),
            "external instrumentation images are unsupported")
    require(set(contents) <= CONTENT_KEYS, "unknown generated instrumentation file")
    empty_dev = contents.get("device_config") or {}
    require(empty_dev.get("sha256") == sha256_id(b"{}").split(":")[-1] and empty_dev.get("size") == 2
            and not contents.get("fuzzing_schema") and not contents.get("savestate_extra_ranges"),
            "generated device/fuzzing/savestate configuration is active")
    require(set(settings) <= SETTINGS_KEYS and settings.get("log_options") == "none"
            and not settings.get("icount")
            and not settings.get("globals") and settings.get("supercache_flat") is False,
            "manifest captures unsupported runtime options")
    parsed = {}
    for opt in settings.get("plugin_options", ()):
        require(isinstance(opt, str), "unsupported generated plugin option")
        key, sep, val = opt.partition("=")
        require(sep and key not in parsed and val in PLUGIN_OPTIONS.get(key, ()),
                f"unsupported generated plugin option: {key}")
        parsed[key] = val
    require({"coverage", "edge_coverage", "fuzzing", "exact_budget_stop", "twintrace"} <= parsed.keys(),
            "manifest does not bind inactive runtime instrumentation")
    facts = admission_facts(manifest=manifest, qemu_sha256=None, check_files=False)
    require(facts["exec_vaddr_aliases"] == [] and not facts["tb_start_cb_possible"]
            and not facts["ac4_excluded_virtuals_present"], "unproved execution aliases or TB hooks")

    pcs = set(INTERNAL_CALLBACKS)
    for callback in cpu.get("virtuals", ()):
        require(isinstance(callback, dict) and set(callback) <= {"at", "instruction", "args"} and callback.get("instruction") in VIRTUALS,
                "unsupported virtual callback")
        pcs.add(_pc(callback))
    for modifier in cpu.get("modifiers", ()):
        require(isinstance(modifier, dict) and set(modifier) == {"at", "patch"} and MODIFIER.fullmatch(modifier["patch"]),
                "unsupported dynamic or PC-writing modifier")
        pcs.add(_pc(modifier))
    return cpu, frozenset(pcs)


def _record_eligible(unit, chunk, pcs):
    if (unit.meta_status != "ok" or unit.origin != "elf_verified"
            or len(unit.vaddrs) != 1 or chunk.vaddr != unit.vaddrs[0]
            or unit.key.page_addr1 is not None or chunk.tb_chunk_version != 1
            or chunk.native_fragments or chunk.stub_start is not None
            or int(chunk.tb_flags) != chunk.flags
            or chunk.tb_flags & (TbFlag.CAPTURE_ERROR | TbFlag.CAPTURE_OVERFLOW |
                                 TbFlag.GUEST_BYTES_TRUNCATED | TbFlag.PLUGIN_CBS_PRESENT)
            or len(chunk.insns) != unit.icount or unit.icount <= 0
            or unit.guest_size <= 0 or len(chunk.guest_bytes) != unit.guest_size):
        return False
    start = chunk.vaddr
    end = start + unit.guest_size
    return 0 <= start < end <= 1 << 32 and not any(start <= pc < end for pc in pcs)


def validate_restore_hints(data, sc):
    """Parse canonical ASCII and verify every nominated offset/checksum tuple."""
    require(sc.loadable and sc.header.version == 5 and sc.header.checksum_ok,
            "invalid cache header/checksum")
    require(not sc.skipped and sc.count == sc.header.record_count, "invalid cache record table")
    rows = data.split(b"\n")[:-1]
    header = HEADER.fullmatch(rows[0]) if rows else None
    require(header is not None and data.endswith(b"\n"), "invalid restore-hints magic or format")
    bound = (int(header[1], 16), int(header[2], 16), int(header[3], 10))
    expected = (sc.header.runtime_signature, sc.header.checksum, sc.header.record_count)
    require(bound == expected, "restore-hints header binding mismatch")
    by_offset = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    require(len(by_offset) == sc.count, "duplicate cache record offsets")
    prior = -1
    result = []
    for row in rows[1:]:
        fields = ROW.fullmatch(row)
        require(fields is not None, "invalid restore-hints record row")
        offset, checksum = (int(s, 16) for s in fields.groups())
        record = by_offset.get(offset)
        require(offset > prior and record is not None and record.checksum == checksum
                and record.checksum_ok, "invalid or duplicate restore-hints record/checksum")
        result.append((offset, checksum))
        prior = offset
    require(bool(result), "no independently eligible records")
    return tuple(result)


def _sources(directory):
    doc = json.loads((directory / "snapshot.json").read_bytes())
    captures = doc.get("capture", {})
    require(captures.get("complete") is True, "snapshot has no complete capture")
    collected = []
    for capture in captures.get("files", ()):
        name = capture["name"]
        require(Path(name).name == name and name.endswith(".scm"), "invalid capture filename")
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "missing immutable capture file")
        require(sha256_id(path.read_bytes()) == capture["sha256"], "snapshot capture digest mismatch")
        collected.append(path)
    require(bool(collected), "snapshot has no bound SCM")
    return collected


def _config_image(value, captured):
    path = Path(value)
    if not path.is_absolute():
        path = fastdyn_root() / path
    path = path.resolve(strict=True)
    require(path == Path(captured["path"]).resolve(strict=True), "configured native image path mismatch")
    return path


def _fingerprint(path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def package_restore_hints(snapshot_dir, config, qemu, store):
    """Certify an unchanged v5 cache and seal its independently derived hints."""
    from ..manager import PassManager
    from ..validate import ValidateAndPackage

    directory = Path(snapshot_dir).resolve(strict=True)
    config = Path(config).resolve(strict=True)
    qemu = Path(qemu).resolve(strict=True)
    sc_path = directory / "cache.sc"
    manifest_path = directory / "manifest.json"
    runtime_path = directory / "runtime_identity.json"
    frozen = {p: _fingerprint(p) for p in (config, qemu, sc_path, manifest_path, runtime_path)}
    sc = read_sc(sc_path)
    require(sc.loadable and sc.header.version == 5, "invalid v5 cache")
    require(not structural_findings(sc, scan_zeros=False), "cache has invalid header or records")
    snapshot = open_snapshot(directory)
    require(sc.snapshot_id() == snapshot.snapshot_id, "snapshot/cache content identity mismatch")
    runtime = read_runtime_identity(runtime_path)
    cfg_text = config.read_text()
    require(normalized_config_sha256(cfg_text) == runtime["normalized_config_sha256"],
            "restore-hints configuration does not match the captured RuntimeIdentity")
    require(file_sha256(qemu) == runtime["qemu_sha256"], "QEMU digest mismatch")
    manifest = json.loads(manifest_path.read_bytes())
    cpu, pcs = callback_addresses(tomllib.loads(cfg_text), manifest)
    native = manifest["inputs"]["native_files"]
    plugin = _config_image(cpu["plugin_library"], native["plugin"])
    firmware = _config_image(cpu["binary"], native["firmware"])
    frozen.update({p: _fingerprint(p) for p in (plugin, firmware)})
    require(plugin.name == "libfastdyn.so" and file_sha256(plugin) == runtime["plugin_sha256"],
            "FastDyn plugin digest or name mismatch")
    bundle = identity_bundle(config, manifest)
    check_bundle(snapshot.bundle, bundle)
    originals = _sources(directory)
    frozen.update({path: _fingerprint(path) for path in originals})
    checked = ImportCache(sc_path, originals, manifest=manifest_path,
                          bundle=bundle, runtime_identity=runtime, firmware=firmware,
                          plugin=plugin, strict=True).run().snapshot
    require(checked.snapshot_id == snapshot.snapshot_id, "raw capture failed identity re-import")
    binding = join(sc, originals)
    require(not binding.capture_problems, "incomplete or foreign capture metadata")
    rows = []
    for unit in checked.units():
        captured = binding.slots[unit.slot]
        if captured.bound and _record_eligible(unit, captured.chunk, pcs):
            record = sc.records[unit.slot]
            rows.append((record.tb_offset, record.checksum))
    rows.sort()
    header = sc.header
    hints = (f"{MAGIC} 1 {header.runtime_signature:#x} {header.checksum:#x} {header.record_count}\n"
             + "".join(f"{off:#x} {ck:#x}\n" for off, ck in rows)).encode("ascii")
    require(validate_restore_hints(hints, sc) == tuple(rows), "hint tuple roundtrip mismatch")
    cache_hash = sha256_sparse(sc_path)
    proof = {
        "schema": SCHEMA, "sha256": sha256_id(hints), "qemu_sha256": runtime["qemu_sha256"],
        "plugin_sha256": runtime["plugin_sha256"], "config_sha256": runtime["normalized_config_sha256"],
        "input_snapshot_id": snapshot.snapshot_id, "cache_sha256": "sha256:" + cache_hash,
        "scm_sha256": sorted(sha256_id(path.read_bytes()) for path in originals),
        "admitted_count": len(rows), "record_count": header.record_count,
        "runtime_signature": hex(header.runtime_signature), "header_checksum": hex(header.checksum),
    }

    # Keep the ordinary V1–V9 certificate and SCM bytes. A deterministic
    # certification clock prevents the wrapper's content ID from varying.
    clock = lambda: "1970-01-01T00:00:00.000Z"
    with TemporaryDirectory(prefix="scopt-restore-identity-") as temporary:
        candidate = PassManager(snapshot, "identity", clock=clock).execute()
        base = ValidateAndPackage(candidate, temporary, clock=clock, join_result=binding).run()
        require(base.status == "published", f"identity certification failed: {base.reason}")
        source = Path(base.artifact_dir)
        manifest_rows = A.parse_manifest((source / A.MANIFEST).read_bytes())
        require(manifest_rows.get("cache.sc") == cache_hash, "identity package changed the cache bytes")
        produced = Counter(manifest_rows[name] for name in A.derived_captures(source))
        require(produced == Counter(d[7:] for d in proof["scm_sha256"]),
                "identity package changed the original SCM")
        stage = A.staging_dir(store)
        try:
            for name in manifest_rows:
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if name == "cache.sc":
                    sparse_copy(source / name, destination)
                else:
                    shutil.copyfile(source / name, destination)
            require(sha256_sparse(stage / "cache.sc") == cache_hash,
                    "staged cache differs from the source")
            sidecar = json.loads((stage / A.SIDECAR).read_bytes())
            # Identity-pass wall time is not proof metadata.
            for step in sidecar["pipeline"]:
                step["duration_s"] = 0.0
            sidecar["x_restore_hints"] = proof
            require(not A.sidecar_errors(sidecar), "restore-hints artifact sidecar is invalid")
            A.write_json_file(stage / A.SIDECAR, sidecar)
            (stage / HINTS).write_bytes(hints)
            require(all(_fingerprint(p) == fingerprint for p, fingerprint in frozen.items()),
                    "restore-hints source changed during packaging")
            artifact_id, result, status = A.publish(stage, store, known={"cache.sc": cache_hash})
        finally:
            if stage.exists():
                A.remove_tree(stage)
    return {"artifact_dir": str(result), "artifact_id": artifact_id, "status": status, **proof}
