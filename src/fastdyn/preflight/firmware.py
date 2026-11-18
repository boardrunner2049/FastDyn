"""Conservative firmware evidence extraction and target qualification."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from elftools.elf.constants import P_FLAGS
from elftools.elf.elffile import ELFFile

from .candidate import FirmwareCandidate, publish_firmware_candidate
from .model import (
    BundleValidationError,
    MemoryRange,
    OriginRange,
    TargetDescription,
    hash_file,
)


_CORTEX_M_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "cortex-m0": ("armv6-m", ("armv6-m", "thumb")),
    "cortex-m0+": ("armv6-m", ("armv6-m", "thumb")),
    "cortex-m0plus": ("armv6-m", ("armv6-m", "thumb")),
    "cortex-m3": ("armv7-m", ("armv7-m", "thumb")),
}


@dataclass(frozen=True)
class TargetQualification:
    """Evidence-backed eligibility decision for one immutable image and target."""

    target: TargetDescription
    identity_eligible: bool
    rewrite_eligible: bool
    reason_codes: tuple[str, ...]
    executable_ranges: tuple[OriginRange, ...]
    entry_points: tuple[int, ...]
    has_relocations: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible qualification report."""

        return {
            "target": self.target.to_dict(),
            "identity_eligible": self.identity_eligible,
            "rewrite_eligible": self.rewrite_eligible,
            "reason_codes": list(self.reason_codes),
            "executable_ranges": [item.to_dict() for item in self.executable_ranges],
            "entry_points": list(self.entry_points),
            "has_relocations": self.has_relocations,
        }


def target_from_cpu(cpu: Any) -> TargetDescription:
    """Create an exact target description from a FastDyn CPU object."""

    arch = str(getattr(cpu, "arch", "")).strip().lower()
    cpu_name = str(getattr(cpu, "cpu", "")).strip().lower()
    if cpu_name in _CORTEX_M_TARGETS:
        _profile, features = _CORTEX_M_TARGETS[cpu_name]
        return TargetDescription("arm", "thumb", "little", cpu_name, features)
    if arch in {"x86_64", "amd64"} or cpu_name in {"x86_64", "qemu64", "host"}:
        return TargetDescription("x86_64", "long", "little", cpu_name or "x86_64", ())
    if arch in {"aarch64", "arm64"}:
        return TargetDescription("aarch64", "a64", "little", cpu_name or "generic", ())
    if arch in {"riscv64", "riscv"}:
        return TargetDescription("riscv64", "rv64", "little", cpu_name or "generic", ())
    raise BundleValidationError(
        f"unsupported or ambiguous target arch={arch!r}, cpu={cpu_name!r}; "
        "preflight requires an exact ISA/mode"
    )


def _elf_evidence(
    image: Path,
) -> tuple[tuple[OriginRange, ...], tuple[int, ...], bool, str, str]:
    try:
        with image.open("rb") as stream:
            elf = ELFFile(stream)
            ranges = []
            for segment in elf.iter_segments():
                if segment["p_type"] != "PT_LOAD" or not (segment["p_flags"] & P_FLAGS.PF_X):
                    continue
                start = int(segment["p_vaddr"])
                size = int(segment["p_filesz"])
                if size:
                    ranges.append(OriginRange(start, start + size, start, start + size))
            entries = []
            elf_entry = int(elf.header["e_entry"])
            if elf_entry:
                entries.append(elf_entry & ~1)
            has_relocations = any(
                section["sh_type"] in {"SHT_REL", "SHT_RELA"}
                for section in elf.iter_sections()
            )
            machine = str(elf.header["e_machine"])
            endianness = "little" if elf.little_endian else "big"
    except (OSError, ValueError) as exc:
        raise BundleValidationError(f"cannot inspect ELF image {image}: {exc}") from exc
    if not ranges:
        raise BundleValidationError(
            f"ELF image has no file-backed executable PT_LOAD range: {image}"
        )
    return tuple(ranges), tuple(entries), has_relocations, machine, endianness


def _read_virtual_bytes(image: Path, address: int, size: int) -> bytes | None:
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            if segment["p_type"] != "PT_LOAD":
                continue
            start = int(segment["p_vaddr"])
            file_size = int(segment["p_filesz"])
            if start <= address and address + size <= start + file_size:
                stream.seek(int(segment["p_offset"]) + address - start)
                return stream.read(size)
    return None


def qualify_firmware(image: Path, cpu: Any) -> TargetQualification:
    """Qualify identity emission and bounded BOLT rewrite support for a firmware image.

    Cortex-M identity bundles are supported, while rewrite eligibility remains false until
    an ARM32/Thumb BOLT backend is supplied and qualified. This is an explicit source-audit
    result, not a claim that the firmware is semantically invalid.
    """

    image = image.resolve()
    target = target_from_cpu(cpu)
    ranges, entries, has_relocations, elf_machine, endianness = _elf_evidence(image)
    target = TargetDescription(
        target.isa,
        target.mode,
        endianness,
        target.cpu,
        target.features,
    )
    reasons: list[str] = []
    rewrite_eligible = False
    expected_machines = {
        "arm": "EM_ARM",
        "x86_64": "EM_X86_64",
        "aarch64": "EM_AARCH64",
        "riscv64": "EM_RISCV",
    }
    identity_eligible = expected_machines.get(target.isa) == elf_machine
    if not identity_eligible:
        reasons.append("elf_target_mismatch")

    if target.isa == "arm" and target.mode == "thumb":
        reasons.append("bolt_arm32_thumb_backend_unavailable")
        if not has_relocations:
            reasons.append("elf_has_no_relocations")
        vector_value = getattr(cpu, "init_nsvtor", None)
        vector_base = (
            vector_value
            if isinstance(vector_value, int)
            else int(str(vector_value), 0)
            if vector_value is not None
            else None
        )
        # Address zero is a valid and common Cortex-M vector-table base.
        vector = (
            _read_virtual_bytes(image, vector_base, 8)
            if vector_base is not None
            else None
        )
        if vector is None:
            reasons.append("vector_table_not_file_backed_at_declared_base")
        else:
            reset = int.from_bytes(vector[4:8], "little") & ~1
            if reset:
                entries = tuple(sorted(set((*entries, reset))))
    elif target.isa in {"x86_64", "aarch64", "riscv64"}:
        bolt = shutil.which("llvm-bolt")
        if bolt is None:
            reasons.append("llvm_bolt_executable_unavailable")
        else:
            rewrite_eligible = True
            reasons.append("rewrite_requires_audited_pass_policy")
    else:
        reasons.append("target_not_qualified")

    return TargetQualification(
        target=target,
        identity_eligible=identity_eligible,
        rewrite_eligible=rewrite_eligible,
        reason_codes=tuple(reasons),
        executable_ranges=ranges,
        entry_points=tuple(sorted(set(entries))),
        has_relocations=has_relocations,
    )


def create_identity_firmware_candidate(
    image: Path,
    cpu: Any,
    destination: Path,
    *,
    memory_map: Iterable[MemoryRange] = (),
) -> FirmwareCandidate:
    """Publish a patch-independent identity candidate for one firmware image."""

    image = image.resolve()
    qualification = qualify_firmware(image, cpu)
    if not qualification.identity_eligible:
        raise BundleValidationError(
            "firmware identity qualification failed: "
            + ", ".join(qualification.reason_codes)
        )
    declared_memory = tuple(memory_map) + tuple(
        MemoryRange(
            f"firmware-exec-{index}",
            item.original_start,
            item.original_end,
            "r-x",
            "firmware",
        )
        for index, item in enumerate(qualification.executable_ranges)
    )
    return publish_firmware_candidate(
        destination,
        image=image,
        original_image_hash=hash_file(image),
        target=qualification.target,
        entry_points=qualification.entry_points,
        memory_map=declared_memory,
        origin_ranges=qualification.executable_ranges,
        optimization_report={
            "status": "identity",
            "passes": [],
            "code_growth_bytes": 0,
            "reason_codes": list(qualification.reason_codes),
            "qualification": qualification.to_dict(),
        },
    )
