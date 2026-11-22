"""``scopt import``: build an immutable :class:`CacheSnapshot` and store it (design §4.3). WP7.

:class:`ImportCache` is the import service:

1. take ``flock(LOCK_SH|LOCK_NB)`` on the ``.sc`` (QEMU holds ``LOCK_EX`` while
   it runs, supercache.c:581; failure: ``cache_locked``);
2. parse the header and records like QEMU (:mod:`scopt.formats.sc_v5`), bind
   the captures (:mod:`scopt.importer.join`), check the bundle against the
   FastDyn manifest (``bundle_mismatch``) and reject unsupported setups
   (``unsupported_flat``, ``unsupported_arena_below_4g``, ``run_info_mismatch:*``);
3. run the §2.7 cross-check on every bound slot
   (:mod:`scopt.importer.crosscheck`) and build the ``TBUnit`` model;
4. optionally write the store directory (:func:`write_store`): files are built
   in ``store/snapshots/.tmp-*`` and renamed into place. If the directory
   already exists, every file must be byte-identical (re-import is
   byte-deterministic) or the import fails with ``store_conflict``.

With ``strict``, any ``missing_reference`` (a captured site not found in the
bytes: a contradiction, §2.7 step 5) rejects the whole import.

Bundle check (:func:`check_bundle`). A bundle needs the cache's FastDyn
manifest (otherwise ``bundle_unverified``). Every field the manifest
determines is recomputed with :func:`scopt.formats.bundle_v1.manifest_bundle_fields`
(the same constructions as the identity bundle, WP8): ``patch_plan_hash``,
``contract_id`` and, from the native files after a size/mtime check
(``bundle_stale``), ``image_hash``, ``target``, ``entry_points``,
``instrumentation_hash``, ``device_model_hash``. A difference is
``bundle_mismatch`` (e.g. ``modifiers.txt`` edited between bundle and prep
run). ``memory_map_hash`` needs the TOML and is reported as unverified. A
manifest with ``supercache_flat = true`` is ``unsupported_flat``.

RuntimeIdentity check (:func:`check_runtime_identity`): the ``.sc`` header
fields, the RUN_INFO of every used capture (``prologue_sha256``,
``icache_linesize``, ``cpuinfo``, QEMU executable size/mtime) and, through the
manifest's native files, ``qemu_sha256``/``plugin_sha256``
(``runtime_mismatch:<field>``, ``runtime_stale:<file>``). ``snapshot.json``
records what was verified (``identity_checks``). :func:`open_snapshot`
re-checks the stored bundle/RuntimeIdentity without re-reading native files.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import shutil
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..canon import canonical_json, canonical_jsonl, content_id, hexaddr, sha256_hex, sha256_id
from ..decode.x86 import DecodeMemo
from ..formats import bundle_v1 as _bv1
from ..formats import schemas
from ..formats.region_v1 import build_region
from ..formats.sc_v5 import ScFile, read_sc, sha256_sparse, sparse_copy
from ..ids import bundle_id as _bundle_id
from ..ids import tb_uid, unit_ids
from ..model.cache_snapshot import CacheSnapshot, snapshot_dir_name
from ..model.unit import Bindings, HelperBinding, RelocInfo, TBUnit
from .classify import HostContext, context_for
from .crosscheck import Firmware, UnitCheck, crosscheck_unit
from .join import ImportRejected, JoinResult, join

__all__ = [
    "ImportCache", "ImportResult", "ImportRejected", "import_snapshot", "write_store",
    "open_snapshot", "find_inputs", "g17_tallies", "G17", "manifest_plan_hash",
    "manifest_contract_id", "SNAPSHOT_SCHEMA",
]

SNAPSHOT_SCHEMA = "scopt.snapshot"

#: design §0 G17 [B0] reference tallies (minimal fixture)
G17: dict[str, Any] = {
    "helper_pool_calls": 394, "plugin_rel32_calls": 3, "ext_rip_lea": 3, "tb_ret_jmps": 316,
    "epilogue_jmps": 3, "own_struct_lea": 316, "own_struct_by_idx": {"0": 105, "1": 50, "3": 161},
    "raddr_lea": 336, "goto_ptr": 52, "movabs": 0, "host_ptr_imm32": 0, "helper_names": 11,
}

# ---------------------------------------------------------------------------
# Manifest / bundle / RuntimeIdentity checks (field constructions: scopt.formats.bundle_v1)
# ---------------------------------------------------------------------------

def _bundle_err(e: Exception) -> ImportRejected:
    code = getattr(e, "code", "bundle_error")
    return ImportRejected("manifest_format" if code == "manifest_format" else code, str(e))


def manifest_plan_hash(manifest: Mapping[str, Any]) -> str:
    try:
        return _bv1.manifest_patch_plan_hash(manifest)
    except _bv1.BundleError as e:
        raise _bundle_err(e) from e


def manifest_contract_id(manifest: Mapping[str, Any]) -> str:
    return _bv1.manifest_contract_id(manifest)


def check_bundle(bundle: Mapping[str, Any] | None, manifest: Mapping[str, Any] | None, *,
                 files: bool = True) -> dict[str, Any] | None:
    """Check ``bundle`` against the cache's FastDyn manifest (§4.3 step 2).

    Every field the manifest determines (:func:`scopt.formats.bundle_v1.
    manifest_bundle_fields`) must be equal, otherwise ``bundle_mismatch``.
    With ``files`` (import time) the firmware/plugin the manifest names are
    hashed, after the size/mtime check (``bundle_stale`` when they changed
    since the prep run). A bundle without a manifest cannot be checked and
    is rejected (``bundle_unverified``). Returns ``{"verified": [...],
    "unverified": [...]}`` for ``snapshot.json`` (None without a bundle).
    """
    if manifest is not None:
        st = manifest.get("inputs", {}).get("settings", {})
        if st.get("supercache_flat") is True:
            raise ImportRejected("unsupported_flat", "manifest settings.supercache_flat = true")
    if bundle is None:
        return None
    errs = schemas.errors(dict(bundle), "execution_bundle.v1")
    if errs:
        raise ImportRejected("bundle_schema", "; ".join(errs[:3]))
    if bundle.get("bundle_id") != _bundle_id(bundle):
        raise ImportRejected("bundle_id_mismatch", "bundle_id does not match its content")
    if manifest is None:
        raise ImportRejected("bundle_unverified",
                             "a bundle was given but the cache has no FastDyn manifest to "
                             "check it against")
    try:
        want, unver = _bv1.manifest_bundle_fields(manifest, files=files)
    except _bv1.BundleError as e:
        raise _bundle_err(e) from e
    diff = sorted(k for k, v in want.items() if bundle.get(k) != v)
    if diff:
        raise ImportRejected("bundle_mismatch",
                             f"bundle {bundle.get('bundle_id')} disagrees with the cache's "
                             f"manifest in {', '.join(diff)}")
    return {"verified": sorted(want), "unverified": unver}


def _sha_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return "sha256:" + h.hexdigest()


def check_runtime_identity(ri: Mapping[str, Any] | None, sc: ScFile, *,
                           run_info: Any = None, manifest: Mapping[str, Any] | None = None,
                           files: bool = True) -> dict[str, Any] | None:
    """Check a RuntimeIdentity (§4.2) against the ``.sc`` header, the bound
    captures' RUN_INFO and the FastDyn manifest.

    A field that is ``null`` in the RuntimeIdentity is not compared. Failures
    reject the import with ``runtime_mismatch:<field>`` (``runtime_stale:<file>``
    when a native file changed since the prep run, so its hash cannot be
    compared). Returns ``{"verified": [...], "unverified": [...]}``.
    """
    if ri is None:
        return None
    if ri.get("runtime_identity_id") != content_id(ri, "runtime_identity_id"):
        raise ImportRejected("runtime_identity_id_mismatch", "")
    if ri.get("supercache_flat") is True:
        raise ImportRejected("unsupported_flat", "RuntimeIdentity supercache_flat")
    verified: list[str] = []
    unver: list[str] = []

    def cmp(field: str, want: Any, have: Any, src: str) -> None:
        if want is None:
            return
        if have is None:
            unver.append(f"{field}@{src}")
            return
        if want != have:
            raise ImportRejected(f"runtime_mismatch:{field}",
                                 f"RuntimeIdentity {want!r} != {src} {have!r}")
        verified.append(f"{field}@{src}")

    h = sc.header
    for k in ("runtime_signature", "code_address", "code_size", "prologue_end"):
        cmp(k, ri.get(k), hexaddr(getattr(h, k)), "sc")
    for k in ("tb_size", "host_page_size", "target_page_bits"):
        cmp(k, ri.get(k), getattr(h, k), "sc")
    est = ri.get("qemu_exe_stat") if isinstance(ri.get("qemu_exe_stat"), Mapping) else {}
    if run_info is not None:
        cmp("prologue_sha256", ri.get("prologue_sha256"), sha256_id(run_info.prologue),
            "run_info")
        cmp("icache_linesize", ri.get("icache_linesize"), run_info.icache_linesize, "run_info")
        cmp("cpuinfo", ri.get("cpuinfo"), hexaddr(run_info.cpuinfo), "run_info")
        cmp("qemu_exe_stat.size", est.get("size"), run_info.exe_st_size, "run_info")
        cmp("qemu_exe_stat.mtime_ns", est.get("mtime_ns"), run_info.exe_st_mtim_ns, "run_info")
    nf = (manifest or {}).get("inputs", {}).get("native_files", {}) if manifest else {}
    q = nf.get("qemu") if isinstance(nf.get("qemu"), Mapping) else None
    if q is not None:
        cmp("qemu_exe_stat.size", est.get("size"), q.get("size"), "manifest")
        cmp("qemu_exe_stat.mtime_ns", est.get("mtime_ns"), q.get("mtime_ns"), "manifest")
    for name, key in (("qemu", "qemu_sha256"), ("plugin", "plugin_sha256")):
        ent = nf.get(name) if isinstance(nf.get(name), Mapping) else None
        if ri.get(key) is None:
            continue
        if not files or ent is None or "path" not in ent:
            unver.append(f"{key}@file")
            continue
        try:
            st = os.stat(ent["path"])
        except OSError:
            raise ImportRejected(f"runtime_stale:{name}", f"{ent['path']} is gone") from None
        if st.st_size != ent.get("size") or st.st_mtime_ns != ent.get("mtime_ns"):
            raise ImportRejected(f"runtime_stale:{name}",
                                 f"{ent['path']} changed since the prep run")
        cmp(key, ri.get(key), _sha_file(ent["path"]), "file")
    return {"verified": sorted(set(verified)), "unverified": sorted(set(unver))}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def find_inputs(prep: str | os.PathLike) -> tuple[str, list[str], str | None]:
    """``(sc, [scm...], manifest|None)`` from a prep directory or a ``.sc`` path."""
    p = Path(prep)
    if p.is_file():
        sc = p
        scms = sorted(str(x) for x in p.parent.glob("*.scm"))
    else:
        scs = sorted(x for x in p.rglob("*.sc") if x.is_file())
        if len(scs) != 1:
            raise ImportRejected("inputs", f"{p}: expected exactly one .sc, found {len(scs)}")
        sc = scs[0]
        scms = sorted(str(x) for x in p.rglob("*.scm") if x.is_file())
    man = Path(str(sc) + ".fastdyn.json")
    return str(sc), scms, str(man) if man.is_file() else None


def _load_json(path: str | os.PathLike | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


class _ShLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = -1

    def __enter__(self) -> "_ShLock":
        self.fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as e:
            os.close(self.fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise ImportRejected("cache_locked", f"{self.path} is locked (QEMU running?)")
            raise
        return self

    def __exit__(self, *a: Any) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@dataclass
class ImportResult:
    snapshot: CacheSnapshot
    join: JoinResult
    checks: dict[int, UnitCheck]  # by slot (bound slots only)
    summary: dict[str, Any]
    regions: list[dict[str, Any]]
    sc_path: str
    scm_paths: list[str]
    manifest: dict[str, Any] | None
    manifest_bytes: bytes | None
    bundle: dict[str, Any] | None
    runtime_identity: dict[str, Any] | None
    store_dir: str | None = None
    store_status: str | None = None  # created | exists
    identity_checks: dict[str, Any] | None = None
    # .sc v6 (B3, WP18): the extension table and the re-imported variants
    ext: Any = None  # scopt.formats.sc_v6.ExtTable (v6 files only)
    variants: list[dict[str, Any]] = field(default_factory=list)

    @property
    def used_scm_paths(self) -> list[str]:
        """The capture files that were used (not excluded as foreign), input order."""
        used = {f.path for f in self.join.captures}
        return [p for p in self.scm_paths if p in used]

    def evidence_rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.join.capture_problems:
            out.append({"subject": "capture", "id": p.file, "check": p.code, "result": "fail",
                        "detail": p.detail})
        for s in self.join.sc.skipped:
            out.append({"subject": "record", "id": f"slot:{s.slot}", "check": s.reason,
                        "result": "fail", "detail": s.detail})
        for u in self.snapshot.units():
            b = self.join.slots[u.slot]
            out.append({"subject": "unit", "id": u.uid, "slot": u.slot, "check": "bind",
                        "result": "pass" if b.bound else "fail",
                        "detail": b.reason or "bound" if not b.detail else
                        f"{b.reason}: {b.detail}"})
            uc = self.checks.get(u.slot)
            if uc is None:
                continue
            for e in uc.evidence:
                out.append({"subject": "unit", "id": u.uid, "slot": u.slot, **e})
        return out


def _content_hash(record: bytes, body: bytes, meta: Mapping[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(record)
    h.update(body)
    h.update(canonical_json(meta))
    return "sha256:" + h.hexdigest()


def _meta_doc(b, uc: UnitCheck | None, reloc: RelocInfo, origin: str, vaddrs: tuple[int, ...],
              meta_status: str) -> dict[str, Any]:
    return {
        "chunk_sha256": None if b.chunk is None else sha256_hex(b.chunk.encode()),
        "meta_status": meta_status, "origin": origin, "vaddrs": [hexaddr(v) for v in vaddrs],
        "relocatable": reloc.relocatable, "reasons": list(reloc.reasons),
        "fixups": [[f.field_off, f.width, f.rule, f.site_kind, f.target_class,
                    None if f.target_abs is None else hexaddr(f.target_abs)]
                   for f in reloc.fixups],
        "recovery": [] if uc is None else [[p.id, p.host_return_offset, p.insn,
                                            [hexaddr(w) for w in p.words], p.source]
                                           for p in uc.recovery],
        "helpers": [] if uc is None else [[h.id, h.name, h.module, h.symbol, h.flags]
                                          for h in uc.helpers],
        "unchecked": [] if uc is None else [[p.cls, p.anchor, p.site] for p in uc.unchecked],
    }


def _native_binding_problem(binding: Any, context: HostContext, runtime: Mapping[str, Any] | None,
                            uid: str, resolvers: dict, images: dict) -> str | None:
    """Check v3/v4 provenance against the captured runtime's exact ELF bytes."""
    from ..model.native import UDIV_KINDS, UDIV_TYPEMASK, supports_tcg_integer_abi
    from .native_callee import CalleeResolutionError, NativeCalleeResolver

    if runtime is None:
        return "missing runtime identity"
    if id(binding.source) not in resolvers:
        try:
            digest = sha256_id(Path(binding.source.path).read_bytes())
        except (OSError, TypeError):
            return "source capture unavailable"
        resolvers[id(binding.source)] = NativeCalleeResolver(
            context, binding.source.run_info, runtime, digest, images)
    resolver = resolvers[id(binding.source)]
    for req in binding.chunk.native_fragments:
        helper = context.helper(req.helper_id)
        if req.target != uid or helper is None or helper.func != req.target_abs:
            return "native fragment does not identify this unit and captured helper"
        framed = req.splice is not None and req.splice.kind == "frame"
        is_udiv = req.splice is not None and req.splice.kind in UDIV_KINDS
        if is_udiv:
            if (helper.name_str != "udiv" or helper.tcg_flags != 1
                    or helper.typemask != UDIV_TYPEMASK or req.symbol != "helper_udiv"):
                return "native fragment is not the captured ARM UDIV helper"
        elif framed:
            if helper.name_str is not None or helper.tcg_flags != 0 or helper.typemask != 400:
                return "framed native fragment is not a captured FastDyn callback"
        elif helper.name_str is None or (helper.tcg_flags & 0xF) != 5:
            return "native fragment is not a captured pure QEMU helper"
        target = context.code_target(helper.func)
        if target is None:
            return "native fragment target has no executable mapping"
        try:
            function = resolver.resolve(HelperBinding(
                req.helper_id, helper.name_str, target.module, req.symbol, helper.tcg_flags,
                helper.func))
        except CalleeResolutionError as e:
            return f"{e.reason}: {e}"
        expected_role = "plugin" if framed else "qemu"
        if framed and req.splice.frame_chain != function.frame_chain:
            return "nested frame bodies differ from the bound executable"
        if (function.role != expected_role
                or (is_udiv and (function.symbol != req.symbol or function.abi != "sysv-amd64"))
                or (not framed and not is_udiv and not supports_tcg_integer_abi(function.tcg_typemask))
                or (req.splice is not None and req.tcg_typemask != function.tcg_typemask)
                or function.code != req.callee_code or function.image_id != req.image_id
                or function.elf_address != req.elf_address or function.image_offset != req.image_offset):
            return "native fragment source differs from the bound executable"
    return None


class ImportCache:
    """The import service (see the module docstring)."""

    def __init__(self, sc: str | os.PathLike, scms: Sequence[str | os.PathLike] = (), *,
                 manifest: str | os.PathLike | None = None,
                 bundle: str | os.PathLike | Mapping[str, Any] | None = None,
                 runtime_identity: str | os.PathLike | Mapping[str, Any] | None = None,
                 firmware: str | os.PathLike | None = None,
                 plugin: str | os.PathLike | None = None, strict: bool = False,
                 use_manifest_firmware: bool = True, verify_files: bool = True) -> None:
        self.sc_path = os.fspath(sc)
        self.scm_paths = [os.fspath(p) for p in scms]
        self.manifest_path = None if manifest is None else os.fspath(manifest)
        self.bundle = dict(bundle) if isinstance(bundle, Mapping) else _load_json(bundle)
        self.runtime_identity = (dict(runtime_identity) if isinstance(runtime_identity, Mapping)
                                 else _load_json(runtime_identity))
        self.firmware = None if firmware is None else os.fspath(firmware)
        self.plugin = None if plugin is None else os.fspath(plugin)
        self.strict = strict
        self.use_manifest_firmware = use_manifest_firmware
        self.verify_files = verify_files

    def run(self) -> ImportResult:
        man_bytes = Path(self.manifest_path).read_bytes() if self.manifest_path else None
        manifest = json.loads(man_bytes) if man_bytes is not None else None
        bchk = check_bundle(self.bundle, manifest, files=self.verify_files)
        with _ShLock(self.sc_path):
            sc = read_sc(self.sc_path, versions=(5, 6))
            f6 = None
            if sc.loadable and sc.header.version == 6:
                from ..formats.sc_v6 import read_sc6

                f6 = read_sc6(self.sc_path)
                if f6.problem is not None:
                    # A version-6 header without the v6 extension at all (feature
                    # EXT_TABLE clear) is a bad version, as B1-9 has it (stable
                    # reason "version"); a present but refused extension is
                    # "sc6_extension".
                    code = "version" if f6.problem[0] == "ext_table_clear" else "sc6_extension"
                    raise ImportRejected(code, f"{f6.problem[0]}: {f6.problem[1]}")
                sc = f6.sc
            j = join(sc, self.scm_paths)
        rchk = None
        for f in (j.captures or (None,)):
            rchk = check_runtime_identity(self.runtime_identity, sc,
                                          run_info=None if f is None else f.run_info,
                                          manifest=manifest, files=self.verify_files)

        nf = (manifest or {}).get("inputs", {}).get("native_files", {})
        fw_path = self.firmware
        if fw_path is None and self.use_manifest_firmware and isinstance(nf.get("firmware"),
                                                                         Mapping):
            fw_path = nf["firmware"].get("path")
        firmware = Firmware.load(fw_path) if fw_path and os.path.isfile(fw_path) else None
        plugin = self.plugin
        if plugin is None and isinstance(nf.get("plugin"), Mapping):
            plugin = nf["plugin"].get("path")

        ctxs: dict[int, HostContext] = {}
        decode_memo = DecodeMemo() if os.environ.get("SCOPT_IMPORT_DECODE_MEMO") == "1" else None
        native_resolvers, native_images = {}, {}
        units_in = sc.units()
        uids = unit_ids(u.record.tb_uid for u in units_in)
        checks: dict[int, UnitCheck] = {}
        units: list[TBUnit] = []
        if f6 is not None:
            from ..formats.sc_v6 import sc6_snapshot_id

            sid = sc6_snapshot_id(f6)
        else:
            sid = sc.snapshot_id()
        for su, uid in zip(units_in, uids):
            b = j.slots[su.slot]
            r = su.record
            uc = None
            if b.bound:
                ri = b.source.run_info
                ctx = context_for(b.source, ctxs, plugin_path=plugin)
                uc = crosscheck_unit(su, b.chunk, sc.header, ctx,
                                     icache_linesize=ri.icache_linesize, firmware=firmware, decode_memo=decode_memo)
                if b.chunk.native_fragments and uc.meta_status == "ok":
                    problem = _native_binding_problem(b, ctx, self.runtime_identity, uid,
                                                      native_resolvers, native_images)
                    uc.check("native_images", problem is None,
                             problem or "native fragment sources match verified runtime images",
                             "native_image_mismatch")
                    if problem is not None:
                        uc.meta_status = "incomplete"
                checks[su.slot] = uc
            if uc is None:
                reasons = ("no_meta",) + ((b.reason,) if b.reason and b.reason != "no_chunk"
                                         else ())
                reloc = RelocInfo(False, tuple(sorted(reasons)), ())
                meta_status = "no_meta"
                origin = "unknown"
                vaddrs: tuple[int, ...] = ()
                arrays: dict[str, tuple] = {}
                chunk = None
            else:
                rs = uc.sorted_reasons()
                reloc = RelocInfo(not rs, rs, uc.fixups)
                meta_status = uc.meta_status
                origin = uc.origin
                vaddrs = (b.chunk.vaddr,)
                arrays = uc.arrays
                chunk = b.chunk
            meta = _meta_doc(b, uc, reloc, origin, vaddrs, meta_status)
            stub_start = None if chunk is None else chunk.stub_start
            if stub_start is not None:
                meta["stub_start"] = stub_start  # v2 chunks only: v1 hashes unchanged
            inline_start = None if chunk is None else chunk.inline_start
            if inline_start is not None:
                meta["inline_start"] = inline_start
            splice_applied = chunk is not None and chunk.splice_start is not None
            if splice_applied:
                meta["splice_start"] = chunk.splice_start
            units.append(TBUnit(
                uid=uid, key=r.key, slot=su.slot, guest_size=r.guest_size, icount=r.icount,
                guest_hash=r.guest_hash, vaddrs=vaddrs, origin=origin,  # type: ignore[arg-type]
                tb_offset=r.tb_offset, tc_offset=r.tc_offset, tc_size=r.tc_size,
                end_offset=r.end_offset, code=su.code, search=su.search,
                record=su.record_bytes,
                insn0_start=chunk.insn0_start if chunk else 0,
                body_end=chunk.body_end if chunk else 0,
                ldst_end=chunk.ldst_end if chunk else 0,
                pool_start=chunk.pool_start_or_none if chunk else None,
                insns=arrays.get("insns", ()), sites=arrays.get("sites", ()),
                pool=arrays.get("pool", ()), ldst=arrays.get("ldst", ()),
                opmarks=arrays.get("opmarks", ()), static_exits=arrays.get("static_exits", ()),
                reloc=reloc, recovery=uc.recovery if uc else (),
                bindings=Bindings(uc.helpers, uc.unchecked) if uc else Bindings(),
                meta_status=meta_status,  # type: ignore[arg-type]
                content_hash=_content_hash(su.record_bytes, su.body, meta),
                stub_start=stub_start, inline_start=inline_start, splice_applied=splice_applied,
                native_fragments=chunk.native_fragments if chunk and meta_status == "ok" else ()))
            assert uid.startswith(tb_uid(r.key, r.guest_size, r.icount, r.guest_hash))

        if self.strict:
            miss = [(u.slot, x) for u in units for x in u.reloc.reasons
                    if x.startswith("missing_reference")]
            if miss:
                raise ImportRejected("missing_reference",
                                     f"--strict: {len(miss)} captured sites not found, first "
                                     f"slot {miss[0][0]} {miss[0][1]}")

        ri_id = None if self.runtime_identity is None else \
            self.runtime_identity.get("runtime_identity_id")
        regions = []
        for u in units:
            uc = checks.get(u.slot)
            sb = {c.site.field_off: c.binding for c in uc.classified} if uc else {}
            regions.append(build_region(
                u, snapshot_id=sid, tb_size=sc.header.tb_size,
                page_bits=sc.header.target_page_bits, bundle=self.bundle,
                runtime_identity_id=ri_id, search_len=uc.search_len if uc else None,
                site_bindings=sb,
                helper_returns={c.ref: c.host_return_offset for c in uc.recovery_checks
                                if c.source == "helper_call"} if uc else None))
        first_ri = next((f.run_info for f in j.captures if f.run_info is not None), None)
        snap = CacheSnapshot(
            snapshot_id=sid, header=sc.header, header_bytes=sc.header_bytes, _units=tuple(units),
            runtime=self.runtime_identity, bundle=self.bundle,
            capture_level=None if not j.captures else min(f.header.capture_level
                                                          for f in j.captures),
            run_info=first_ri,
            regions={r_["members"][0]["unit"]: r_ for r_ in regions},
            details={u.uid: checks[u.slot] for u in units if u.slot in checks})
        res = ImportResult(snap, j, checks, {}, regions, self.sc_path, self.scm_paths, manifest,
                           man_bytes, self.bundle, self.runtime_identity)
        res.identity_checks = {"bundle": bchk, "runtime_identity": rchk}
        if f6 is not None:
            res.ext = f6.ext
            res.variants = import_variants(f6, units, j.captures)
        res.summary = _summary(res, fw_path if firmware else None, plugin)
        return res


def import_variants(f6: Any, units: Sequence[TBUnit], captures: Sequence[Any]
                    ) -> list[dict[str, Any]]:
    """Re-import the variants of a v6 file (B3/B4 design §9.1 V19; WP18).

    Each ext variant must be described by exactly one VARIANT chunk of the
    used captures; its bytes in the file must equal the clone of its general
    record ``C`` (relocated by NativeLinker v1 rules to the variant's
    ``tb_offset``) with the chunk's same-length patches applied (old bytes
    checked); its predecessor slot entry must name the chunk's ``pred_slot``
    record and ``slot_n`` and be linked to it. One row per ext variant
    (``ok`` false with ``problems`` otherwise).
    """
    from ..link import LinkError, relocate_unit

    ext = f6.ext
    if ext is None:
        return []
    by_slot = {u.slot: u for u in units}
    chunks: dict[int, list[Any]] = {}
    for f in captures:
        for vc in getattr(f, "variants", ()):
            chunks.setdefault(vc.variant_index, []).append(vc)
    link_of = {lk.variant_index: lk for lk in ext.links}
    rows = []
    for i, v in enumerate(ext.variants):
        probs: list[str] = []
        cs = chunks.get(i, [])
        if len(cs) != 1:
            rows.append({"variant_index": i, "ok": False,
                         "problems": [f"{len(cs)} VARIANT chunks describe it"]})
            continue
        vc = cs[0]
        for k in ("tb_offset", "tc_offset", "tc_size", "end_offset", "code_fnv64"):
            if getattr(vc, k) != getattr(v, k):
                probs.append(f"{k}: chunk {getattr(vc, k):#x} ext {getattr(v, k):#x}")
        c = by_slot.get(vc.general_slot)
        if c is None or c.tb_offset != v.general_tb_offset:
            probs.append(f"general_slot {vc.general_slot} is not the record at "
                         f"{v.general_tb_offset:#x}")
        body = f6.variant_bodies[i] if i < len(f6.variant_bodies) else b""
        if c is not None and not probs:
            try:
                want = bytearray(relocate_unit(c, v.tb_offset).body)
            except LinkError as e:
                want = None
                probs.append(f"relocate: {e}")
            if want is not None:
                for p_ in vc.patches:
                    o = p_.host_off
                    if bytes(want[o:o + len(p_.old)]) != p_.old:
                        probs.append(f"patch @{o}: old bytes differ")
                    want[o:o + len(p_.new)] = p_.new
                if bytes(want) != body:
                    probs.append("bytes != relocate(C) + patches")
        lk = link_of.get(i)
        r = by_slot.get(vc.pred_slot)
        if lk is None:
            probs.append("no link")
        else:
            se = ext.slots[lk.slot_index]
            if r is None or se.src_tb_offset != r.tb_offset or se.slot_n != vc.slot_n:
                probs.append("pred slot differs from the linked slot entry")
        rows.append({"variant_index": i, "ok": not probs, "problems": probs,
                     "variant_key": "sha256:" + vc.variant_key.hex(),
                     "general_unit": None if c is None else c.uid,
                     "pred_unit": None if r is None else r.uid, "slot_n": vc.slot_n,
                     "patches": len(vc.patches), "tb_offset": hex(v.tb_offset)})
    return rows


def import_snapshot(prep: str | os.PathLike | None = None, *, sc: str | None = None,
                    scms: Sequence[str] | None = None, manifest: str | None = None,
                    store: str | os.PathLike | None = None,
                    progress: Callable[[str], None] | None = None,
                    **kw: Any) -> ImportResult:
    """Import from a prep dir (or explicit paths); with ``store``, also write it.

    ``progress`` (optional) receives short status lines while the import runs."""
    if prep is not None:
        sc0, scms0, man0 = find_inputs(prep)
        sc = sc or sc0
        scms = scms if scms is not None else scms0
        manifest = manifest or man0
    if sc is None:
        raise ImportRejected("inputs", "no .sc given")
    if manifest is None and os.path.isfile(str(sc) + ".fastdyn.json"):
        manifest = str(sc) + ".fastdyn.json"
    if progress is not None:
        progress(f"import: reading {sc} and {len(scms or [])} capture file(s)")
    res = ImportCache(sc, scms or [], manifest=manifest, **kw).run()
    if store is not None:
        if progress is not None:
            progress(f"import: {res.summary['records']['valid']} records checked; "
                     f"writing the snapshot to {Path(store) / 'snapshots'}")
        write_store(res, store, progress=progress)
        from .warm_origin import record_import
        record_import(res.store_dir, sc, prep)
    return res


# ---------------------------------------------------------------------------
# Summary / tallies
# ---------------------------------------------------------------------------

def g17_tallies(checks: Iterable[UnitCheck]) -> dict[str, Any]:
    """The G17 reference counts from the classified captured sites (T-B1-3)."""
    from ..decode.x86 import RefKind
    from ..formats.enums import Encoding, SiteKind

    own: Counter = Counter()
    t = Counter()
    helper_targets: set[int] = set()
    for uc in checks:
        for c in uc.classified:
            s = c.site
            if s.kind in (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL) and \
                    s.enc == Encoding.POOL_INDIRECT:
                t["helper_pool_calls"] += 1
                helper_targets.add(s.value)
            if s.kind == SiteKind.HELPER_CALL and s.enc == Encoding.REL32 and \
                    c.cls == "ext.plugin":
                t["plugin_rel32_calls"] += 1
            if s.kind == SiteKind.MOVI_CONST and c.cls == "ext.ptr_riprel":
                t["ext_rip_lea"] += 1
            if c.cls == "prologue.tb_ret":
                t["tb_ret_jmps"] += 1
            if c.cls == "prologue.epilogue":
                t["epilogue_jmps"] += 1
            if c.cls.startswith("own_struct+"):
                own[c.cls.split("+")[1]] += 1
            if s.kind == SiteKind.MOVI_LDST_RA and c.cls == "intra":
                t["raddr_lea"] += 1
            if c.cls == "goto_ptr":
                t["goto_ptr"] += 1
        t["movabs"] += sum(1 for d in uc.decoded if d.kind == RefKind.MOVABS)
        t["host_ptr_imm32"] += sum(1 for d in uc.decoded
                                   if d.kind in (RefKind.IMM32, RefKind.DISP32))
    keys = ("helper_pool_calls", "plugin_rel32_calls", "ext_rip_lea", "tb_ret_jmps",
            "epilogue_jmps", "own_struct_lea", "own_struct_by_idx", "raddr_lea", "goto_ptr",
            "movabs", "host_ptr_imm32", "helper_names")
    out = {k: t.get(k, 0) for k in keys}
    out["own_struct_lea"] = sum(own.values())
    out["own_struct_by_idx"] = dict(sorted(own.items()))
    out["helper_names"] = len(helper_targets)
    return out


def _summary(res: ImportResult, firmware: str | None, plugin: str | None) -> dict[str, Any]:
    snap = res.snapshot
    j = res.join
    sc = j.sc
    reasons: Counter = Counter()
    for u in snap.units():
        for r in u.reloc.reasons:
            reasons[r.split("@", 1)[0]] += 1
    checks = [res.checks[s] for s in sorted(res.checks)]
    hs = [c for uc in checks for c in uc.helper_symbol_checks]
    origins = Counter(u.origin for u in snap.units())
    classes: Counter = Counter()
    for uc in checks:
        for c in uc.classified:
            classes[c.cls] += 1
    ri = snap.run_info
    parsed = {f.path: f for f in j.captures}
    cap_files = []
    for p in res.used_scm_paths:
        data = Path(p).read_bytes()
        f = parsed.get(p)
        s = None if f is None else f.summary()
        if s is not None:
            s.pop("path", None)
        cap_files.append({"name": _capture_name(data), "sha256": sha256_id(data),
                          "summary": s})
    cap_files.sort(key=lambda c: c["name"])
    complete = bool(j.captures) and all(not f.incomplete and not f.rejected_chunks
                                        for f in j.captures)
    doc: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA, "version": 1, "snapshot_id": snap.snapshot_id,
        "header": sc.header.to_json(),
        "bundle_id": None if res.bundle is None else res.bundle.get("bundle_id"),
        "contract_id": None if res.bundle is None else res.bundle.get("contract_id"),
        "runtime_identity_id": None if res.runtime_identity is None else
        res.runtime_identity.get("runtime_identity_id"),
        "records": {"record_count": sc.header.record_count, "count": sc.count,
                    "valid": len(sc.valid_slots), "file_size": sc.file_size,
                    "expected_file_size": sc.expected_file_size,
                    "skipped": [{"slot": s.slot, "reason": s.reason} for s in sc.skipped],
                    "problems": [{"code": p.code, "detail": p.detail} for p in sc.problems],
                    "max_end": hexaddr(snap.max_end)},
        "capture": {"files": cap_files, "complete": complete,
                    "level": snap.capture_level,
                    "binding": j.histogram(),
                    "problems": [{"file": p.file, "code": p.code, "detail": p.detail,
                                  "prefix": p.prefix} for p in j.capture_problems]},
        "status": snap.status_histogram(),
        "meta_status": dict(sorted(Counter(u.meta_status for u in snap.units()).items())),
        "pin_reasons": dict(sorted(reasons.items())),
        "origins": dict(sorted(origins.items())),
        "classes": dict(sorted(classes.items())),
        "tallies": g17_tallies(checks),
        "helper_symbols": {"checked": sum(1 for h in hs if h[3] is not None),
                           "ok": sum(1 for h in hs if h[3]),
                           "mismatch": sorted({f"{h[1]}!={h[2]}" for h in hs if h[3] is False}),
                           "unnamed": sorted({h[0] for h in hs if h[1] is None})},
        "run_info": None if ri is None else {
            "code_address": hexaddr(ri.code_address), "code_size": hexaddr(ri.code_size),
            "prologue_end": hexaddr(ri.prologue_end), "tb_ret_off": ri.tb_ret_off,
            "epilogue_off": ri.epilogue_off, "icache_linesize": ri.icache_linesize,
            "tb_struct_size": ri.tb_struct_size, "highwater_off": hexaddr(ri.highwater_off),
            "loaded_max_end": hexaddr(ri.loaded_max_end), "cpuinfo": hexaddr(ri.cpuinfo),
            "flags": ri.flags, "prologue_sha256": sha256_id(ri.prologue)},
        "identity_checks": res.identity_checks,
        # ``--strict`` is not recorded: a strict import that succeeds is identical
        # to a non-strict one, so both must give the same store directory.
        "options": {"firmware": firmware, "plugin": plugin},
        "tools": {"scopt": __version__, "capstone": _capstone_version(),
                  "pyelftools": _pyelftools_version()},
    }
    return doc


def _capstone_version() -> str:
    import capstone

    return ".".join(str(x) for x in capstone.version_bind()[:2]) + f" ({capstone.__version__})"


def _pyelftools_version() -> str:
    import elftools

    return getattr(elftools, "__version__", "?")


def _capture_name(data: bytes) -> str:
    return f"capture-{hashlib.sha256(data).hexdigest()[:16]}.scm"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _write(path: Path, data: bytes, mode: int = 0o444, *, sync: bool = True) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        if sync:
            os.fsync(f.fileno())
    os.chmod(path, mode)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


#: threads used to fsync the store files. One fsync per file in sequence costs
#: one journal commit each (minutes for ~2000 code files on a rotating disk);
#: concurrent fsyncs share commits. Every file is still fsynced before the
#: directory fsync and the rename, so the durability guarantee is unchanged.
FSYNC_THREADS = 32


def _fsync_all(paths: Sequence[Path]) -> None:
    if not paths:
        return
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(FSYNC_THREADS, len(paths))) as ex:
        for _ in ex.map(_fsync_path, paths):  # re-raises the first OSError
            pass


def _tree(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file()}


def _same_tree(a: Path, b: Path) -> list[str]:
    ta, tb = _tree(a), _tree(b)
    diff = sorted(set(ta) ^ set(tb))
    for k in sorted(set(ta) & set(tb)):
        if k == "cache.sc":
            if sha256_sparse(ta[k]) != sha256_sparse(tb[k]):
                diff.append(k)
        elif ta[k].read_bytes() != tb[k].read_bytes():
            diff.append(k)
    return diff


def write_store(res: ImportResult, store: str | os.PathLike,
                progress: Callable[[str], None] | None = None) -> str:
    """Write ``store/snapshots/<hex>/`` (§4.3 step 4); returns its path.

    Every file is written into ``snapshots/.tmp-*``, fsynced, and the directory
    is renamed into place only when complete; an exception (including
    ``KeyboardInterrupt``) removes the temporary directory, so the store never
    holds a partial snapshot. ``progress`` receives short status lines.
    """
    say = progress or (lambda _m: None)
    snaps = Path(store) / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    final = snaps / snapshot_dir_name(res.snapshot.snapshot_id)
    tmp = snaps / f".tmp-{os.getpid()}-{secrets.token_hex(4)}"
    tmp.mkdir()
    try:
        with _ShLock(res.sc_path):
            sparse_copy(res.sc_path, tmp / "cache.sc")
        os.chmod(tmp / "cache.sc", 0o444)
        from ..formats.sc_v6 import read_sc6, sc6_snapshot_id

        cp6 = read_sc6(tmp / "cache.sc")
        cp = cp6.sc
        if sc6_snapshot_id(cp6) != res.snapshot.snapshot_id or \
                cp.record_bytes != res.join.sc.record_bytes:  # pragma: no cover
            raise ImportRejected("store_copy", "cache.sc copy differs from the imported file")
        caps = []
        for p in res.used_scm_paths:
            data = Path(p).read_bytes()
            name = _capture_name(data)
            caps.append(name)
            _write(tmp / name, data)
        if res.manifest_bytes is not None:
            _write(tmp / "manifest.json", res.manifest_bytes)
        if res.bundle is not None:
            _write(tmp / "bundle.json", canonical_json(res.bundle) + b"\n")
        if res.runtime_identity is not None:
            _write(tmp / "runtime_identity.json", canonical_json(res.runtime_identity) + b"\n")
        summary = dict(res.summary)
        summary["files"] = {"captures": sorted(caps),
                            "manifest": res.manifest_bytes is not None,
                            "bundle": res.bundle is not None,
                            "runtime_identity": res.runtime_identity is not None}
        summary["options"] = dict(summary["options"])
        _write(tmp / "snapshot.json", canonical_json(summary) + b"\n")
        _write(tmp / "units.jsonl", canonical_jsonl(res.regions))
        (tmp / "code").mkdir()
        code_files = []
        for u in res.snapshot.units():
            body = u.code + u.search
            p = tmp / "code" / f"{sha256_hex(body)}.bin"
            if not p.exists():
                _write(p, body, sync=False)
                code_files.append(p)
        _write(tmp / "evidence.jsonl", canonical_jsonl(res.evidence_rows()))
        say(f"store: syncing {len(code_files)} code files to disk")
        _fsync_all(code_files)
        _fsync_path(tmp / "code")
        _fsync_path(tmp)
        if final.exists():
            diff = _same_tree(tmp, final)
            if diff:
                raise ImportRejected("store_conflict", f"{final} exists and differs in {diff[:5]}")
            res.store_status = "exists"
        else:
            os.rename(tmp, final)
            _fsync_path(snaps)
            res.store_status = "created"
    finally:
        if tmp.exists():
            if res.store_status is None:
                say(f"store: removing {tmp.name}; nothing was stored")
            _rmtree(tmp)
    res.store_dir = str(final)
    object.__setattr__(res.snapshot, "path", str(final))
    return str(final)


def _rmtree(p: Path) -> None:
    def onerror(func, path, exc):  # read-only files
        os.chmod(os.path.dirname(path), 0o755)
        os.chmod(path, 0o644)
        func(path)
    shutil.rmtree(p, onerror=onerror)


def open_snapshot(path: str | os.PathLike) -> CacheSnapshot:
    """Rebuild the :class:`CacheSnapshot` of a store directory.

    The cross-check is re-run on the stored ``cache.sc`` and captures (with
    the stored manifest, bundle and RuntimeIdentity and the recorded
    firmware/plugin options); the SnapshotId and every region must equal the
    stored ``snapshot.json``/``units.jsonl``, otherwise ``store_mismatch``.
    """
    d = Path(path)
    meta = json.loads((d / "snapshot.json").read_text())
    opts = meta.get("options", {})
    files = meta.get("files", {})
    res = ImportCache(
        d / "cache.sc", [d / c for c in files.get("captures", [])],
        manifest=d / "manifest.json" if files.get("manifest") else None,
        bundle=d / "bundle.json" if files.get("bundle") else None,
        runtime_identity=d / "runtime_identity.json" if files.get("runtime_identity") else None,
        firmware=opts.get("firmware"), plugin=opts.get("plugin"),
        use_manifest_firmware=False, verify_files=False).run()
    if res.snapshot.snapshot_id != meta["snapshot_id"]:
        raise ImportRejected("store_mismatch", "snapshot_id differs on re-open")
    if canonical_jsonl(res.regions) != (d / "units.jsonl").read_bytes():
        raise ImportRejected("store_mismatch",
                             "units.jsonl differs on re-open (external ELF files changed?)")
    object.__setattr__(res.snapshot, "path", str(d))
    return res.snapshot


def is_store_snapshot(path: str | os.PathLike) -> bool:
    return (Path(path) / "snapshot.json").is_file() and (Path(path) / "cache.sc").is_file()


__all__ += ["is_store_snapshot", "check_bundle", "check_runtime_identity"]
