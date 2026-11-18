"""Tests for patch-aware preflight artifacts and identity publication."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fastdyn.preflight import (
    BundleValidationError,
    MemoryRange,
    OriginRange,
    PatchBinding,
    TargetDescription,
    create_identity_bundle,
    load_execution_bundle,
    publish_execution_bundle,
)
from fastdyn.preflight.artifacts import hash_file
from fastdyn.preflight.contracts import import_patch_contracts
from fastdyn.preflight.__main__ import main
from fastdyn.preflight import firmware as firmware_module


def _target() -> TargetDescription:
    return TargetDescription("x86_64", "long", "little", "fixture", ())


def _publish(tmp_path: Path, *, expression: str = "rax <- 1"):
    image = tmp_path / "fixture.bin"
    image.write_bytes(b"\x90\xc3")
    image_hash = hash_file(image)
    binding = PatchBinding(
        binding_id="patch-one",
        original_identity=f"sha256:{image_hash}:cpu0:pc:0x1000",
        original_pc=0x1000,
        current_pc=0x1000,
        isa="x86_64",
        mode="long",
        trigger_phase="instruction_entry",
        kind="modifier",
        action={"expression": expression},
        inputs=("architectural_state",),
        outputs=("architectural_state",),
    )
    return publish_execution_bundle(
        tmp_path / f"bundle-{expression[-1]}",
        image=image,
        original_image_hash=image_hash,
        target=_target(),
        entry_points=(0x1000,),
        memory_map=(MemoryRange("flash", 0x1000, 0x1002, "r-x", "flash"),),
        patch_plan={"actions": [{"expression": expression}]},
        patch_bindings=(binding,),
        origin_ranges=(OriginRange(0x1000, 0x1002, 0x1000, 0x1002),),
        contract_id="test-contract-v1",
        instrumentation={"coverage": False},
        device_model={"name": "none"},
    )


def test_publish_and_load_identity_bundle(tmp_path):
    bundle = _publish(tmp_path)

    assert bundle.image_path.read_bytes() == b"\x90\xc3"
    assert bundle.original_image_hash == bundle.image_hash
    assert load_execution_bundle(bundle.path).bundle_id == bundle.bundle_id
    manifest = json.loads((bundle.path / "execution-bundle.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["patch_bindings_hash"] == bundle.patch_bindings_hash


def test_patch_change_changes_bundle_identity(tmp_path):
    first = _publish(tmp_path, expression="rax <- 1")
    second = _publish(tmp_path, expression="rax <- 2")

    assert first.image_hash == second.image_hash
    assert first.patch_bindings_hash != second.patch_bindings_hash
    assert first.bundle_id != second.bundle_id


def test_tampered_image_is_rejected(tmp_path):
    bundle = _publish(tmp_path)
    bundle.image_path.write_bytes(b"\xcc\xc3")

    with pytest.raises(BundleValidationError, match="image_hash mismatch"):
        load_execution_bundle(bundle.path)


def test_tampered_execution_environment_is_rejected(tmp_path):
    bundle = _publish(tmp_path)
    (bundle.path / "device-model.json").write_text('{"name":"different"}\n')

    with pytest.raises(BundleValidationError, match="device_model_hash mismatch"):
        load_execution_bundle(bundle.path)


def test_binding_must_have_exact_origin_mapping(tmp_path):
    image = tmp_path / "fixture.bin"
    image.write_bytes(b"\x90")
    image_hash = hash_file(image)
    binding = PatchBinding(
        binding_id="patch-one",
        original_identity="original-one",
        original_pc=0x2000,
        current_pc=0x2000,
        isa="x86_64",
        mode="long",
        trigger_phase="instruction_entry",
        kind="modifier",
        action={"expression": "rax <- 1"},
        inputs=(),
        outputs=("rax",),
    )

    with pytest.raises(BundleValidationError, match="exact origin mapping"):
        publish_execution_bundle(
            tmp_path / "bundle",
            image=image,
            original_image_hash=image_hash,
            target=_target(),
            entry_points=(),
            memory_map=(),
            patch_plan={"actions": []},
            patch_bindings=(binding,),
            origin_ranges=(OriginRange(0x1000, 0x1001, 0x1000, 0x1001),),
            contract_id="test-contract-v1",
            instrumentation={},
            device_model={},
        )
    assert not (tmp_path / "bundle").exists()


def test_symbolic_patch_anchor_is_rejected():
    with pytest.raises(BundleValidationError, match="resolved numeric anchor"):
        import_patch_contracts(
            modifiers=("main r0 <- 1",),
            virtuals=(),
            image_hash="0" * 64,
            cpu_index=0,
            target=TargetDescription(
                "arm", "thumb", "little", "cortex-m3", ("armv7-m", "thumb")
            ),
        )


def test_cortex_m_identity_bundle_preserves_patch_anchors(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    image = repository / "tests/sample_binaries/RTOS/RTOSDemo.axf"
    cpu = SimpleNamespace(
        arch="arm",
        cpu="cortex-m3",
        binary=str(image),
        init_nsvtor=0x08000000,
        modifiers=["0x0800397e r0 <- 3"],
        virtuals=["0x080068b0 printreg 0"],
        plugin_config={},
    )
    memory = SimpleNamespace(
        memory_start="0x20000000",
        memory_size="512K",
        memory_type="SRAM",
    )
    opts = SimpleNamespace(coverage=False, edge_coverage=False)
    machine = SimpleNamespace(
        cpus=[cpu],
        memories={"main": memory},
        qemu_target_opts=opts,
        models={},
        parsed_device={},
    )

    bundle = create_identity_bundle(machine, tmp_path / "identity")
    bindings = json.loads((bundle.path / "patch-bindings.json").read_text())["bindings"]

    assert bundle.target.cpu == "cortex-m3"
    assert bundle.original_image_hash == bundle.image_hash
    assert 0x08000000 not in bundle.entry_points
    assert [binding["current_pc"] for binding in bindings] == [0x0800397E, 0x080068B0]
    assert all(binding["placement"] == "exact_anchor" for binding in bindings)
    assert all(binding["clone_policy"] == "forbidden" for binding in bindings)


def test_existing_patch_files_are_frozen_into_identity_bundle(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    image = repository / "tests/sample_binaries/RTOS/RTOSDemo.axf"
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "modifiers.txt").write_text("0x0800397e r0 <- 9\n", encoding="utf-8")
    (existing / "virtuals.txt").write_text("0x080068b0 printreg 0\n", encoding="utf-8")
    cpu = SimpleNamespace(
        arch="arm",
        cpu="cortex-m3",
        binary=str(image),
        init_nsvtor=0x08000000,
        exstng_config_path=str(existing),
        modifiers=[],
        virtuals=[],
        plugin_config={},
    )
    machine = SimpleNamespace(
        cpus=[cpu],
        memories={},
        qemu_target_opts=SimpleNamespace(coverage=False, edge_coverage=False),
        models={},
        parsed_device={},
    )

    bundle = create_identity_bundle(machine, tmp_path / "identity-existing")
    bindings = json.loads((bundle.path / "patch-bindings.json").read_text())["bindings"]

    assert [item["kind"] for item in bindings] == ["modifier", "virtual"]
    assert bindings[0]["action"] == {"expression": "r0 <- 9"}


def test_elf_target_mismatch_is_rejected(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    image = repository / "tests/sample_binaries/RTOS/RTOSDemo.axf"
    cpu = SimpleNamespace(
        arch="x86_64",
        cpu="x86_64",
        binary=str(image),
        init_nsvtor=0,
        modifiers=[],
        virtuals=[],
        plugin_config={},
    )
    machine = SimpleNamespace(
        cpus=[cpu],
        memories={},
        qemu_target_opts=SimpleNamespace(coverage=False, edge_coverage=False),
        models={},
        parsed_device={},
    )

    with pytest.raises(BundleValidationError, match="elf_target_mismatch"):
        create_identity_bundle(machine, tmp_path / "wrong-target")


def test_zero_is_a_valid_cortex_m_vector_base(tmp_path, monkeypatch):
    image = tmp_path / "firmware.elf"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(
        firmware_module,
        "_elf_evidence",
        lambda _image: (
            (OriginRange(0, 0x200, 0, 0x200),),
            (0x100,),
            False,
            "EM_ARM",
            "little",
        ),
    )
    observed_addresses = []

    def read_vector(_image, address, size):
        observed_addresses.append((address, size))
        return (0x20001000).to_bytes(4, "little") + (0x121).to_bytes(4, "little")

    monkeypatch.setattr(firmware_module, "_read_virtual_bytes", read_vector)
    qualification = firmware_module.qualify_firmware(
        image,
        SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0),
    )

    assert observed_addresses == [(0, 8)]
    assert qualification.entry_points == (0x100, 0x120)
    assert "vector_table_not_file_backed_at_declared_base" not in qualification.reason_codes


def test_command_help_does_not_load_runtime_configuration(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])

    assert result.value.code == 0
    assert "ExecutionBundle v1" in capsys.readouterr().out
