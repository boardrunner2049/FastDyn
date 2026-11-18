"""External inputs of the WP20 analyses: the guest firmware ELF and the patch-plan content.

Neither is part of a snapshot (B1 stores only hashes of them in the bundle),
so the analyses resolve them here, always **by hash**:

* the guest image is used only if its file sha256 equals the bundle's
  ``image_hash``;
* the patch plan is used only if it was built for the bundle's
  ``patch_plan_hash`` (:meth:`~scopt.analysis.patch_join.PatchPlan.load`
  checks every file against the manifest).

Callers (CLI, pass manager) :func:`register` what they have; otherwise the
store directory of the snapshot is consulted (``snapshot.json`` options /
``manifest.json`` firmware path, and the manifest's content hashes, which
make an empty ``modifiers.txt`` known without the file). Anything not
resolvable is ``None`` / an *unavailable* plan, and every consumer fails
closed on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..decode.guest_symbols import GuestImage, GuestImageError, load_guest_image
from .patch_join import PatchPlan, PatchPlanError

__all__ = ["register", "registered", "clear", "guest_image_for", "patch_plan_for",
           "snapshot_dir_of", "resolve_for_snapshot"]

_IMAGES: dict[str, GuestImage] = {}
_PLANS: dict[str, PatchPlan] = {}


def register(*, image: GuestImage | None = None, plan: PatchPlan | None = None) -> None:
    if image is not None:
        _IMAGES[image.sha256] = image
    if plan is not None and plan.patch_plan_hash:
        _PLANS[plan.patch_plan_hash] = plan


def registered() -> dict[str, Any]:
    return {"images": sorted(_IMAGES), "plans": sorted(_PLANS)}


def clear() -> None:
    _IMAGES.clear()
    _PLANS.clear()


def snapshot_dir_of(obj: Any) -> Path | None:
    """The store directory of a snapshot, a view over one, or an analysis ctx."""
    for path in (("path",), ("working_set", "base", "path"), ("_store", "view", "working_set",
                                                                "base", "path")):
        o = obj
        for a in path:
            o = getattr(o, a, None)
            if o is None:
                break
        if isinstance(o, (str, Path)) and Path(o).is_dir():
            return Path(o)
    return None


def _firmware_candidates(d: Path) -> list[str]:
    out = []
    try:
        meta = json.loads((d / "snapshot.json").read_text())
        fw = (meta.get("options") or {}).get("firmware")
        if fw:
            out.append(fw)
    except (OSError, ValueError):
        pass
    try:
        man = json.loads((d / "manifest.json").read_text())
        fw = ((man.get("inputs") or {}).get("native_files") or {}).get("firmware") or {}
        if fw.get("path"):
            out.append(fw["path"])
    except (OSError, ValueError):
        pass
    return out


def guest_image_for(ctx_or_snapshot: Any, bundle: Any) -> GuestImage | None:
    want = (bundle or {}).get("image_hash") if bundle is not None else None
    if want is None:
        return None
    if want in _IMAGES:
        return _IMAGES[want]
    d = snapshot_dir_of(ctx_or_snapshot)
    if d is None:
        return None
    for fw in _firmware_candidates(d):
        try:
            img = load_guest_image(fw)
        except (OSError, GuestImageError, ValueError):
            continue
        if img.sha256 == want:
            _IMAGES[want] = img
            return img
    return None


def patch_plan_for(ctx_or_snapshot: Any, bundle: Any) -> PatchPlan:
    pph = (bundle or {}).get("patch_plan_hash") if bundle is not None else None
    if pph in _PLANS:
        return _PLANS[pph]
    d = snapshot_dir_of(ctx_or_snapshot)
    if d is not None and (d / "manifest.json").is_file():
        try:
            plan = PatchPlan.load(d / "manifest.json", bundle=bundle)
        except (PatchPlanError, OSError, ValueError):
            return PatchPlan.unknown(pph)
        return plan
    return PatchPlan.unknown(pph)


def resolve_for_snapshot(snapshot: Any, *, elf: str | None = None, prep: str | None = None,
                         modifiers: str | None = None, virtuals: str | None = None
                         ) -> tuple[GuestImage | None, PatchPlan]:
    """CLI helper: build (and register) the inputs for ``snapshot`` from explicit files,
    falling back to the store directory. Raises on a file that does not match."""
    bundle = snapshot.bundle
    img = None
    if elf:
        img = load_guest_image(elf)
        if bundle is not None and img.sha256 != bundle.get("image_hash"):
            raise GuestImageError(f"{elf}: sha256 {img.sha256} != bundle image_hash "
                                  f"{bundle.get('image_hash')}")
        register(image=img)
    else:
        img = guest_image_for(snapshot, bundle)
    d = snapshot_dir_of(snapshot)
    man = d / "manifest.json" if d is not None and (d / "manifest.json").is_file() else None
    if prep or modifiers or virtuals:
        if man is None:
            raise PatchPlanError("no_manifest", "the snapshot has no manifest.json")
        if prep:
            plan = PatchPlan.from_prep(prep, manifest=man, bundle=bundle)
        else:
            plan = PatchPlan.load(man, bundle=bundle, modifiers=modifiers, virtuals=virtuals)
        register(plan=plan)
    else:
        plan = patch_plan_for(snapshot, bundle)
    return img, plan
