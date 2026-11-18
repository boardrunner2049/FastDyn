"""Phase-two FastDyn adapter for conservative identity execution bundles.

This module consumes firmware evidence and imports FastDyn patches. It is not
part of the patch-independent phase-one firmware optimizer.
"""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Any, Mapping

from .artifacts import (
    ExecutionBundle,
    BundleValidationError,
    MemoryRange,
    hash_file,
    publish_execution_bundle,
)
from .contracts import import_patch_contracts
from .firmware import qualify_firmware


DEFAULT_CONTRACT_ID = "fastdyn-guest-observations-v1"


def _integer(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def _memory_size(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    suffixes = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if text and text[-1].lower() in suffixes:
        return int(text[:-1], 0) * suffixes[text[-1].lower()]
    return int(text, 0)


def _memory_map(machine: Any, executable_ranges: tuple[Any, ...]) -> tuple[MemoryRange, ...]:
    ranges = []
    for name, memory in sorted((getattr(machine, "memories", {}) or {}).items()):
        start = _integer(getattr(memory, "memory_start", 0))
        size = _memory_size(getattr(memory, "memory_size"))
        kind_value = getattr(memory, "memory_type", "ram")
        kind = getattr(kind_value, "name", str(kind_value)).lower()
        permissions = "r-x" if kind in {"flash", "rom"} else "rw-"
        ranges.append(MemoryRange(str(name), start, start + size, permissions, kind))
    for index, item in enumerate(executable_ranges):
        ranges.append(
            MemoryRange(
                f"firmware-exec-{index}",
                item.original_start,
                item.original_end,
                "r-x",
                "firmware",
            )
        )
    return tuple(ranges)


def _snapshot(value: Any) -> Any:
    """Convert configuration state to stable, non-executable identity material."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.name
    if dataclasses.is_dataclass(value):
        return _snapshot(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _snapshot(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_snapshot(item) for item in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if hasattr(value, "__dict__"):
        return {
            key: _snapshot(item)
            for key, item in sorted(vars(value).items())
            if not key.startswith("_") and not callable(item)
        }
    return repr(value)


def _existing_patch_lines(path_value: Any) -> tuple[list[str], list[str]]:
    if path_value in (None, "", False):
        return [], []
    path = Path(path_value).expanduser()
    virtual_path = path / "virtuals.txt" if path.is_dir() else None
    modifier_path = path / "modifiers.txt" if path.is_dir() else path

    def lines(source: Path | None) -> list[str]:
        if source is None or not source.is_file():
            return []
        return [
            line.strip()
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    if not path.exists():
        raise FileNotFoundError(f"existing_config_path not found: {path}")
    return lines(virtual_path), lines(modifier_path)


def create_identity_bundle(
    machine: Any,
    destination: Path,
    *,
    cpu_index: int = 0,
    contract_id: str = DEFAULT_CONTRACT_ID,
) -> ExecutionBundle:
    """Publish an unchanged-image ExecutionBundle for one parsed FastDyn CPU.

    This is the A1 conservative path and the bootstrap artifact for Student 2. It performs
    no binary optimization. All configured patch anchors must already be numeric and must
    lie in file-backed executable ranges of the immutable ELF reference.
    """

    cpus = tuple(getattr(machine, "cpus", ()))
    try:
        cpu = cpus[cpu_index]
    except IndexError as exc:
        raise IndexError(f"CPU index {cpu_index} is not present") from exc
    image = Path(getattr(cpu, "binary")).expanduser().resolve()
    image_hash = hash_file(image)
    qualification = qualify_firmware(image, cpu)
    if not qualification.identity_eligible:
        raise BundleValidationError(
            "firmware identity qualification failed: "
            + ", ".join(qualification.reason_codes)
        )
    existing_virtuals, existing_modifiers = _existing_patch_lines(
        getattr(cpu, "exstng_config_path", None)
    )
    patch_plan, bindings = import_patch_contracts(
        modifiers=(*existing_modifiers, *(getattr(cpu, "modifiers", ()) or ())),
        virtuals=(*existing_virtuals, *(getattr(cpu, "virtuals", ()) or ())),
        image_hash=image_hash,
        cpu_index=cpu_index,
        target=qualification.target,
    )

    entry_points = set(qualification.entry_points)
    instrumentation = {
        "coverage": bool(getattr(machine.qemu_target_opts, "coverage", False)),
        "edge_coverage": bool(getattr(machine.qemu_target_opts, "edge_coverage", False)),
        "plugins": _snapshot(getattr(cpu, "plugin_config", {}) or {}),
    }
    device_model = {
        "models": _snapshot(getattr(machine, "models", {}) or {}),
        "devices": _snapshot(getattr(machine, "parsed_device", {}) or {}),
    }
    patch_plan = {
        **patch_plan,
        "qualification": qualification.to_dict(),
    }
    return publish_execution_bundle(
        destination,
        image=image,
        original_image_hash=image_hash,
        target=qualification.target,
        entry_points=entry_points,
        memory_map=_memory_map(machine, qualification.executable_ranges),
        patch_plan=patch_plan,
        patch_bindings=bindings,
        origin_ranges=qualification.executable_ranges,
        contract_id=contract_id,
        instrumentation=instrumentation,
        device_model=device_model,
    )
