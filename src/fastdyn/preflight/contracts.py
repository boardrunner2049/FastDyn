"""Import FastDyn PC-triggered actions as explicit patch contracts."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from .artifacts import BundleValidationError, PatchBinding, TargetDescription


def _binding_id(
    image_hash: str,
    cpu_index: int,
    kind: str,
    ordinal: int,
    pc: int,
) -> str:
    material = f"{image_hash}\0{cpu_index}\0{kind}\0{ordinal}\0{pc:x}".encode("utf-8")
    return f"patch-{hashlib.sha256(material).hexdigest()[:24]}"


def _patch_identity(image_hash: str, cpu_index: int, pc: int) -> str:
    return f"sha256:{image_hash}:cpu{cpu_index}:pc:{pc:#x}"


def _numeric_anchor(value: Any, description: str) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError as exc:
        raise BundleValidationError(
            f"{description} does not have a resolved numeric anchor"
        ) from exc


def _parse_modifier(line: Any) -> tuple[int, str]:
    if hasattr(line, "at") and hasattr(line, "patch"):
        return _numeric_anchor(line.at, f"modifier {line!r}"), str(line.patch).strip()
    parts = str(line).strip().split(maxsplit=1)
    if len(parts) != 2:
        raise BundleValidationError(f"invalid modifier contract {line!r}")
    return _numeric_anchor(parts[0], f"modifier {line!r}"), parts[1]


def _parse_virtual(line: Any) -> tuple[int, str, list[str]]:
    if hasattr(line, "at") and hasattr(line, "instruction"):
        return (
            _numeric_anchor(line.at, f"virtual {line!r}"),
            str(line.instruction),
            [str(item) for item in (getattr(line, "args", ()) or ())],
        )
    parts = str(line).strip().split()
    if len(parts) < 2:
        raise BundleValidationError(f"invalid virtual contract {line!r}")
    return _numeric_anchor(parts[0], f"virtual {line!r}"), parts[1], parts[2:]


def _modifier_binding(
    line: Any,
    *,
    image_hash: str,
    cpu_index: int,
    ordinal: int,
    target: TargetDescription,
) -> PatchBinding:
    anchor, expression = _parse_modifier(line)
    pc = anchor & ~1 if target.mode == "thumb" else anchor
    return PatchBinding(
        binding_id=_binding_id(image_hash, cpu_index, "modifier", ordinal, pc),
        original_identity=_patch_identity(image_hash, cpu_index, pc),
        original_pc=pc,
        current_pc=pc,
        isa=target.isa,
        mode=target.mode,
        trigger_phase="instruction_entry",
        kind="modifier",
        action={"expression": expression},
        inputs=("architectural_state",),
        outputs=("architectural_state",),
    )


def _virtual_binding(
    line: Any,
    *,
    image_hash: str,
    cpu_index: int,
    ordinal: int,
    target: TargetDescription,
) -> PatchBinding:
    anchor, instruction, args = _parse_virtual(line)
    pc = anchor & ~1 if target.mode == "thumb" else anchor
    return PatchBinding(
        binding_id=_binding_id(image_hash, cpu_index, "virtual", ordinal, pc),
        original_identity=_patch_identity(image_hash, cpu_index, pc),
        original_pc=pc,
        current_pc=pc,
        isa=target.isa,
        mode=target.mode,
        trigger_phase="instruction_entry",
        kind="virtual",
        action={"instruction": instruction, "args": args},
        inputs=("architectural_state", "environment"),
        outputs=("architectural_state", "event_stream", "environment"),
    )


def import_patch_contracts(
    *,
    modifiers: Iterable[Any],
    virtuals: Iterable[Any],
    image_hash: str,
    cpu_index: int,
    target: TargetDescription,
) -> tuple[dict[str, Any], tuple[PatchBinding, ...]]:
    """Return a normalized patch plan and exact identity bindings.

    Symbolic anchors are deliberately rejected here. They must be resolved against the
    immutable reference image before preflight, so a later rewriter never guesses or uses
    nearest-address matching.
    """

    modifier_values = tuple(modifiers)
    virtual_values = tuple(virtuals)
    bindings = tuple(
        _modifier_binding(
            item,
            image_hash=image_hash,
            cpu_index=cpu_index,
            ordinal=index,
            target=target,
        )
        for index, item in enumerate(modifier_values)
    ) + tuple(
        _virtual_binding(
            item,
            image_hash=image_hash,
            cpu_index=cpu_index,
            ordinal=index,
            target=target,
        )
        for index, item in enumerate(virtual_values)
    )
    patch_plan = {
        "cpu_index": cpu_index,
        "actions": [
            {
                "binding_id": item.binding_id,
                "original_identity": item.original_identity,
                "kind": item.kind,
                "action": dict(item.action),
                "trigger_phase": item.trigger_phase,
                "multiplicity": item.multiplicity,
            }
            for item in bindings
        ],
    }
    return patch_plan, bindings
