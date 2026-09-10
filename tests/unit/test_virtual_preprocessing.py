from pathlib import Path
import shutil

import pytest

from fastdyn.machine import VirtualInstruction
from fastdyn.fastdyn import Machine
from fastdyn import toml_parser
from fastdyn.targets.qemu_target import build_qemu_cmd
from fastdyn.virtual_preprocessing import (
    RUN_PREPROCESSORS,
    RunDefinition,
    RunPrepareResult,
    VIRTUAL_DEFINITIONS,
    VirtualDefinition,
    VirtualPreparationError,
    VirtualRule,
    prepare_run_preprocessors,
    prepare_virtual_rules,
)


class _MachineContext:
    def __init__(self, irq_map=None):
        self.irq_map = irq_map or {}


class _Cpu:
    binary = "firmware.elf"
    arch = "arm"
    machine = "cortexm"
    cpu = "cortex-m4"
    symbol_dict = {"main": 0x08000101}

    def __init__(self, *, plugin_enabled=False, plugin_config=None, irq_map=None):
        self.plugin_config = (
            plugin_config
            if plugin_config is not None
            else ({"introspection": {"enabled": True}} if plugin_enabled else {})
        )
        self.machine_obj = _MachineContext(irq_map)


class _Machine:
    def __init__(self, cpu):
        self.cpus = [cpu]


def test_irq_preprocessor_converts_symbolic_cortexm_irq_to_exception_vector(tmp_path):
    cpu = _Cpu(irq_map={"TIM1_BRK_TIM9": 24})

    rules = prepare_virtual_rules(
        cpu,
        tmp_path,
        [VirtualRule(VirtualInstruction("main+4", "raiseirq", ["TIM1_BRK_TIM9"]))],
    )

    assert rules == ["0x8000104 raiseirq 40"]


def test_legacy_raise_irq_alias_is_normalized(tmp_path):
    cpu = _Cpu()

    rules = prepare_virtual_rules(
        cpu,
        tmp_path,
        [VirtualRule(VirtualInstruction("0x8000100", "raise_irq", ["44"]))],
    )

    assert rules == ["0x8000100 raiseirq 44"]


def test_generic_virtual_arguments_still_receive_symbol_resolution(tmp_path):
    cpu = _Cpu()

    rules = prepare_virtual_rules(
        cpu,
        tmp_path,
        [VirtualRule(VirtualInstruction("0x8000100", "debug_log", ["main"]))],
    )

    assert rules == ["0x8000100 debug_log 0x8000101"]


def test_conflicting_virtuals_at_one_pc_are_rejected(tmp_path):
    cpu = _Cpu()

    with pytest.raises(VirtualPreparationError, match="conflicting virtuals"):
        prepare_virtual_rules(
            cpu,
            tmp_path,
            [
                VirtualRule(VirtualInstruction("0x8000100", "debug_log", ["one"])),
                VirtualRule(
                    VirtualInstruction("0x8000100", "benchmark_start", []),
                    origin="introspection",
                ),
            ],
        )


def test_virtual_definition_requires_declared_capability(monkeypatch, tmp_path):
    monkeypatch.setitem(
        VIRTUAL_DEFINITIONS,
        "needs_fmu",
        VirtualDefinition(name="needs_fmu", requires=frozenset({"fmu"})),
    )

    with pytest.raises(VirtualPreparationError, match="requires unavailable capabilities: fmu"):
        prepare_virtual_rules(
            _Cpu(),
            tmp_path,
            [VirtualRule(VirtualInstruction("0x8000100", "needs_fmu", []))],
        )


def test_run_preprocessor_results_remain_declarative(monkeypatch, tmp_path):
    class FakeIntrospection:
        def prepare(self, ctx):
            artifact = ctx.artifact_path("fake/schema.txt")
            artifact.write_text("schema", encoding="utf-8")
            return RunPrepareResult(
                virtuals=[VirtualInstruction("0x8000100", "debug_log", ["ready"])],
                artifacts=[artifact],
            )

    monkeypatch.setitem(
        RUN_PREPROCESSORS,
        "introspection",
        RunDefinition(
            name="introspection",
            prepare=FakeIntrospection(),
            enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        ),
    )
    cpu = _Cpu(plugin_enabled=True)
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    generated = machine.generated_virtual_rules[id(cpu)]
    assert generated[0].origin == "introspection"
    assert generated[0].virtual.instruction == "debug_log"
    assert not hasattr(machine, "runtime_plugin_args")
    assert machine.preprocessing_prepared is True
    assert machine.preprocessing_artifacts == [
        tmp_path.resolve() / "run-artifacts" / "fake" / "schema.txt"
    ]


def test_run_preprocessor_cannot_return_plugin_command_line_arguments():
    with pytest.raises(TypeError, match="plugin_args"):
        RunPrepareResult(plugin_args={"untrusted": "value"})


def test_function_counter_plugin_generates_one_entry_virtual_per_elf_function(tmp_path):
    cpu = _Cpu(plugin_config={"function_counter": {"enabled": True}})
    cpu.binary = str(Path("tests/binaries/rtos/zephyr.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    generated = machine.generated_virtual_rules[id(cpu)]
    rules = [rule.virtual for rule in generated if rule.origin == "function_counter"]
    assert "function_counter" in VIRTUAL_DEFINITIONS
    assert len(rules) > 100
    assert all(rule.instruction == "function_counter" for rule in rules)
    assert len({rule.at for rule in rules}) == len(rules)
    manifest = tmp_path / "run-artifacts" / "function_counter" / "functions.tsv"
    assert "z_arm_pendsv" in manifest.read_text(encoding="utf-8")


def test_function_counter_reports_unmatched_function_filters_with_examples(tmp_path):
    cpu = _Cpu(plugin_config={"function_counter": {"enabled": True, "include": ["main"]}})
    cpu.binary = str(
        Path("tests/binaries/freertos_stm32f429i_discovery/freertos_stm32f429i_discovery.elf").resolve()
    )
    machine = _Machine(cpu)

    with pytest.raises(VirtualPreparationError) as error:
        prepare_run_preprocessors(machine, tmp_path)

    message = str(error.value)
    assert "matched no executable functions" in message
    assert "include=['main']" in message
    assert "Reset_Handler" in message
    assert "remove include" in message


def test_function_tracer_generates_dwarf_argument_schema_for_structures(tmp_path):
    cpu = _Cpu(
        plugin_config={
            "function_tracer": {
                "enabled": True,
                "include": ["z_impl_k_sleep_ticks"],
                "max_functions": 8,
                "max_events": 10,
            }
        }
    )
    cpu.binary = str(Path("tests/binaries/rtos/zephyr.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    generated = machine.generated_virtual_rules[id(cpu)]
    rules = [rule.virtual for rule in generated if rule.origin == "function_tracer"]
    assert "function_tracer" in VIRTUAL_DEFINITIONS
    assert len(rules) == 1
    assert rules[0].instruction == "function_tracer"
    assert rules[0].args == ["z_impl_k_sleep_ticks"]
    arguments = (
        tmp_path / "run-artifacts" / "function_tracer" / "arguments.tsv"
    ).read_text(encoding="utf-8")
    assert "timeout\t" in arguments
    assert "\tstruct\t" in arguments
    assert "ticks|0|int|" in arguments


def test_object_sanitizer_resolves_a_static_dwarf_object(tmp_path):
    cpu = _Cpu(plugin_config={"object_sanitizer": {"enabled": True, "object": "thread_a_sem"}})
    cpu.binary = str(Path("tests/binaries/rtos/zephyr.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    manifest = (tmp_path / "run-artifacts" / "object_sanitizer" / "objects.tsv").read_text()
    assert "thread_a_sem" in manifest
    assert "0x2000003c" in manifest
    assert "\t16\tstatic\tLIVE\t" in manifest


def test_object_sanitizer_resolves_static_objects_by_dwarf_type(tmp_path):
    cpu = _Cpu(plugin_config={"object_sanitizer": {"enabled": True, "type": "struct k_sem"}})
    cpu.binary = str(Path("tests/binaries/rtos/zephyr.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    manifest = (tmp_path / "run-artifacts" / "object_sanitizer" / "objects.tsv").read_text()
    assert "thread_a_sem" in manifest


def test_object_sanitizer_normalizes_a_selected_custom_allocation_site(tmp_path):
    cpu = _Cpu(plugin_config={"object_sanitizer": {
        "enabled": True,
        "function": "Reset_Handler",
        "allocation": 1,
        "allocators": [{
            "name": "fixture_pool", "allocate": "pool_alloc", "size_arg": 0,
            "free": "pool_free", "pointer_arg": 0,
        }],
    }})
    cpu.binary = str(Path("tests/binaries/object_sanitizer/dynamic_oob.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    generated = [rule.virtual for rule in machine.generated_virtual_rules[id(cpu)]]
    assert [item.instruction for item in generated].count("object_sanitizer_alloc_call") == 1
    assert [item.instruction for item in generated].count("object_sanitizer_alloc_return") == 1
    assert [item.instruction for item in generated].count("object_sanitizer_free") == 1
    sites = (tmp_path / "run-artifacts" / "object_sanitizer" / "allocation_sites.tsv").read_text()
    assert "fixture_pool" in sites
    assert "Reset_Handler" in sites


def test_variable_watch_resolves_a_dwarf_structure_field(tmp_path):
    cpu = _Cpu(plugin_config={"variable_watch": {
        "enabled": True,
        "variable": "motor_state.temperature",
        "access": "write",
        "changes_only": True,
    }})
    cpu.binary = str(Path("tests/binaries/variable_watch/variable_watch.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    watch = (tmp_path / "run-artifacts" / "variable_watch" / "watch.tsv").read_text()
    assert "motor_state.temperature\t0x20000008\t4" in watch
    candidates = (tmp_path / "run-artifacts" / "variable_watch" / "candidate_accesses.tsv").read_text()
    assert "set_temperature" in candidates


def test_variable_watch_accepts_a_raw_address_without_dwarf_resolution(tmp_path):
    cpu = _Cpu(plugin_config={"variable_watch": {
        "enabled": True, "address": "0x20000008", "size": 4, "access": "read_write",
    }})
    cpu.binary = str(Path("tests/binaries/variable_watch/variable_watch.elf").resolve())
    machine = _Machine(cpu)

    prepare_run_preprocessors(machine, tmp_path)

    watch = (tmp_path / "run-artifacts" / "variable_watch" / "watch.tsv").read_text()
    assert "0x20000008\t4\traw" in watch


def test_toml_plugin_settings_are_passed_without_frontend_feature_dispatch(tmp_path):
    config = tmp_path / "plugin-settings.toml"
    config.write_text(
        """
[Machine]
platform = "generic-cortexm"

[Device.Models.unhandled]

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "1M"
memory_type = "SRAM"
backend = "file"
memory_file = "/tmp/fastdyn-plugin-settings.ram"
share = true

[CPU]
[[CPU.cpu0]]
binary = "firmware.elf"

[CPU.cpu0.plugins.example]
enabled = true
setting = "from-toml"
""",
        encoding="utf-8",
    )

    handle = toml_parser.parser(
        str(tmp_path / "work"), "machine0", str(config),
        "third_party/common/cmsis-svd-data", load_fmu=False,
    )
    cpu = handle.machines["machine0"].cpus[0]

    assert cpu.plugin_config == {
        "example": {"enabled": True, "setting": "from-toml"}
    }


def test_native_plugins_use_the_runtime_sdk_not_plugin_arguments_or_core_dispatch():
    source = Path("core/core.c").read_text(encoding="utf-8")
    runtime_sdk = Path("virtuals/runtime_sdk.c").read_text(encoding="utf-8")
    introspection = Path("virtuals/introspection/runtime/inspct.c").read_text(encoding="utf-8")
    activity = Path("virtuals/introspection/runtime/activity.c").read_text(encoding="utf-8")
    counter = Path("virtuals/function_counter/runtime/function_counter.c").read_text(encoding="utf-8")

    assert 'utils_get_arg("introspection"' not in source
    assert 'utils_get_arg("introspection_schema"' not in source
    assert 'utils_get_arg("introspection_activity_log"' not in activity
    assert "introspection/schema.txt" not in source
    assert "virtual_initialize_plugins" in runtime_sdk
    assert 'VIRTUAL_PLUGIN("introspection"' in introspection
    assert 'VIRTUAL_PLUGIN("function_counter"' in counter
    assert "virtual_artifact_path" in activity
    assert "core_get_run_artifact_path" not in counter
    assert "virtual_register(" not in counter
    assert "core_read_ram" not in runtime_sdk
    assert "core_write_ram" not in runtime_sdk


def test_runtime_sdk_keeps_guest_state_and_instrumentation_operations_public():
    header = Path("include/fastdyn_runtime.h").read_text(encoding="utf-8")
    implementation = Path("virtuals/runtime_sdk.c").read_text(encoding="utf-8")

    for operation in (
        "virtual_read_memory",
        "virtual_write_memory",
        "virtual_read_register",
        "virtual_write_register",
        "virtual_read_register_bytes",
        "virtual_write_register_bytes",
        "virtual_pc",
        "virtual_sp",
        "virtual_icount",
        "virtual_guest_time_ns",
        "virtual_raise_irq",
        "virtual_register_irq_hook",
        "virtual_register_tb_translation_hook",
        "virtual_register_rule",
        "virtual_register_update",
        "virtual_register_gated_modifier",
        "virtual_log",
    ):
        assert operation in header
        assert operation in implementation

    for header in (
        "include/fastdyn/arch/arm32.h",
        "include/fastdyn/arch/arm_v7m.h",
        "include/fastdyn/arch/aarch64.h",
        "include/fastdyn/arch/riscv64.h",
        "include/fastdyn/arch/x86_64.h",
    ):
        assert Path(header).is_file()

    assert "VIRTUAL_FIRST_ARG" in Path("include/fastdyn/arch/arm32.h").read_text()
    assert "VIRTUAL_FIRST_ARG VIRTUAL_AARCH64_X0" in Path(
        "include/fastdyn/arch/aarch64.h"
    ).read_text()
    assert "VIRTUAL_FIRST_ARG VIRTUAL_RISCV64_A0" in Path(
        "include/fastdyn/arch/riscv64.h"
    ).read_text()
    assert "VIRTUAL_FIRST_ARG VIRTUAL_X86_64_RDI" in Path(
        "include/fastdyn/arch/x86_64.h"
    ).read_text()


def test_qemu_serialization_uses_the_shared_virtual_pipeline(tmp_path):
    plugin = tmp_path / "libfastdyn.so"
    plugin.touch()
    firmware = Path("tests/sample_binaries/RTOS/RTOSDemo.axf").resolve()

    machine = Machine("machine0", "STM32F429")
    machine.irq_map = {"TIM1_BRK_TIM9": 24}
    machine.add_memory(
        "main", "ram0", "0x20000000", "1K", "SRAM", "file",
        str(tmp_path / "ram.bin"), True,
    )
    cpu = machine.add_cpu(
        "arm", "cortexm", "cortex-m4", str(firmware), None,
        "None", None, False,
    )
    cpu.plugin_library = str(plugin)
    cpu.add_virtual_instruction(
        VirtualInstruction("0x08000100", "raiseirq", ["TIM1_BRK_TIM9"])
    )
    machine.qemu_target_opts.qemu_path = shutil.which("true")
    machine.qemu_target_opts.log_options = "none"

    build_qemu_cmd(machine, str(tmp_path / "dev_config.json"), str(tmp_path))

    assert (tmp_path / "virtuals" / "virtuals.txt").read_text(encoding="utf-8") == (
        "0x8000100 raiseirq 40\n"
    )


def test_qemu_omits_ram_base_global_for_toml_zero_address(tmp_path):
    plugin = tmp_path / "libfastdyn.so"
    plugin.touch()
    firmware = Path("tests/sample_binaries/RTOS/RTOSDemo.axf").resolve()

    machine = Machine("machine0", "generic-armv7a")
    machine.add_memory(
        "main", "ram0", "0x0", "16M", "SRAM", "file",
        str(tmp_path / "ram.bin"), True,
    )
    cpu = machine.add_cpu(
        "arm", "virt", "cortex-a7", str(firmware), None,
        "None", None, False,
    )
    cpu.plugin_library = str(plugin)
    machine.qemu_target_opts.qemu_path = shutil.which("true")
    machine.qemu_target_opts.log_options = "none"

    qemu_cmd, *_ = build_qemu_cmd(
        machine, str(tmp_path / "dev_config.json"), str(tmp_path)
    )

    assert "virt-soc.ram_baseaddr0=0x0" not in qemu_cmd
