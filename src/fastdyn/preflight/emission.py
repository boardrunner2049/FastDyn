"""Reference-safe compact in-place emission for qualified machine regions."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .model import BundleValidationError, hash_file
from .region import FirmwareUnitView
from .sdk import Decision, PassDescriptor, ReplacementEdit


class EmitInPlace:
    """Emit one checked replacement without retaining a second live body."""

    @staticmethod
    def _padding(unit: FirmwareUnitView, size: int) -> bytes:
        if size < 0:
            raise BundleValidationError("replacement exceeds qualified capacity")
        if unit.target.isa == "x86_64" and unit.target.mode == "long":
            return b"\x90" * size
        if unit.target.isa == "arm" and unit.target.mode == "thumb":
            if size % 2:
                raise BundleValidationError("Thumb padding must be halfword aligned")
            return b"\x00\xbf" * (size // 2)
        raise BundleValidationError("emit-in-place target is not qualified")

    def _selection(self, unit: FirmwareUnitView, edit: ReplacementEdit):
        positions = {item.identity: index for index, item in enumerate(unit.instructions)}
        indexes = [positions[item] for item in edit.selected_identities]
        if indexes != list(range(min(indexes), max(indexes) + 1)):
            raise BundleValidationError("selected instructions are not contiguous")
        selected = tuple(unit.instructions[index] for index in indexes)
        if tuple(item.identity for item in selected) != edit.selected_identities:
            raise BundleValidationError("selected instruction order is not canonical")
        return selected

    def validate(
        self,
        unit: FirmwareUnitView,
        edit: ReplacementEdit,
        descriptor: PassDescriptor,
    ) -> Decision:
        if "replace_contiguous" not in descriptor.allowed_edits:
            return Decision("unsupported", "edit_kind_not_allowed")
        if not (
            (unit.target.isa == "x86_64" and unit.target.mode == "long")
            or (unit.target.isa == "arm" and unit.target.mode == "thumb")
        ):
            return Decision("unsupported", "emit_in_place_target_not_qualified")
        try:
            selected = self._selection(unit, edit)
        except BundleValidationError as exc:
            return Decision("emission_failure", "noncontiguous_selection", {"message": str(exc)})
        selected_ids = {item.identity for item in selected}
        protected = [
            item.constraint_id for item in unit.protected_entries
            if item.instruction_identity in selected_ids
        ]
        if protected:
            return Decision(
                "emission_failure", "protected_entry_selected", {"constraints": protected}
            )
        selection_start = selected[0].address
        selection_end = selected[-1].address + selected[-1].size
        # Replacing from a declared entry is legal: the replacement keeps that
        # exact address.  An entry strictly inside the selected range is an
        # alternate entry and cannot be overwritten by this bounded emitter.
        interior_entries = [
            entry for entry in unit.entries if selection_start < entry < selection_end
        ]
        if interior_entries:
            return Decision(
                "emission_failure",
                "entry_inside_replacement",
                {"entries": interior_entries},
            )
        inbound = [
            item.reference_id for item in unit.references
            if item.target_identity in selected_ids
            and item.source_identity not in selected_ids
        ]
        if inbound:
            return Decision(
                "emission_failure",
                "inbound_reference_to_replaced_instruction",
                {"references": inbound},
            )
        internal = [
            item for item in unit.references
            if item.source_identity in selected_ids and item.target_identity in selected_ids
        ]
        replacement_origins = {
            item.original_identity for item in edit.replacement
        }
        if internal and not (
            edit.semantic_evidence.get("layout_analysis", {}).get(
                "internal_reference_repair_complete"
            ) is True
            and all(
                unit.instruction(item.target_identity).original_identity
                in replacement_origins
                for item in internal
                if item.target_identity is not None
            )
        ):
            return Decision(
                "emission_failure", "internal_reference_repair_not_proved",
                {"references": [item.reference_id for item in internal]},
            )
        if any(item.status != "resolved" for item in unit.references):
            return Decision(
                "unsupported",
                "unresolved_reference_keeps_region_unchanged",
                {
                    "references": [
                        item.reference_id for item in unit.references if item.status != "resolved"
                    ]
                },
            )
        capacity = sum(item.size for item in selected)
        replacement_size = len(bytes.fromhex(edit.replacement_bytes))
        growth = replacement_size - capacity
        if replacement_size > capacity:
            return Decision(
                "emission_failure",
                "replacement_does_not_fit_qualified_range",
                {"capacity": capacity, "replacement_bytes": replacement_size},
            )
        try:
            padding = self._padding(unit, capacity - replacement_size)
        except BundleValidationError as exc:
            return Decision(
                "emission_failure",
                "target_padding_not_encodable",
                {"message": str(exc)},
            )
        selected_original = {item.original_identity for item in selected}
        for anchor in unit.patch_contract.anchors:
            if anchor.instruction_identity in selected_ids and not any(
                origin.original_identity == anchor.original_identity for origin in edit.origins
            ):
                return Decision(
                    "emission_failure", "patch_anchor_missing_exact_origin", {"anchor": anchor.anchor_id}
                )
        bad_origins = [
            item.original_identity for item in edit.origins
            if item.original_identity not in selected_original
        ]
        if bad_origins:
            return Decision(
                "emission_failure", "origin_mapping_outside_selection", {"origins": bad_origins}
            )
        return Decision(
            "accepted",
            "placement_and_reference_checks_passed",
            {
                "capacity_bytes": capacity,
                "replacement_bytes": replacement_size,
                "padding_bytes": len(padding),
                "padding_encoding": padding.hex(),
                "growth_bytes": growth,
                "protected_entries_preserved": len(unit.protected_entries),
                "references_preserved": len(unit.references),
            },
        )

    def emit(
        self,
        image: Path,
        output: Path,
        unit: FirmwareUnitView,
        edit: ReplacementEdit,
    ) -> dict[str, Any]:
        image = image.resolve()
        output = output.resolve()
        if hash_file(image) != unit.input_image_hash:
            raise BundleValidationError("input image hash changed before emission")
        if output.exists():
            raise FileExistsError(output)
        selected = self._selection(unit, edit)
        start = selected[0].file_offset
        capacity = sum(item.size for item in selected)
        payload = bytes.fromhex(edit.replacement_bytes)
        original = image.read_bytes()
        if start < 0 or start + capacity > len(original):
            raise BundleValidationError("qualified replacement range is outside the image")
        selected_bytes = original[start : start + capacity]
        expected = b"".join(bytes.fromhex(item.encoding) for item in selected)
        if selected_bytes != expected:
            raise BundleValidationError("input bytes no longer match the immutable region")
        padding = self._padding(unit, capacity - len(payload))
        emitted = original[:start] + payload + padding + original[start + capacity :]
        output.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(image.stat().st_mode)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output.name}.", dir=output.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(emitted)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(mode)
            os.replace(temporary, output)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        output_hash = hash_file(output)
        return {
            "input_image_hash": unit.input_image_hash,
            "output_image_hash": output_hash,
            "proposal_hash": edit.proposal_hash,
            "file_offset": start,
            "qualified_capacity_bytes": capacity,
            "retained_original_bytes_in_range": 0,
            "live_replacement_bytes": len(payload),
            "padding_bytes": len(padding),
            "repair_bytes": 0,
            "mapped_executable_span_bytes": unit.file_end - unit.file_start,
            "occupied_image_bytes": len(emitted),
            "origin_mappings": [asdict(item) for item in edit.origins],
            "patch_contract_hash": unit.patch_contract.contract_hash,
            "output_mode": oct(mode),
            "output_sha256_recheck": hashlib.sha256(output.read_bytes()).hexdigest(),
        }
