"""ExecutionBundle v1 reader, identity bundle and RuntimeIdentity (§4.2). WP8.

* :func:`read_bundle` validates a bundle against
  ``schemas/execution_bundle.v1.json``, rejects an unknown ``schema_version``
  and recomputes ``bundle_id``. Errors: :class:`BundleSchemaError`,
  :class:`BundleIdMismatch`.
* :func:`identity_bundle` builds the identity bundle of a configuration from
  its TOML and the FastDyn manifest (``<cache>.sc.fastdyn.json``) that the
  deploy oracle wrote for it. Run-directory paths never enter it, so every
  per-run TOML of one base TOML has the same ``bundle_id``. A firmware or
  plugin whose size/mtime differ from the manifest raises :class:`BundleStale`.
* :func:`check_bundle` compares two bundles and raises :class:`BundleMismatch`
  (reason code ``bundle_mismatch``) naming the differing fields.
* :func:`runtime_identity` is kept outside the bundle (``runtime_identity.json``):
  QEMU/plugin identity, meson build options, the ``.sc`` header's runtime
  fields, the FastDyn commit and the normalized config hash.

Field constructions (exact; any change alters every bundle_id):

``original_image_hash`` = ``image_hash``
    sha256 of the firmware ELF bytes (manifest ``native_files.firmware.path``).
``patch_plan_hash``
    sha256(canonical ``{virtuals, modifiers, finline, fuzzing_schema,
    preprocessing_artifact_*}``), each value the manifest content sha256 as
    ``"sha256:…"`` (``null`` when absent, ``"missing"`` when FastDyn recorded
    a missing file).
``patch_bindings_hash``
    sha256(canonical ``{"kind":"identity","plan":patch_plan_hash}``).
``origin_map_hash``
    sha256(``"identity-origin/1:" + image_hash``).
``target``
    ``isa`` and ``features`` from the QEMU cpu model (target/arm/tcg/cpu-v7m.c
    ``set_feature`` calls, lowercased ``ARM_FEATURE_*`` names), ``cpu`` from
    the manifest settings.
``entry_points``
    ELF ``e_entry``; ``initial_sp`` and ``reset_vector`` are words 0 and 1 of
    the vector table at ``init_nsvtor``, read from the ELF's PT_LOAD segments.
``memory_map_hash``
    sha256(canonical ``{platform, machine, banks:[{id, base, size, type,
    backend, share}] sorted by id, dev_config_sha256}``).
``contract_id``
    sha256(canonical ``{plugin_options (manifest order), log_options, icount,
    coverage, edge_coverage, fuzzing, twintrace, supercache_flat:false}``).
``instrumentation_hash``
    sha256(canonical ``{plugin: sha256(.so bytes), virtual_libraries:
    [sha256(bytes)], virtuals, modifiers, finline}``).
``device_model_hash``
    sha256(canonical ``{dev_config_sha256, platform, machine, cpu, fmu}``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..canon import canonical_json, hexaddr, is_sha256_id, sha256_id
from ..ids import bundle_id as _bundle_id
from ..ids import runtime_identity_id
from . import schemas

__all__ = [
    "SCHEMA_VERSION", "BundleError", "BundleSchemaError", "BundleIdMismatch", "BundleStale",
    "BundleMismatch", "read_bundle", "validate_bundle", "identity_bundle", "check_bundle",
    "runtime_identity", "read_runtime_identity", "check_runtime_identity", "cpu_model",
    "meson_build_options", "PATCH_PLAN_KEYS", "CPU_MODELS", "run_info_fields",
    "manifest_patch_plan_hash", "manifest_contract_id", "manifest_bundle_fields",
]

SCHEMA_VERSION = 1
RUNTIME_IDENTITY_SCHEMA = "scopt.runtime_identity/1"
# v2 = v1 plus the B3/B4 "admission" block (design §7.2, §9.1 V20; WP17).
RUNTIME_IDENTITY_SCHEMA_V2 = "scopt.runtime_identity/2"
RUNTIME_IDENTITY_SCHEMAS = (RUNTIME_IDENTITY_SCHEMA, RUNTIME_IDENTITY_SCHEMA_V2)

# target/arm/tcg/cpu-v7m.c (QEMU 12f5e688): isa and set_feature() per model.
CPU_MODELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "cortex-m0": ("armv6-m", ("m", "v6")),
    "cortex-m3": ("armv7-m", ("m", "m_main", "v7")),
    "cortex-m4": ("armv7-m", ("m", "m_main", "thumb_dsp", "v7")),
    "cortex-m7": ("armv7-m", ("m", "m_main", "thumb_dsp", "v7")),
    "cortex-m33": ("armv8-m.main", ("m", "m_main", "m_security", "thumb_dsp", "v8")),
    "cortex-m55": ("armv8-m.main", ("m", "m_main", "m_security", "thumb_dsp", "v8", "v8_1m")),
}

PATCH_PLAN_KEYS = ("virtuals", "modifiers", "finline", "fuzzing_schema")


class BundleError(ValueError):
    code = "bundle_error"

    def __init__(self, detail: str = "", *, code: str | None = None, **info: Any):
        if code is not None:
            self.code = code
        self.detail = detail
        self.info = info
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class BundleSchemaError(BundleError):
    code = "bundle_schema"


class BundleIdMismatch(BundleError):
    code = "bundle_id_mismatch"


class BundleStale(BundleError):
    code = "bundle_stale"


class BundleMismatch(BundleError):
    code = "bundle_mismatch"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _file_sha256_id(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def _cid(obj: Any) -> str:
    return sha256_id(canonical_json(obj))


def _content_sha(entry: Any) -> str | None:
    """Manifest content-file entry -> ``"sha256:…"`` / None / ``"missing"``."""
    if entry is None:
        return None
    if isinstance(entry, Mapping):
        if "sha256" in entry:
            return "sha256:" + str(entry["sha256"])
        if "missing" in entry:
            return "missing"
    raise BundleSchemaError(f"unexpected manifest content entry {entry!r}", code="manifest_format")


def _check_native(name: str, entry: Any) -> str | None:
    """Path of a manifest native file after checking size/mtime are unchanged."""
    if entry is None:
        return None
    if not isinstance(entry, Mapping) or "path" not in entry:
        raise BundleStale(f"{name}: manifest records it as missing ({entry!r})")
    p = entry["path"]
    try:
        st = os.stat(p)
    except FileNotFoundError:
        raise BundleStale(f"{name}: {p} no longer exists") from None
    if st.st_size != entry.get("size") or st.st_mtime_ns != entry.get("mtime_ns"):
        raise BundleStale(f"{name}: {p} changed since the manifest was written "
                          f"(size {entry.get('size')}->{st.st_size}, "
                          f"mtime_ns {entry.get('mtime_ns')}->{st.st_mtime_ns})")
    return p


def cpu_model(cpu: str) -> tuple[str, list[str]]:
    try:
        isa, feats = CPU_MODELS[cpu]
    except KeyError:
        raise BundleSchemaError(f"cpu model {cpu!r} is not a supported M-profile model",
                                code="unsupported_cpu") from None
    return isa, sorted(feats)


def _elf_entry_points(path: str, vtor: int) -> dict[str, str]:
    from elftools.elf.elffile import ELFFile

    with open(path, "rb") as f:
        elf = ELFFile(f)
        entry = elf.header["e_entry"]
        words = None
        for seg in elf.iter_segments():
            if seg["p_type"] != "PT_LOAD":
                continue
            for base in (seg["p_paddr"], seg["p_vaddr"]):
                if base <= vtor and vtor + 8 <= base + seg["p_filesz"]:
                    data = seg.data()[vtor - base: vtor - base + 8]
                    words = (int.from_bytes(data[:4], "little"), int.from_bytes(data[4:], "little"))
                    break
            if words:
                break
    if words is None:
        raise BundleSchemaError(f"no PT_LOAD segment of {path} holds the vector table at "
                                f"0x{vtor:x}", code="no_vector_table")
    return {"elf_entry": hexaddr(entry), "vtor": hexaddr(vtor),
            "initial_sp": hexaddr(words[0]), "reset_vector": hexaddr(words[1])}


def _plugin_options(opts: list[str]) -> dict[str, str]:
    out = {}
    for o in opts:
        k, _, v = str(o).partition("=")
        out[k] = v
    return out


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------

def validate_bundle(doc: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(doc, Mapping):
        raise BundleSchemaError("bundle is not a JSON object")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise BundleSchemaError(f"schema_version {doc.get('schema_version')!r} is not "
                                f"{SCHEMA_VERSION}", code="unknown_schema_version")
    errs = schemas.errors(dict(doc), "execution_bundle.v1")
    if errs:
        raise BundleSchemaError("; ".join(errs[:5]), errors=errs)
    want = _bundle_id(doc)
    if doc["bundle_id"] != want:
        raise BundleIdMismatch(f"bundle_id {doc['bundle_id']} != recomputed {want}")
    return dict(doc)


def read_bundle(path_or_doc: str | os.PathLike | Mapping[str, Any]) -> dict[str, Any]:
    """Load and validate an ExecutionBundle v1 (file path or parsed JSON)."""
    if isinstance(path_or_doc, Mapping):
        return validate_bundle(path_or_doc)
    try:
        doc = json.loads(Path(path_or_doc).read_text(encoding="utf-8"))
    except ValueError as e:
        raise BundleSchemaError(f"{path_or_doc}: not JSON ({e})") from e
    return validate_bundle(doc)


# --------------------------------------------------------------------------
# identity bundle
# --------------------------------------------------------------------------

def identity_bundle(config: str | os.PathLike, manifest: str | os.PathLike | Mapping[str, Any],
                    ) -> dict[str, Any]:
    """The identity ExecutionBundle v1 for ``config`` + its FastDyn ``manifest``."""
    from ..run.toml_rewrite import memory_banks

    cfg_text = Path(config).read_text(encoding="utf-8")
    cfg = tomllib.loads(cfg_text)
    man = manifest if isinstance(manifest, Mapping) else json.loads(Path(manifest).read_text())
    if man.get("version") != 1 or not isinstance(man.get("inputs"), Mapping):
        raise BundleSchemaError("not a FastDyn manifest v1", code="manifest_format")
    inputs = man["inputs"]
    cf = inputs.get("content_files", {})
    nf = inputs.get("native_files", {})
    st = inputs.get("settings", {})
    if st.get("supercache_flat") is not False:
        raise BundleSchemaError("supercache_flat must be false", code="unsupported_flat")
    cpus = st.get("cpus") or []
    if len(cpus) != 1:
        raise BundleSchemaError(f"{len(cpus)} CPUs; exactly one is supported", code="unsupported_smp")
    cpu0 = cpus[0]

    # the TOML must describe the same machine as the manifest
    mach = cfg.get("Machine", {})
    toml_cpus = [t for v in (cfg.get("CPU") or {}).values()
                 for t in (v if isinstance(v, list) else [v]) if isinstance(t, Mapping)]
    if mach.get("platform") != st.get("platform") or \
            (toml_cpus and toml_cpus[0].get("cpu") != cpu0.get("cpu")):
        raise BundleStale("the TOML and the manifest describe different machines",
                          code="config_manifest_mismatch")

    fw = _check_native("firmware", nf.get("firmware"))
    if fw is None:
        raise BundleSchemaError("manifest has no firmware", code="manifest_format")
    plugin = _check_native("plugin", nf.get("plugin"))
    vlibs = [_check_native(k, nf[k]) for k in sorted(nf) if k.startswith("virtual_library_")]
    fmu = _check_native("fmu", nf.get("fmu"))

    image_hash = _file_sha256_id(fw)
    patch_plan_hash = manifest_patch_plan_hash(man)
    plan = _manifest_plan(man)

    isa, feats = cpu_model(str(cpu0.get("cpu")))
    vtor = int(str(cpu0.get("init_nsvtor", "0")), 0)
    dev_sha = _content_sha(cf.get("device_config"))
    banks = sorted((b.canonical() for b in memory_banks(cfg)), key=lambda b: b["id"])

    bundle: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "original_image_hash": image_hash,
        "image_hash": image_hash,
        "patch_plan_hash": patch_plan_hash,
        "patch_bindings_hash": _cid({"kind": "identity", "plan": patch_plan_hash}),
        "origin_map_hash": sha256_id(("identity-origin/1:" + image_hash).encode()),
        "target": {"isa": isa, "mode": "thumb", "endianness": "little",
                   "cpu": str(cpu0.get("cpu")), "features": feats},
        "entry_points": _elf_entry_points(fw, vtor),
        "memory_map_hash": _cid({
            "platform": st.get("platform"), "machine": cpu0.get("machine"),
            "banks": banks, "dev_config_sha256": dev_sha}),
        "contract_id": manifest_contract_id(man),
        "instrumentation_hash": _cid({
            "plugin": None if plugin is None else _file_sha256_id(plugin),
            "virtual_libraries": [_file_sha256_id(p) for p in vlibs],
            "virtuals": plan["virtuals"], "modifiers": plan["modifiers"],
            "finline": plan["finline"]}),
        "device_model_hash": _cid({
            "dev_config_sha256": dev_sha, "platform": st.get("platform"),
            "machine": cpu0.get("machine"), "cpu": cpu0.get("cpu"),
            "fmu": None if fmu is None else _file_sha256_id(fmu)}),
    }
    bundle["bundle_id"] = _bundle_id(bundle)
    return validate_bundle(bundle)


# --------------------------------------------------------------------------
# manifest-derived fields (shared with the importer, scopt.importer.import_cache)
# --------------------------------------------------------------------------

def _manifest_plan(man: Mapping[str, Any]) -> dict[str, str | None]:
    cf = man.get("inputs", {}).get("content_files", {})
    plan = {k: _content_sha(cf.get(k)) for k in PATCH_PLAN_KEYS}
    plan.update({k: _content_sha(v) for k, v in cf.items()
                 if k.startswith("preprocessing_artifact_")})
    return plan


def manifest_patch_plan_hash(man: Mapping[str, Any]) -> str:
    """``patch_plan_hash`` from a FastDyn manifest's ``inputs.content_files``."""
    return _cid(_manifest_plan(man))


def manifest_contract_id(man: Mapping[str, Any]) -> str:
    """``contract_id`` from a FastDyn manifest's ``inputs.settings``."""
    st = man.get("inputs", {}).get("settings", {})
    popts = list(st.get("plugin_options") or [])
    pmap = _plugin_options(popts)
    return _cid({
        "plugin_options": popts, "log_options": st.get("log_options"),
        "icount": st.get("icount"), "coverage": pmap.get("coverage"),
        "edge_coverage": pmap.get("edge_coverage"), "fuzzing": pmap.get("fuzzing"),
        "twintrace": pmap.get("twintrace"), "supercache_flat": False})


def manifest_bundle_fields(man: Mapping[str, Any], *, files: bool = True
                           ) -> tuple[dict[str, Any], list[str]]:
    """Bundle fields that a FastDyn manifest determines without the TOML.

    Returns ``(fields, unverifiable)``. ``patch_plan_hash`` and ``contract_id``
    come from the manifest alone. With ``files``, the native files the
    manifest names are read (after the same size/mtime check as
    :func:`identity_bundle`; a changed file raises :class:`BundleStale`) to
    give ``image_hash``, ``target``, ``entry_points``,
    ``instrumentation_hash`` and ``device_model_hash``. A field that cannot be
    derived (no firmware or plugin entry, no single CPU) is listed in
    ``unverifiable``. ``memory_map_hash`` needs the TOML's memory banks and
    is always unverifiable here; ``original_image_hash``,
    ``patch_bindings_hash`` and ``origin_map_hash`` describe the patch plan
    and are not checked against a prep run.
    """
    out: dict[str, Any] = {"patch_plan_hash": manifest_patch_plan_hash(man),
                           "contract_id": manifest_contract_id(man)}
    unver = ["memory_map_hash"]
    derived = ("image_hash", "target", "entry_points", "instrumentation_hash",
               "device_model_hash")
    if not files:
        return out, unver + list(derived)
    inputs = man.get("inputs", {})
    cf = inputs.get("content_files", {})
    nf = inputs.get("native_files", {})
    st = inputs.get("settings", {})
    cpus = st.get("cpus") or []
    cpu0 = cpus[0] if len(cpus) == 1 and isinstance(cpus[0], Mapping) else None
    fw = _check_native("firmware", nf["firmware"]) if nf.get("firmware") is not None else None
    if fw is not None:
        out["image_hash"] = _file_sha256_id(fw)
    else:
        unver.append("image_hash")
    if cpu0 is not None and str(cpu0.get("cpu")) in CPU_MODELS:
        isa, feats = cpu_model(str(cpu0.get("cpu")))
        out["target"] = {"isa": isa, "mode": "thumb", "endianness": "little",
                         "cpu": str(cpu0.get("cpu")), "features": feats}
        dev_sha = _content_sha(cf.get("device_config"))
        fmu = _check_native("fmu", nf.get("fmu"))
        out["device_model_hash"] = _cid({
            "dev_config_sha256": dev_sha, "platform": st.get("platform"),
            "machine": cpu0.get("machine"), "cpu": cpu0.get("cpu"),
            "fmu": None if fmu is None else _file_sha256_id(fmu)})
        if fw is not None:
            out["entry_points"] = _elf_entry_points(
                fw, int(str(cpu0.get("init_nsvtor", "0")), 0))
        else:
            unver.append("entry_points")
    else:
        unver += ["target", "device_model_hash", "entry_points"]
    if nf.get("plugin") is not None:
        plugin = _check_native("plugin", nf.get("plugin"))
        vlibs = [_check_native(k, nf[k]) for k in sorted(nf) if k.startswith("virtual_library_")]
        plan = _manifest_plan(man)
        out["instrumentation_hash"] = _cid({
            "plugin": None if plugin is None else _file_sha256_id(plugin),
            "virtual_libraries": [_file_sha256_id(p) for p in vlibs],
            "virtuals": plan["virtuals"], "modifiers": plan["modifiers"],
            "finline": plan["finline"]})
    else:
        unver.append("instrumentation_hash")
    return out, sorted(unver)


def check_bundle(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    """Raise :class:`BundleMismatch` unless both bundles have the same id."""
    if expected.get("bundle_id") == actual.get("bundle_id"):
        return
    fields = sorted(k for k in set(expected) | set(actual)
                    if k != "bundle_id" and expected.get(k) != actual.get(k))
    raise BundleMismatch(f"bundle_id {expected.get('bundle_id')} != {actual.get('bundle_id')}; "
                         f"differing fields: {', '.join(fields)}", fields=fields)


# --------------------------------------------------------------------------
# RuntimeIdentity
# --------------------------------------------------------------------------

def meson_build_options(qemu: str | os.PathLike) -> dict[str, Any] | None:
    """``buildtype``, ``optimization``, ``debug`` from the QEMU build's meson introspection."""
    p = Path(qemu).resolve().parent / "meson-info" / "intro-buildoptions.json"
    if not p.is_file():
        return None
    opts = {o["name"]: o["value"] for o in json.loads(p.read_text())}
    return {k: opts.get(k) for k in ("buildtype", "optimization", "debug")}


def _git_head(path: str | os.PathLike) -> str | None:
    try:
        r = subprocess.run(["git", "-C", os.fspath(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _stat_doc(path: str | os.PathLike) -> dict[str, Any]:
    """What QEMU's ``runtime_signature`` hashes of a file (supercache.c:149-180):
    st_dev, st_ino, st_size and st_mtim of ``/proc/self/exe``; the path (the r-x
    ``/proc/self/maps`` lines), size and mtime of every mapped object."""
    st = os.stat(path)
    return {"path": str(Path(path).resolve()), "dev": st.st_dev, "ino": st.st_ino,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def run_info_fields(run_info: Any) -> dict[str, Any]:
    """The RuntimeIdentity inputs of a ``.scm`` RUN_INFO (``scm_v1.RunInfo``).

    ``prologue_sha256`` hashes the captured prologue bytes ``[0, prologue_end)``.
    The exe stat fields let :func:`runtime_identity` check that the capture came
    from the same QEMU executable. RUN_INFO has no ``base_cflags`` field, so
    that stays ``null``.
    """
    pro = bytes(run_info.prologue)
    return {
        "prologue_sha256": "sha256:" + hashlib.sha256(pro).hexdigest(),
        "prologue_len": len(pro),
        "icache_linesize": run_info.icache_linesize,
        "cpuinfo": run_info.cpuinfo,
        "code_address": run_info.code_address,
        "code_size": run_info.code_size,
        "prologue_end": run_info.prologue_end,
        "runtime_signature": run_info.runtime_signature,
        "exe_st_dev": run_info.exe_st_dev,
        "exe_st_ino": run_info.exe_st_ino,
        "exe_st_size": run_info.exe_st_size,
        "exe_st_mtim_ns": run_info.exe_st_mtim_ns,
    }


def runtime_identity(*, qemu: str | os.PathLike, plugin: str | os.PathLike | None,
                     config_text: str, sc_header: Any = None,
                     run_info: Mapping[str, Any] | None = None,
                     fastdyn_src: str | os.PathLike | None = None,
                     admission: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """RuntimeIdentity (§4.2). ``sc_header`` (an ``ScHeader``) supplies the runtime
    fields recorded in the ``.sc``; ``run_info`` (:func:`run_info_fields` of a
    ``.scm`` RUN_INFO, when a capture exists) supplies ``prologue_sha256``,
    ``icache_linesize`` and ``cpuinfo``, which a ``.sc`` alone does not record
    (they are ``null`` otherwise). ``base_cflags`` has no source in either file
    and is ``null``.

    With ``run_info``, the capture must describe the same runtime: its exe
    stat must equal ``qemu``'s and its ``code_address``/``code_size``/
    ``prologue_end``/``runtime_signature`` must equal ``sc_header``'s (when
    given); otherwise :class:`BundleStale` with code
    ``runtime_mismatch:run_info_<field>``.
    """
    from ..run.toml_rewrite import normalized_config_sha256

    qst = os.stat(qemu)
    h = sc_header
    ri = dict(run_info or {})
    if ri.get("exe_st_ino") is not None:
        for f, have in (("exe_st_dev", qst.st_dev), ("exe_st_ino", qst.st_ino),
                        ("exe_st_size", qst.st_size), ("exe_st_mtim_ns", qst.st_mtime_ns)):
            if ri[f] != have:
                raise BundleStale(f"RUN_INFO {f}={ri[f]} but {qemu} has {have}",
                                  code=f"runtime_mismatch:run_info_{f}")
    if h is not None:
        for f in ("code_address", "code_size", "prologue_end", "runtime_signature"):
            if ri.get(f) is not None and ri[f] != getattr(h, f):
                raise BundleStale(f"RUN_INFO {f}={ri[f]:#x} but the .sc header has "
                                  f"{getattr(h, f):#x}", code=f"runtime_mismatch:run_info_{f}")

    def hx(v: Any) -> str | None:
        return None if v is None else hexaddr(int(v))

    doc: dict[str, Any] = {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "qemu_sha256": _file_sha256_id(qemu),
        "qemu_exe_stat": _stat_doc(qemu),
        "plugin_sha256": None if plugin is None else _file_sha256_id(plugin),
        "plugin_stat": None if plugin is None else _stat_doc(plugin),
        "build_options": meson_build_options(qemu),
        "runtime_signature": hx(getattr(h, "runtime_signature", None)),
        "code_address": hx(getattr(h, "code_address", None)),
        "code_size": hx(getattr(h, "code_size", None)),
        "prologue_end": hx(getattr(h, "prologue_end", None)),
        "tb_size": getattr(h, "tb_size", None),
        "host_page_size": getattr(h, "host_page_size", None),
        "target_page_bits": getattr(h, "target_page_bits", None),
        "prologue_sha256": ri.get("prologue_sha256"),
        "icache_linesize": ri.get("icache_linesize"),
        "cpuinfo": hx(ri.get("cpuinfo")) if ri.get("cpuinfo") is not None else None,
        "base_cflags": hx(ri.get("base_cflags")) if ri.get("base_cflags") is not None else None,
        "supercache_flat": False,
        "fastdyn_commit": _git_head(fastdyn_src) if fastdyn_src else None,
        "capture_format": 1,
        "normalized_config_sha256": normalized_config_sha256(config_text),
    }
    for k in ("qemu_sha256", "plugin_sha256"):
        assert doc[k] is None or is_sha256_id(doc[k])
    doc["runtime_identity_id"] = runtime_identity_id(doc)
    if admission is not None:
        return extend_runtime_identity(doc, admission)
    return doc


def read_runtime_identity(path_or_doc: str | os.PathLike | Mapping[str, Any]) -> dict[str, Any]:
    doc = path_or_doc if isinstance(path_or_doc, Mapping) else \
        json.loads(Path(path_or_doc).read_text(encoding="utf-8"))
    if doc.get("schema") not in RUNTIME_IDENTITY_SCHEMAS:
        raise BundleSchemaError(f"unknown runtime identity schema {doc.get('schema')!r}",
                                code="unknown_schema_version")
    if doc.get("schema") == RUNTIME_IDENTITY_SCHEMA_V2:
        errs = admission_errors(doc.get("admission"))
        if errs:
            raise BundleSchemaError("; ".join(errs), code="admission_schema")
    want = runtime_identity_id(doc)
    if doc.get("runtime_identity_id") != want:
        raise BundleIdMismatch(f"runtime_identity_id {doc.get('runtime_identity_id')} != {want}",
                               code="runtime_identity_id_mismatch")
    return dict(doc)


# Fields that can be compared before a run (the .sc header's runtime fields need QEMU).
# The stat fields feed QEMU's runtime_signature: a rebuilt or touched binary or
# plugin makes QEMU truncate the cache, so deploy refuses up front instead of
# paying for a run that the post-run proof would mark INVALID.
_PRE_RUN_FIELDS = ("qemu_sha256", "qemu_exe_stat", "plugin_sha256", "plugin_stat",
                   "build_options", "supercache_flat", "normalized_config_sha256")


def _field_differs(want: Any, have: Any) -> bool:
    if isinstance(want, Mapping) and isinstance(have, Mapping):
        # stat documents: compare the keys the expected side recorded (older
        # documents have no dev/ino)
        return any(have.get(k) != v for k, v in want.items())
    return want != have


def check_runtime_identity(expected: Mapping[str, Any], actual: Mapping[str, Any],
                           fields: tuple[str, ...] = _PRE_RUN_FIELDS) -> list[str]:
    """Reason codes (``runtime_mismatch:<field>``) for differing ``fields``."""
    out = []
    for f in fields:
        if expected.get(f) is None:
            continue
        if _field_differs(expected.get(f), actual.get(f)):
            out.append("config_mismatch" if f == "normalized_config_sha256"
                       else f"runtime_mismatch:{f}")
    return out


# --------------------------------------------------------------------------
# RuntimeIdentity v2: the admission block (B3/B4 design §7.2, §9.1 V20, §14 AC1-AC4; WP17)
# --------------------------------------------------------------------------
#
# ``admission`` records the facts the guard and the observation contract
# depend on, bound to the QEMU executable:
#
#   schema                        "scopt.admission/1"
#   exe_sha256                    = qemu_sha256 of the identity
#   cpu_model                     the configured CPU model ("cortex-m3" is the only admitted one)
#   envlayout_id                  EnvLayout bound to exe_sha256, or null
#   tb_start_cb_possible          bool; true unless the configuration provably registers no
#                                 TB-translation hook and no core TB-exec callback (unknown => true)
#   tb_start_cb_reasons           why (stable strings)
#   mem_get_value_outside_mem_cb  false only if no loaded plugin library imports
#                                 qemu_plugin_mem_get_value; true if one does; null if unknown
#   mem_get_value_evidence        per library: "absent" | "imported" | "unreadable"
#   ac4_excluded_virtuals_present names of AC4-excluded virtuals seen in the configuration
#   ac4_inputs                    what was inspected ("manifest", "config", "virtuals")
#   exec_vaddr_aliases            informational: executable vaddr aliases of the machine, or null
#
# Facts are derived from the FastDyn manifest (and, when given, the run TOML
# and the generated virtuals file); they are evidence for V20, which also
# cross-checks tb_start_cb_possible against the Tier-0 capture.

ADMISSION_SCHEMA = "scopt.admission/1"
ADMITTED_CPU_MODELS = ("cortex-m3",)
# FastDyn sources at the pinned commit (design §0 G30, G24): built-in plugins that
# call virtual_register_tb_translation_hook (variable_watch.c:193,
# object_sanitizer.c:376); the core plugin registers a TB-exec callback only
# when twintrace is on (core/core.c:823-829).
TB_HOOK_PLUGINS = ("variable_watch", "object_sanitizer")
TB_HOOK_SYMBOLS = ("virtual_register_tb_translation_hook", "core_register_tb_trans_hook",
                   "qemu_plugin_register_vcpu_tb_trans_cb", "qemu_plugin_register_vcpu_tb_exec_cb",
                   "qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu")
MEM_GET_VALUE_SYMBOL = "qemu_plugin_mem_get_value"
# AC4 (design §14): virtuals that start threads writing CPU state, excluded by
# name until X-ASYNC clears them (G24: fuzz_aflnet.c:143, ardupilot.c:94,
# gcs_receiver.c:322-323).
AC4_EXCLUDED: dict[str, tuple[str, ...]] = {
    "fuzz_aflnet": ("aflnet",),
    "ardupilot": ("ardupilot", "gcs_receiver"),
}
# Executable vaddr aliases by QEMU machine (G20: hw/arm/mps2.c:209-212; the
# fixtures' cortexm machine maps flash once).
_MACHINE_ALIASES: dict[str, list[dict[str, str]]] = {
    "cortexm": [],
    "mps2": [{"region": "ssram1", "base": "0x0", "alias": "0x400000", "size": "0x400000"}],
}

_ADMISSION_KEYS = {
    "schema": str, "exe_sha256": (str, type(None)), "cpu_model": (str, type(None)),
    "envlayout_id": (str, type(None)), "tb_start_cb_possible": bool,
    "tb_start_cb_reasons": list, "mem_get_value_outside_mem_cb": (bool, type(None)),
    "mem_get_value_evidence": dict, "ac4_excluded_virtuals_present": list,
    "ac4_inputs": list, "exec_vaddr_aliases": (list, type(None)),
}


def admission_errors(adm: Any) -> list[str]:
    """Shape problems of an admission block ([] when valid)."""
    if not isinstance(adm, Mapping):
        return ["admission: missing or not an object"]
    out = []
    if adm.get("schema") != ADMISSION_SCHEMA:
        out.append(f"admission.schema {adm.get('schema')!r} is not {ADMISSION_SCHEMA}")
    for k, ty in _ADMISSION_KEYS.items():
        if k not in adm:
            out.append(f"admission.{k}: missing")
        elif not isinstance(adm[k], ty):
            out.append(f"admission.{k}: bad type {type(adm[k]).__name__}")
    for k in adm:
        if k not in _ADMISSION_KEYS and not str(k).startswith("x_"):
            out.append(f"admission.{k}: unknown key")
    return out


def _dynsym_imports(path: str | os.PathLike) -> set[str] | None:
    """Undefined dynamic symbols of an ELF shared object, or None if unreadable."""
    try:
        from elftools.elf.elffile import ELFFile

        with open(path, "rb") as f:
            elf = ELFFile(f)
            sec = elf.get_section_by_name(".dynsym")
            if sec is None:
                return set()
            return {s.name for s in sec.iter_symbols()
                    if s.name and s["st_shndx"] == "SHN_UNDEF"}
    except (OSError, ValueError, ImportError, Exception):  # noqa: BLE001 - unreadable => unknown
        return None


def _dynsym_all(path: str | os.PathLike) -> set[str] | None:
    try:
        from elftools.elf.elffile import ELFFile

        with open(path, "rb") as f:
            elf = ELFFile(f)
            sec = elf.get_section_by_name(".dynsym")
            return set() if sec is None else {s.name for s in sec.iter_symbols() if s.name}
    except Exception:  # noqa: BLE001 - unreadable => unknown
        return None


def _machine_aliases(machine: str | None) -> list[dict[str, str]] | None:
    if not machine:
        return None
    for k, v in _MACHINE_ALIASES.items():
        if machine == k or machine.startswith(k + "-") or machine.startswith(k):
            return list(v)
    return None


def _toml_plugins_enabled(config_text: str | None) -> list[str] | None:
    if config_text is None:
        return None
    try:
        cfg = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return None
    out = []
    cpus = cfg.get("CPU") or {}
    for v in cpus.values():
        for c in (v if isinstance(v, list) else [v]):
            if not isinstance(c, Mapping):
                continue
            for name, tab in (c.get("plugins") or {}).items():
                if isinstance(tab, Mapping) and tab.get("enabled", False) is True:
                    out.append(str(name))
    return sorted(set(out))


def admission_facts(*, manifest: Mapping[str, Any] | None, qemu_sha256: str | None,
                    envlayout: Any = None, config_text: str | None = None,
                    virtuals_text: str | None = None,
                    plugin_path: str | os.PathLike | None = None,
                    check_files: bool = True) -> dict[str, Any]:
    """The admission block for one configuration (see the module comment above).

    ``manifest`` is the FastDyn manifest (``<cache>.sc.fastdyn.json``) of the
    run; ``envlayout`` an :class:`~scopt.formats.envlayout_v1.EnvLayout` (or
    None); ``config_text`` the run TOML; ``virtuals_text`` the generated
    ``virtuals.txt``. Missing inputs make the affected facts unknown, which
    is fail-closed (``tb_start_cb_possible = true``,
    ``mem_get_value_outside_mem_cb = null``).
    """
    reasons: list[str] = []
    inputs = man_inputs = (manifest or {}).get("inputs", {}) if manifest else {}
    st = man_inputs.get("settings", {}) if isinstance(man_inputs, Mapping) else {}
    nf = man_inputs.get("native_files", {}) if isinstance(man_inputs, Mapping) else {}
    cf = man_inputs.get("content_files", {}) if isinstance(man_inputs, Mapping) else {}
    cpus = st.get("cpus") or []
    cpu0 = cpus[0] if len(cpus) == 1 and isinstance(cpus[0], Mapping) else {}
    popts = _plugin_options(list(st.get("plugin_options") or []))
    # -- tb_start_cb_possible ----------------------------------------------------
    possible = False
    if not manifest:
        possible = True
        reasons.append("unknown:no_manifest")
    else:
        tt = popts.get("twintrace")
        if tt is None:
            possible = True
            reasons.append("unknown:no_twintrace_option")
        elif tt.lower() not in ("off", "none", "0", ""):
            possible = True
            reasons.append(f"twintrace={tt}")
        arts = sorted(k for k in cf if k.startswith("preprocessing_artifact_"))
        if arts:
            possible = True
            reasons.append("preprocessing_artifacts:" + ",".join(arts))
        enabled = _toml_plugins_enabled(config_text)
        if config_text is not None and enabled is None:
            possible = True
            reasons.append("unknown:config_unparseable")
        for name in enabled or ():
            if name in TB_HOOK_PLUGINS:
                possible = True
                reasons.append(f"plugin:{name}")
        vlibs = [nf[k] for k in sorted(nf) if k.startswith("virtual_library_")]
        for i, e in enumerate(vlibs):
            path = e.get("path") if isinstance(e, Mapping) else None
            syms = _dynsym_all(path) if (path and check_files) else None
            if syms is None:
                possible = True
                reasons.append(f"virtual_library_{i}:unknown")
            elif syms & set(TB_HOOK_SYMBOLS):
                possible = True
                reasons.append(f"virtual_library_{i}:tb_hook")
        plugin = plugin_path or ((nf.get("plugin") or {}).get("path")
                                 if isinstance(nf.get("plugin"), Mapping) else None)
        if plugin is None:
            possible = True
            reasons.append("unknown:no_plugin")
        elif Path(plugin).name != "libfastdyn.so":
            possible = True
            reasons.append("unknown:plugin_not_fastdyn")
    if not possible:
        reasons.append("config:no_tb_hook_no_twintrace")
    # -- qemu_plugin_mem_get_value ------------------------------------------------
    evidence: dict[str, str] = {}
    libs: list[tuple[str, Any]] = []
    plugin = plugin_path or ((nf.get("plugin") or {}).get("path")
                             if isinstance(nf.get("plugin"), Mapping) else None)
    if plugin is not None:
        libs.append(("plugin", plugin))
    for k in sorted(nf):
        if k.startswith("virtual_library_"):
            e = nf[k]
            libs.append((k, e.get("path") if isinstance(e, Mapping) else None))
    mgv: bool | None
    if not manifest or plugin is None:
        mgv = None
    else:
        mgv = False
        for name, path in libs:
            imp = _dynsym_imports(path) if (path and check_files) else None
            if imp is None:
                evidence[name] = "unreadable"
                mgv = None if mgv is False else mgv
            elif MEM_GET_VALUE_SYMBOL in imp:
                evidence[name] = "imported"
                mgv = True
            else:
                evidence[name] = "absent"
    # -- AC4 ------------------------------------------------------------------------
    present: set[str] = set()
    ac4_inputs = []
    texts = []
    if manifest:
        ac4_inputs.append("manifest")
        texts += [str(v.get("path")) for k, v in nf.items()
                  if k.startswith("virtual_library_") and isinstance(v, Mapping)]
        texts += [str(o) for o in st.get("plugin_options") or []]
        fz = popts.get("fuzzing")
        if fz is not None and fz not in ("0", "", "off", "false", "False"):
            present.add("fuzz_aflnet")
    if config_text is not None:
        ac4_inputs.append("config")
        texts.append(config_text)
    if virtuals_text is not None:
        ac4_inputs.append("virtuals")
        texts.append(virtuals_text)
    blob = "\n".join(texts).lower()
    for name, toks in AC4_EXCLUDED.items():
        if any(t in blob for t in toks):
            present.add(name)
    return {
        "schema": ADMISSION_SCHEMA,
        "exe_sha256": qemu_sha256,
        "cpu_model": str(cpu0.get("cpu")) if cpu0.get("cpu") else None,
        "envlayout_id": None if envlayout is None else envlayout.envlayout_id,
        "tb_start_cb_possible": bool(possible),
        "tb_start_cb_reasons": reasons,
        "mem_get_value_outside_mem_cb": mgv,
        "mem_get_value_evidence": evidence,
        "ac4_excluded_virtuals_present": sorted(present),
        "ac4_inputs": ac4_inputs,
        "exec_vaddr_aliases": _machine_aliases(str(cpu0.get("machine")) if cpu0 else None),
    }


def extend_runtime_identity(ri: Mapping[str, Any], admission: Mapping[str, Any]) -> dict[str, Any]:
    """RuntimeIdentity v2: ``ri`` (v1 or v2) with ``admission``; the id is recomputed.

    ``admission.exe_sha256`` must equal ``ri.qemu_sha256`` (else
    ``BundleMismatch`` with code ``admission_exe_mismatch``).
    """
    errs = admission_errors(admission)
    if errs:
        raise BundleSchemaError("; ".join(errs), code="admission_schema")
    if admission.get("exe_sha256") != ri.get("qemu_sha256"):
        raise BundleMismatch(f"admission exe {admission.get('exe_sha256')} != qemu_sha256 "
                             f"{ri.get('qemu_sha256')}", code="admission_exe_mismatch")
    doc = {k: v for k, v in ri.items() if k != "runtime_identity_id"}
    doc["schema"] = RUNTIME_IDENTITY_SCHEMA_V2
    doc["admission"] = json.loads(json.dumps(dict(admission)))
    doc["runtime_identity_id"] = runtime_identity_id(doc)
    return doc


def admission_of(ri: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The admission block of a v2 RuntimeIdentity (None for v1 or None)."""
    if not isinstance(ri, Mapping) or ri.get("schema") != RUNTIME_IDENTITY_SCHEMA_V2:
        return None
    adm = ri.get("admission")
    return adm if isinstance(adm, Mapping) else None


def tb_start_cb_possible(ri: Mapping[str, Any] | None) -> bool:
    """The RuntimeIdentity fact; unknown (v1 identity) => True (fail closed, §7.2)."""
    adm = admission_of(ri)
    if adm is None:
        return True
    return bool(adm.get("tb_start_cb_possible", True))


def mem_get_value_outside_mem_cb(ri: Mapping[str, Any] | None) -> bool | None:
    """False only when the identity proves no plugin imports qemu_plugin_mem_get_value."""
    adm = admission_of(ri)
    if adm is None:
        return None
    v = adm.get("mem_get_value_outside_mem_cb")
    return v if isinstance(v, bool) else None


__all__ += ["RUNTIME_IDENTITY_SCHEMA_V2", "ADMISSION_SCHEMA", "ADMITTED_CPU_MODELS",
            "admission_facts", "admission_errors", "extend_runtime_identity", "admission_of",
            "tb_start_cb_possible", "mem_get_value_outside_mem_cb", "AC4_EXCLUDED",
            "TB_HOOK_PLUGINS"]
