"""Immutable publication of one trained BOLT deployment decision.

This module does not choose a policy from final measurements.  It consumes a
previously frozen training-only selection, verifies the corresponding held-out
evidence and artifact hashes, and atomically publishes the exact executable
selected for one workload and layout contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from elftools.elf.constants import P_FLAGS
from elftools.elf.elffile import ELFFile
import capstone

from .candidate import FirmwareCandidate, publish_firmware_candidate
from .model import (
    BundleValidationError,
    MemoryRange,
    OriginRange,
    TargetDescription,
    canonical_json_bytes,
    hash_file,
    hash_json,
    require_hex_digest,
)


SCHEMA_VERSION = 1
MANIFEST_FILE = "bolt-deployment.json"
IMAGE_FILE = "image.elf"
SELECTION_FILE = "policy-selection.json"
EVIDENCE_FILE = "held-out-policy-report.json"
LAYOUT_MODES = {"relocatable", "fixed_layout"}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"cannot read {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


@dataclass(frozen=True)
class BoltDeploymentArtifact:
    """One validated executable selected by a frozen BOLT policy."""

    path: Path
    decision_id: str
    workload_id: str
    layout_mode: str
    choice: str
    image_hash: str
    original_image_hash: str
    selection_hash: str
    evidence_hash: str

    @property
    def image_path(self) -> Path:
        return self.path / IMAGE_FILE

    def identity_material(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "bolt_deployment_decision",
            "workload_id": self.workload_id,
            "layout_mode": self.layout_mode,
            "choice": self.choice,
            "image_hash": self.image_hash,
            "original_image_hash": self.original_image_hash,
            "selection_hash": self.selection_hash,
            "evidence_hash": self.evidence_hash,
        }


def _artifact_for_choice(
    evidence: Mapping[str, Any], layout_mode: str, choice: str
) -> Mapping[str, Any]:
    if choice == "reference":
        return evidence["artifacts"]["reference"]
    if choice == "full_fop" and layout_mode == "relocatable":
        return evidence["artifacts"]["full_relocatable"]
    if choice == "stock_bolt" and layout_mode == "relocatable":
        return evidence["artifacts"]["stock_bolt"]
    if choice == "compact_fop":
        return evidence["artifacts"]["compact_fixed_layout"]
    raise BundleValidationError(
        f"choice {choice!r} is not allowed for layout mode {layout_mode!r}"
    )


def publish_bolt_deployment(
    destination: Path,
    *,
    selection_path: Path,
    evidence_path: Path,
    workload_id: str,
    layout_mode: str,
) -> BoltDeploymentArtifact:
    """Verify and atomically publish the executable chosen during training."""

    destination = destination.resolve()
    selection_path = selection_path.resolve()
    evidence_path = evidence_path.resolve()
    if destination.exists():
        raise FileExistsError(f"BOLT deployment destination exists: {destination}")
    if layout_mode not in LAYOUT_MODES:
        raise BundleValidationError(f"unknown layout mode {layout_mode!r}")

    selection = _load_json(selection_path)
    evidence = _load_json(evidence_path)
    if selection.get("status") != "frozen_before_held_out_measurement":
        raise BundleValidationError("BOLT policy selection was not frozen")
    if evidence.get("status") != "held_out_policy_evaluation_complete":
        raise BundleValidationError("held-out BOLT evidence is incomplete")
    selection_hash = hash_file(selection_path)
    evidence_hash = hash_file(evidence_path)
    if evidence.get("selection", {}).get("sha256") != selection_hash:
        raise BundleValidationError("held-out evidence does not bind the selection")
    if not evidence.get("selection", {}).get("frozen_before_held_out_measurement"):
        raise BundleValidationError("evidence does not confirm a frozen selection")

    try:
        choice_key = (
            "relocatable_policy" if layout_mode == "relocatable"
            else "fixed_layout_policy"
        )
        choice = str(selection["choices"][workload_id][choice_key])
        workload = evidence["workloads"][workload_id]
    except (KeyError, TypeError) as exc:
        raise BundleValidationError(
            f"workload {workload_id!r} is absent from the frozen policy: {exc}"
        ) from exc
    evidence_choice_key = "relocatable" if layout_mode == "relocatable" else "fixed_layout"
    if workload["training_choice"][evidence_choice_key] != choice:
        raise BundleValidationError("held-out evidence records a different frozen choice")
    if not workload.get("all_outputs_match"):
        raise BundleValidationError("selected workload failed output validation")

    artifact = _artifact_for_choice(evidence, layout_mode, choice)
    image = Path(str(artifact["path"])).resolve()
    if not image.is_file():
        raise BundleValidationError(f"selected BOLT artifact is missing: {image}")
    image_hash = hash_file(image)
    if image_hash != artifact.get("sha256"):
        raise BundleValidationError("selected BOLT artifact hash mismatch")
    original_image_hash = str(evidence["artifacts"]["reference"]["sha256"])
    require_hex_digest("original_image_hash", original_image_hash)

    identity = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "bolt_deployment_decision",
        "workload_id": workload_id,
        "layout_mode": layout_mode,
        "choice": choice,
        "image_hash": image_hash,
        "original_image_hash": original_image_hash,
        "selection_hash": selection_hash,
        "evidence_hash": evidence_hash,
    }
    manifest = {
        **identity,
        "decision_id": hash_json(identity),
        "artifacts": {
            "image": IMAGE_FILE,
            "selection": SELECTION_FILE,
            "evidence": EVIDENCE_FILE,
        },
        "decision": {
            "source": "training_only_frozen_policy",
            "held_out_used_for_retuning": False,
            "exact_reference_fallback": choice == "reference",
            "minimum_useful_training_cpu_ratio": evidence["selection"][
                "minimum_useful_cpu_ratio"
            ],
        },
        "claim_boundaries": dict(evidence["claims"]),
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        shutil.copyfile(image, staging / IMAGE_FILE)
        shutil.copyfile(selection_path, staging / SELECTION_FILE)
        shutil.copyfile(evidence_path, staging / EVIDENCE_FILE)
        _write_json(staging / MANIFEST_FILE, manifest)
        load_bolt_deployment(staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_bolt_deployment(destination)


def publish_hot_fop_fallback_deployment(
    destination: Path,
    *,
    selection_path: Path,
    evidence_path: Path,
    workload_id: str,
) -> BoltDeploymentArtifact:
    """Publish the stock-BOLT fallback frozen before a hot-FOP held-out run."""

    destination = destination.resolve()
    selection_path = selection_path.resolve()
    evidence_path = evidence_path.resolve()
    if destination.exists():
        raise FileExistsError(f"BOLT deployment destination exists: {destination}")
    selection = _load_json(selection_path)
    evidence = _load_json(evidence_path)
    if selection.get("status") != "frozen_before_hot_fop_held_out_measurement":
        raise BundleValidationError("hot-FOP policy selection was not frozen")
    if evidence.get("status") != "held_out_hot_fop_evaluation_complete":
        raise BundleValidationError("hot-FOP held-out evidence is incomplete")
    selection_hash = hash_file(selection_path)
    evidence_hash = hash_file(evidence_path)
    if evidence.get("selection", {}).get("sha256") != selection_hash:
        raise BundleValidationError("hot-FOP evidence does not bind the selection")
    if evidence.get("decision") != "retain_stock_bolt_inconclusive_or_below_gate":
        raise BundleValidationError("hot-FOP evidence did not select stock BOLT")
    if evidence.get("workload_id") != workload_id or not evidence.get(
        "all_outputs_match"
    ):
        raise BundleValidationError("hot-FOP workload identity or outputs are invalid")
    artifact = _artifact_for_choice(evidence, "relocatable", "stock_bolt")
    image = Path(str(artifact["path"])).resolve()
    if not image.is_file() or hash_file(image) != artifact.get("sha256"):
        raise BundleValidationError("selected stock-BOLT artifact hash mismatch")
    original_image_hash = str(evidence["artifacts"]["reference"]["sha256"])
    require_hex_digest("original_image_hash", original_image_hash)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "bolt_deployment_decision",
        "workload_id": workload_id,
        "layout_mode": "relocatable",
        "choice": "stock_bolt",
        "image_hash": hash_file(image),
        "original_image_hash": original_image_hash,
        "selection_hash": selection_hash,
        "evidence_hash": evidence_hash,
    }
    manifest = {
        **identity,
        "decision_id": hash_json(identity),
        "artifacts": {
            "image": IMAGE_FILE,
            "selection": SELECTION_FILE,
            "evidence": EVIDENCE_FILE,
        },
        "decision": {
            "source": "frozen_hot_fop_rule_with_stock_bolt_fallback",
            "held_out_used_for_retuning": False,
            "exact_reference_fallback": False,
            "minimum_useful_training_cpu_ratio": selection["measurement"][
                "minimum_useful_ratio"
            ],
        },
        "claim_boundaries": dict(evidence["claims"]),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        shutil.copyfile(image, staging / IMAGE_FILE)
        shutil.copyfile(selection_path, staging / SELECTION_FILE)
        shutil.copyfile(evidence_path, staging / EVIDENCE_FILE)
        _write_json(staging / MANIFEST_FILE, manifest)
        load_bolt_deployment(staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_bolt_deployment(destination)


def load_bolt_deployment(path: Path) -> BoltDeploymentArtifact:
    """Load and rehash a published BOLT deployment decision."""

    path = path.resolve()
    manifest = _load_json(path / MANIFEST_FILE)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleValidationError("unsupported BOLT deployment schema")
    if manifest.get("artifact_kind") != "bolt_deployment_decision":
        raise BundleValidationError("artifact is not a BOLT deployment decision")
    if manifest.get("artifacts") != {
        "image": IMAGE_FILE,
        "selection": SELECTION_FILE,
        "evidence": EVIDENCE_FILE,
    }:
        raise BundleValidationError("BOLT deployment artifact names do not match schema")
    try:
        artifact = BoltDeploymentArtifact(
            path=path,
            decision_id=str(manifest["decision_id"]),
            workload_id=str(manifest["workload_id"]),
            layout_mode=str(manifest["layout_mode"]),
            choice=str(manifest["choice"]),
            image_hash=str(manifest["image_hash"]),
            original_image_hash=str(manifest["original_image_hash"]),
            selection_hash=str(manifest["selection_hash"]),
            evidence_hash=str(manifest["evidence_hash"]),
        )
    except (KeyError, TypeError) as exc:
        raise BundleValidationError(f"invalid BOLT deployment manifest: {exc}") from exc
    if artifact.layout_mode not in LAYOUT_MODES:
        raise BundleValidationError("invalid BOLT deployment layout mode")
    for name in (
        "decision_id", "image_hash", "original_image_hash", "selection_hash",
        "evidence_hash",
    ):
        require_hex_digest(name, getattr(artifact, name))
    if hash_file(artifact.image_path) != artifact.image_hash:
        raise BundleValidationError("BOLT deployment image hash mismatch")
    if hash_file(path / SELECTION_FILE) != artifact.selection_hash:
        raise BundleValidationError("BOLT deployment selection hash mismatch")
    if hash_file(path / EVIDENCE_FILE) != artifact.evidence_hash:
        raise BundleValidationError("BOLT deployment evidence hash mismatch")
    if hash_json(artifact.identity_material()) != artifact.decision_id:
        raise BundleValidationError("BOLT deployment decision_id mismatch")
    selection = _load_json(path / SELECTION_FILE)
    evidence = _load_json(path / EVIDENCE_FILE)
    if selection.get("status") == "frozen_before_hot_fop_held_out_measurement":
        if evidence.get("status") != "held_out_hot_fop_evaluation_complete":
            raise BundleValidationError("embedded hot-FOP evidence is incomplete")
        if evidence.get("selection", {}).get("sha256") != artifact.selection_hash:
            raise BundleValidationError("embedded hot-FOP evidence does not bind selection")
        if evidence.get("decision") != "retain_stock_bolt_inconclusive_or_below_gate":
            raise BundleValidationError("embedded hot-FOP decision is not stock BOLT")
        if artifact.choice != "stock_bolt" or artifact.layout_mode != "relocatable":
            raise BundleValidationError("invalid hot-FOP fallback deployment choice")
        if evidence.get("workload_id") != artifact.workload_id or not evidence.get(
            "all_outputs_match"
        ):
            raise BundleValidationError("embedded hot-FOP workload evidence is invalid")
        selected = _artifact_for_choice(evidence, artifact.layout_mode, artifact.choice)
        if selected.get("sha256") != artifact.image_hash:
            raise BundleValidationError("deployment image differs from hot-FOP fallback")
        return artifact
    if selection.get("status") != "frozen_before_held_out_measurement":
        raise BundleValidationError("embedded BOLT policy selection was not frozen")
    if evidence.get("status") != "held_out_policy_evaluation_complete":
        raise BundleValidationError("embedded held-out BOLT evidence is incomplete")
    if evidence.get("selection", {}).get("sha256") != artifact.selection_hash:
        raise BundleValidationError("embedded evidence does not bind the selection")
    choice_key = (
        "relocatable_policy"
        if artifact.layout_mode == "relocatable"
        else "fixed_layout_policy"
    )
    evidence_choice_key = (
        "relocatable" if artifact.layout_mode == "relocatable" else "fixed_layout"
    )
    try:
        frozen_choice = str(selection["choices"][artifact.workload_id][choice_key])
        workload = evidence["workloads"][artifact.workload_id]
    except (KeyError, TypeError) as exc:
        raise BundleValidationError(
            f"embedded policy lacks workload {artifact.workload_id!r}: {exc}"
        ) from exc
    if frozen_choice != artifact.choice:
        raise BundleValidationError("deployment choice differs from frozen policy")
    if workload["training_choice"][evidence_choice_key] != artifact.choice:
        raise BundleValidationError("embedded evidence records a different choice")
    if not workload.get("all_outputs_match"):
        raise BundleValidationError("embedded workload failed output validation")
    selected = _artifact_for_choice(evidence, artifact.layout_mode, artifact.choice)
    if selected.get("sha256") != artifact.image_hash:
        raise BundleValidationError("deployment image differs from selected evidence artifact")
    return artifact


def publish_fixed_layout_firmware_candidate(
    deployment_path: Path,
    destination: Path,
    *,
    target: TargetDescription,
) -> FirmwareCandidate:
    """Convert an address-preserving deployment into FirmwareCandidate v1.

    Relocatable BOLT output requires its BAT mapping and is deliberately not
    accepted by this helper.  Fixed-layout reference and compact images retain
    every admitted virtual address, so executable PT_LOAD ranges form exact
    identity origin ranges.
    """

    deployment = load_bolt_deployment(deployment_path)
    if deployment.layout_mode != "fixed_layout" and deployment.choice not in {
        "reference", "compact_fop"
    }:
        raise BundleValidationError(
            "only address-preserving BOLT deployments have identity origin ranges"
        )
    memory: list[MemoryRange] = []
    origins: list[OriginRange] = []
    with deployment.image_path.open("rb") as stream:
        elf = ELFFile(stream)
        machine = str(elf.header["e_machine"])
        endianness = "little" if elf.little_endian else "big"
        expected_machine = {
            "x86_64": "EM_X86_64",
            "aarch64": "EM_AARCH64",
            "riscv64": "EM_RISCV",
        }.get(target.isa)
        if expected_machine != machine or target.endianness != endianness:
            raise BundleValidationError(
                "fixed-layout deployment does not match the declared target"
            )
        for index, segment in enumerate(elf.iter_segments()):
            if segment["p_type"] != "PT_LOAD":
                continue
            start = int(segment["p_vaddr"])
            size = int(segment["p_memsz"])
            if size <= 0:
                continue
            flags = int(segment["p_flags"])
            permissions = "".join((
                "r" if flags & P_FLAGS.PF_R else "-",
                "w" if flags & P_FLAGS.PF_W else "-",
                "x" if flags & P_FLAGS.PF_X else "-",
            ))
            memory.append(MemoryRange(
                f"load-{index}", start, start + size, permissions, "host-elf"
            ))
            file_size = int(segment["p_filesz"])
            if flags & P_FLAGS.PF_X and file_size > 0:
                origins.append(OriginRange(
                    start, start + file_size, start, start + file_size
                ))
        entry = int(elf.header["e_entry"])
    if not origins:
        raise BundleValidationError("fixed-layout deployment has no executable range")
    return publish_firmware_candidate(
        destination,
        image=deployment.image_path,
        original_image_hash=deployment.original_image_hash,
        target=target,
        entry_points=(entry,) if entry else (),
        memory_map=memory,
        origin_ranges=origins,
        optimization_report={
            "status": "accepted",
            "producer": "BOLT deployment policy plus fixed-layout candidate adapter",
            "deployment_decision_id": deployment.decision_id,
            "workload_id": deployment.workload_id,
            "layout_mode": deployment.layout_mode,
            "choice": deployment.choice,
            "selection_hash": deployment.selection_hash,
            "evidence_hash": deployment.evidence_hash,
            "address_preserving": True,
            "patch_contract": "empty_phase_one",
            "limitations": [
                "supported_host_evidence_only",
                "no_original_cortex_m_speedup_claim",
                "no_fastdyn_speedup_claim",
            ],
        },
    )


_BAT_TRANSLATION = re.compile(
    r"^0x([0-9a-fA-F]+) -> (.+) \+ 0x([0-9a-fA-F]+)$"
)
_BOLT_SUFFIX = re.compile(r"(?:/\d+)?(?:\(\*\d+\))?$")
_COLD_SUFFIX = re.compile(r"\.cold\.\d+$")


def _function_bytes(path: Path) -> dict[str, tuple[int, bytes]]:
    records: dict[str, list[tuple[int, bytes]]] = {}
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        segments = [
            item for item in elf.iter_segments()
            if item["p_type"] == "PT_LOAD"
        ]
        for section in elf.iter_sections():
            if section["sh_type"] not in {"SHT_SYMTAB", "SHT_DYNSYM"}:
                continue
            for symbol in section.iter_symbols():
                if str(symbol["st_info"]["type"]) != "STT_FUNC":
                    continue
                address = int(symbol["st_value"])
                size = int(symbol["st_size"])
                if not symbol.name or address <= 0 or size <= 0:
                    continue
                for segment in segments:
                    start = int(segment["p_vaddr"])
                    file_size = int(segment["p_filesz"])
                    if start <= address and address + size <= start + file_size:
                        offset = int(segment["p_offset"]) + address - start
                        stream.seek(offset)
                        records.setdefault(symbol.name, []).append(
                            (address, stream.read(size))
                        )
                        break
    result: dict[str, tuple[int, bytes]] = {}
    for name, candidates in records.items():
        unique = {(address, body) for address, body in candidates}
        if len(unique) == 1:
            result[name] = next(iter(unique))
    return result


def _executable_section_identity(path: Path) -> tuple[tuple[str, int, bytes], ...]:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        return tuple(
            (section.name, int(section["sh_addr"]), section.data())
            for section in elf.iter_sections()
            if int(section["sh_flags"]) & 0x4
        )


def publish_relocatable_firmware_candidate(
    deployment_path: Path,
    destination: Path,
    *,
    target: TargetDescription,
    bat_dump: Path,
    native_report: Path,
    bat_source_image: Path | None = None,
) -> FirmwareCandidate:
    """Publish a relocatable BOLT image with instruction-level BAT origins."""

    deployment = load_bolt_deployment(deployment_path)
    if deployment.layout_mode != "relocatable":
        raise BundleValidationError("BAT publication requires a relocatable deployment")
    if deployment.choice not in {"full_fop", "stock_bolt"}:
        raise BundleValidationError(
            "relocatable reference/compact choices do not require BAT publication"
        )
    bat_dump = bat_dump.resolve()
    native_report = native_report.resolve()
    if not bat_dump.is_file() or not native_report.is_file():
        raise FileNotFoundError(bat_dump if not bat_dump.is_file() else native_report)
    evidence = _load_json(deployment.path / EVIDENCE_FILE)
    reference = Path(str(evidence["artifacts"]["reference"]["path"])).resolve()
    if not reference.is_file() or hash_file(reference) != deployment.original_image_hash:
        raise BundleValidationError("BAT reference image is missing or has the wrong hash")
    bat_source = (
        bat_source_image.resolve()
        if bat_source_image is not None else deployment.image_path
    )
    if not bat_source.is_file():
        raise FileNotFoundError(bat_source)
    if bat_source != deployment.image_path and (
        _executable_section_identity(bat_source)
        != _executable_section_identity(deployment.image_path)
    ):
        raise BundleValidationError(
            "BAT source does not have identical executable sections"
        )
    native = _load_json(native_report)
    if native.get("target") not in {"x86-64", "x86_64"}:
        raise BundleValidationError("native BOLT report target is not x86-64")
    native_functions: dict[str, Mapping[str, Any]] = {}
    for function in native.get("functions", ()):
        name = _BOLT_SUFFIX.sub("", str(function["name"]))
        native_functions[name] = function

    candidate_functions = _function_bytes(deployment.image_path)
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoded: list[tuple[str, capstone.CsInsn]] = []
    for name, (address, body) in candidate_functions.items():
        if address < 0x800000:
            continue
        instructions = list(decoder.disasm(body, address))
        if sum(item.size for item in instructions) != len(body):
            raise BundleValidationError(f"relocated function does not decode exactly: {name}")
        decoded.extend((name, item) for item in instructions)
    if not decoded:
        raise BundleValidationError("relocatable BOLT image has no decoded relocated functions")

    translations: dict[int, tuple[str, int]] = {}
    for start in range(0, len(decoded), 200):
        batch = decoded[start : start + 200]
        command = [str(bat_dump), str(bat_source)] + [
            f"--translate=0x{instruction.address:x}"
            for _name, instruction in batch
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise BundleValidationError(
                f"llvm-bat-dump failed with status {result.returncode}"
            )
        for line in result.stdout.splitlines():
            match = _BAT_TRANSLATION.match(line.strip())
            if match is not None:
                translations[int(match.group(1), 16)] = (
                    match.group(2), int(match.group(3), 16)
                )

    origins: list[OriginRange] = []
    unmapped: list[dict[str, Any]] = []
    mapped_entry = False
    with deployment.image_path.open("rb") as stream:
        elf = ELFFile(stream)
        entry = int(elf.header["e_entry"])
        endianness = "little" if elf.little_endian else "big"
        if str(elf.header["e_machine"]) != "EM_X86_64" or (
            target.isa != "x86_64" or target.endianness != endianness
        ):
            raise BundleValidationError("relocatable image does not match target")
        memory: list[MemoryRange] = []
        for index, segment in enumerate(elf.iter_segments()):
            if segment["p_type"] != "PT_LOAD":
                continue
            address = int(segment["p_vaddr"])
            size = int(segment["p_memsz"])
            if size <= 0:
                continue
            flags = int(segment["p_flags"])
            permissions = "".join((
                "r" if flags & P_FLAGS.PF_R else "-",
                "w" if flags & P_FLAGS.PF_W else "-",
                "x" if flags & P_FLAGS.PF_X else "-",
            ))
            memory.append(MemoryRange(
                f"load-{index}", address, address + size,
                permissions, "host-elf",
            ))
        for section in elf.iter_sections():
            if not int(section["sh_flags"]) & 0x4:  # SHF_EXECINSTR
                continue
            address = int(section["sh_addr"])
            size = int(section["sh_size"])
            if 0 < address < 0x800000 and size > 0:
                origins.append(OriginRange(
                    address, address + size, address, address + size
                ))

    for candidate_name, instruction in decoded:
        translated = translations.get(instruction.address)
        if translated is None:
            unmapped.append({
                "emitted_address": instruction.address,
                "function": candidate_name,
                "reason": "missing_bat_translation",
            })
            continue
        translated_name, original_offset = translated
        normalized = _COLD_SUFFIX.sub("", _BOLT_SUFFIX.sub("", translated_name))
        function = native_functions.get(normalized)
        if function is None:
            unmapped.append({
                "emitted_address": instruction.address,
                "function": candidate_name,
                "translated_name": translated_name,
                "reason": "unknown_original_function",
            })
            continue
        original_size = int(function["original_size"])
        if original_offset < 0 or original_offset >= original_size:
            unmapped.append({
                "emitted_address": instruction.address,
                "function": candidate_name,
                "translated_name": translated_name,
                "original_offset": original_offset,
                "reason": "original_offset_out_of_range",
            })
            continue
        original_address = int(function["original_address"]) + original_offset
        origins.append(OriginRange(
            original_address,
            original_address + 1,
            instruction.address,
            instruction.address + instruction.size,
            "bolt-bat-instruction",
        ))
        if instruction.address <= entry < instruction.address + instruction.size:
            mapped_entry = True
    if unmapped:
        raise BundleValidationError(
            f"BAT origin coverage incomplete for {len(unmapped)} instructions: "
            f"{unmapped[:3]}"
        )
    if not mapped_entry:
        raise BundleValidationError("relocatable entry point has no BAT origin")

    return publish_firmware_candidate(
        destination,
        image=deployment.image_path,
        original_image_hash=deployment.original_image_hash,
        target=target,
        entry_points=(entry,),
        memory_map=memory,
        origin_ranges=origins,
        optimization_report={
            "status": "accepted",
            "producer": "BOLT deployment policy plus BAT origin adapter",
            "deployment_decision_id": deployment.decision_id,
            "workload_id": deployment.workload_id,
            "layout_mode": deployment.layout_mode,
            "choice": deployment.choice,
            "selection_hash": deployment.selection_hash,
            "evidence_hash": deployment.evidence_hash,
            "native_report_sha256": hash_file(native_report),
            "bat_tool_sha256": hash_file(bat_dump),
            "bat_source_image_sha256": hash_file(bat_source),
            "bat_source_is_deployment_image": bat_source == deployment.image_path,
            "bat_instruction_origin_count": len(decoded),
            "identity_executable_range_count": sum(
                item.mapping == "identity" for item in origins
            ),
            "entry_origin_complete": mapped_entry,
            "patch_contract": "empty_phase_one",
        },
    )
