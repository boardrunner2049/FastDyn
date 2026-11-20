"""Native callee identities backed by an explicitly prepared analysis input."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..canon import is_sha256_id, sha256_id
from ..formats.bundle_v1 import runtime_identity_id
from ..formats.sc_v5 import read_sc
from ..formats.scm_v1 import parse_scm
from ..importer.classify import HostContext
from ..importer.join import ImportRejected, join
from ..importer.native_callee import CalleeResolutionError, NativeCallee, NativeCalleeResolver
from ..model.unit import HelperBinding
from .api import AnalysisSpec, ScopeRef
from .host_calls import HostCallSite
from .store import AnalysisInput, value_digest

NATIVE_IMAGES = "native-images"
MANIFEST_VERSION = 1


@dataclass(frozen=True, slots=True)
class NativeCallBinding:
    call: HostCallSite
    callee: NativeCallee | None
    reason: str | None
    input_id: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _ImageBinding:
    helper: HelperBinding
    callee: NativeCallee | None
    reason: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class NativeImageManifest:
    snapshot_id: str
    runtime_identity_id: str | None
    capture_ids: tuple[str, ...]
    backend_revision: str
    bridge_version: int
    policy_version: int
    bridge_sha256: str | None
    bindings: Mapping[tuple[str, int], _ImageBinding]
    reason: str | None = None
    detail: str = ""

    def pre_chain_input_id(self) -> str:
        """Reproduce the native input digest used before nested frame witnesses."""
        bindings = dict(self.bindings)
        for key, binding in self.bindings.items():
            callee = binding.callee
            if callee is not None and callee.frame_chain:
                # The digest includes the dataclass type and every declared field.
                old_callee = NativeCallee(**{f.name: getattr(callee, f.name)
                                            for f in fields(NativeCallee)})
                bindings[key] = replace(binding, callee=old_callee)
        return value_digest([NATIVE_IMAGES, MANIFEST_VERSION, replace(self, bindings=bindings)])


def _verified_captures(snapshot: Any):
    """Hash and parse the very same bytes referenced by the snapshot manifest."""
    d = Path(snapshot.path)
    meta = json.loads((d / "snapshot.json").read_bytes())
    if meta["snapshot_id"] != snapshot.snapshot_id:
        raise ValueError("stored snapshot identity differs")
    expected = {row["name"]: row["sha256"] for row in meta["capture"]["files"]}
    captures, ids = [], {}
    for name in meta["files"].get("captures", ()):
        if Path(name).name != name or name not in expected:
            raise ValueError("invalid capture path or missing content identity")
        p = d / name
        data = p.read_bytes()
        capture_id = sha256_id(data)
        if capture_id != expected[name]:
            raise ValueError(f"{name}: stored capture digest differs")
        capture = parse_scm(data, path=str(p))
        ids[str(p)] = capture_id
        captures.append(capture)
    return join(read_sc(d / "cache.sc", versions=(5, 6)), captures), ids


def prepare_native_images(snapshot: Any, *, backend_revision: str, bridge_version: int,
                          policy_version: int, bridge_sha256: str | None = None) -> AnalysisInput:
    """Read image and capture files before analysis and freeze the verified function bytes.

    Build a fresh input for each optimization invocation. No analysis compute
    function reads a native file. The returned content id also binds refusals,
    backend identity and the policy version used by an eventual optimizer.
    """
    if not backend_revision or any(type(v) is not int or v < 1 for v in
                                   (bridge_version, policy_version)):
        raise ValueError("native image input requires a backend revision and positive versions")
    if bridge_sha256 is not None and not is_sha256_id(bridge_sha256):
        raise ValueError("invalid native bridge SHA-256 id")
    bindings: dict[tuple[str, int], _ImageBinding] = {}
    capture_ids: tuple[str, ...] = ()
    reason, detail = None, ""
    runtime = snapshot.runtime
    runtime_id = runtime.get("runtime_identity_id") if runtime is not None else None
    if not is_sha256_id(runtime_id) or runtime_identity_id(runtime) != runtime_id:
        reason, detail = "native_image_mismatch", "missing or invalid runtime identity"
    elif not getattr(snapshot, "path", None):
        reason, detail = "native_image_unavailable", "snapshot has no stored capture"
    else:
        try:
            joined, ids = _verified_captures(snapshot)
            capture_ids = tuple(sorted(ids.values()))
            if not capture_ids:
                reason, detail = "native_image_unavailable", "snapshot has no stored capture"
            else:
                resolvers = {}
                images = {}
                for unit in snapshot.units():
                    if not unit.bindings.helpers:
                        continue
                    slot = joined.slots.get(unit.slot)
                    cap = slot.source if slot is not None and slot.bound else None
                    if cap is None or cap.run_info is None:
                        continue
                    if cap.path not in resolvers:
                        context = HostContext.from_capture(cap)
                        resolvers[cap.path] = NativeCalleeResolver(
                            context, cap.run_info, runtime, ids[cap.path], images)
                    resolver = resolvers[cap.path]
                    for helper in unit.bindings.helpers:
                        key = unit.uid, helper.id
                        try:
                            bindings[key] = _ImageBinding(helper, resolver.resolve(helper))
                        except CalleeResolutionError as error:
                            bindings[key] = _ImageBinding(helper, None, error.reason, str(error))
        except (OSError, ValueError, KeyError, ImportRejected) as error:
            bindings.clear()
            reason, detail = "native_image_mismatch", str(error)
    manifest = NativeImageManifest(snapshot.snapshot_id, runtime_id, capture_ids, backend_revision,
                                   bridge_version, policy_version, bridge_sha256,
                                   MappingProxyType(bindings), reason, detail)
    return AnalysisInput(value_digest([NATIVE_IMAGES, MANIFEST_VERSION, manifest]), manifest)


def _compute(ctx: Any, scope: ScopeRef) -> tuple[NativeCallBinding, ...]:
    calls = ctx.analysis("host-calls", scope, min_version=1).value
    prepared = ctx.external_input(NATIVE_IMAGES)
    manifest = None if prepared is None else prepared.value
    input_id = None if prepared is None else prepared.input_id
    out = []
    for call in calls:
        callee, reason, detail = None, call.reason, ""
        if reason is None:
            if not isinstance(manifest, NativeImageManifest):
                reason, detail = "native_image_unavailable", "native image input was not prepared"
            elif manifest.snapshot_id != ctx.snapshot_id:
                reason, detail = "native_image_mismatch", "native image input is for a different snapshot"
            elif manifest.reason is not None:
                reason, detail = manifest.reason, manifest.detail
            else:
                row = manifest.bindings.get((call.uid, call.helper_id))
                actual = HelperBinding(call.helper_id, call.helper_name, call.module, call.symbol,
                                       call.helper_flags, call.target_abs)
                if row is None or row.helper != actual:
                    reason, detail = "call_binding_unknown", "no matching prepared helper binding"
                else:
                    callee, reason, detail = row.callee, row.reason, row.detail
        out.append(NativeCallBinding(call, callee, reason, input_id, detail))
    ctx.certify()
    return tuple(out)


ANALYSES = (AnalysisSpec(name="native-callees", version=1, scope="unit",
                         requires=(("host-calls", 1),), inputs=frozenset({f"external:{NATIVE_IMAGES}"}),
                         compute=_compute, result_schema=tuple),)
