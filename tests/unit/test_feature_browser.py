"""Tests for virtual/plugin configuration discovery."""

from click.testing import CliRunner

from fastdyn import feature_browser, main, platform_browser, virtual_preprocessing


def test_feature_browser_exposes_registered_run_plugins_and_toml_examples():
    plugins = {entry.name: entry for entry in feature_browser.plugin_entries()}

    assert {"function_counter", "function_tracer", "introspection", "object_sanitizer", "variable_watch"} <= set(plugins)
    assert "[CPU.cpu0.plugins.variable_watch]" in plugins["variable_watch"].toml
    assert "[CPU.cpu0.plugins.introspection]" in plugins["introspection"].toml


def test_feature_browser_renders_only_sdk_declared_metadata():
    virtuals = {entry.name: entry for entry in feature_browser.virtual_entries()}
    plugins = {entry.name: entry for entry in feature_browser.plugin_entries()}

    assert virtuals["raiseirq"].description == (
        virtual_preprocessing.VIRTUAL_DEFINITIONS["raiseirq"].help.description
    )
    assert plugins["variable_watch"].toml == (
        virtual_preprocessing.RUN_PREPROCESSORS["variable_watch"].help.toml
    )
    assert "object_sanitizer_alloc_call" not in virtuals


def test_feature_browser_builds_virtual_and_plugin_branches():
    root = feature_browser.build_feature_browser()

    assert root.title == "FastDyn virtuals and plugins"
    virtual_menu = root.advance(root.choices[0][1])
    raiseirq = next(value for label, value in virtual_menu.choices if label.startswith("raiseirq"))
    assert virtual_menu.advance(raiseirq).toml.startswith("[[CPU.cpu0.virtuals]]")

    plugin_menu = root.advance(root.choices[1][1])
    watch = next(value for label, value in plugin_menu.choices if label.startswith("variable_watch"))
    assert "variable =" in plugin_menu.advance(watch).toml

    documentation_menu = root.advance(root.choices[-1][1])
    documentation = {
        documentation_menu.advance(value).path
        for _, value in documentation_menu.choices
    }
    assert "docs/VirtualsAndModifiers.md" in documentation
    assert "docs/VariableWatch.md" in documentation


def test_modifier_browser_exposes_modifier_forms():
    entries = {entry.name: entry for entry in feature_browser.modifier_entries()}

    assert "register assignment" in entries
    assert 'patch = "r0 <- 1"' in entries["register assignment"].toml
    assert "riscv_pc" in entries["RISC-V control-flow redirect"].toml


def test_device_model_browser_exposes_handler_fragments():
    entries = {entry.name: entry for entry in feature_browser.device_model_entries()}

    assert "classic" in entries
    assert 'model = "classic"' in entries["classic"].toml
    assert "backend = \"stlink\"" in entries["passthrough"].toml


def test_essential_run_configuration_browsers_expose_toml_fragments():
    memory = {entry.name: entry for entry in feature_browser.memory_entries()}
    firmware = {entry.name: entry for entry in feature_browser.firmware_entries()}
    machine = {entry.name: entry for entry in feature_browser.machine_entries()}
    run = {entry.name: entry for entry in feature_browser.run_entries()}

    assert "[Memory.main]" in memory["primary file-backed RAM"].toml
    assert 'binary = "build/firmware.elf"' in firmware["ELF firmware"].toml
    assert "monitor_port" not in firmware["start under GDB"].toml
    assert "existing [Machine] table" in firmware["start under GDB"].toml
    assert '# qemu_path = "path/to/qemu-system-arm"  # optional' in machine["headless QEMU"].toml
    assert "fastdyn run -c configs/target.toml" in run["run a configuration"].toml


def test_virtuals_command_prints_catalog_and_selected_plugin(monkeypatch):
    plain = CliRunner().invoke(main.cli, ["help", "virtuals", "--no-browse"])

    assert plain.exit_code == 0
    assert "Virtual instructions:" in plain.output
    assert "Run-wide plugins:" in plain.output
    assert "variable_watch" in plain.output

    selected = feature_browser.FeatureEntry(
        "Run-wide plugin", "variable_watch", "Watch a source variable.",
        '[CPU.cpu0.plugins.variable_watch]\nenabled = true', "docs/VariableWatch.md",
    )
    monkeypatch.setattr(feature_browser, "browse_features", lambda: selected)
    selected_result = CliRunner().invoke(main.cli, ["help", "virtuals", "--browse"])

    assert selected_result.exit_code == 0
    assert "Selected run-wide plugin: variable_watch" in selected_result.output
    assert "[CPU.cpu0.plugins.variable_watch]" in selected_result.output


def test_help_group_exposes_discovery_subcommands():
    result = CliRunner().invoke(main.cli, ["help"])
    modifiers = CliRunner().invoke(main.cli, ["help", "modifiers", "--no-browse"])
    models = CliRunner().invoke(main.cli, ["help", "device-models", "--no-browse"])
    memory = CliRunner().invoke(main.cli, ["help", "memory", "--no-browse"])
    firmware = CliRunner().invoke(main.cli, ["help", "firmware", "--no-browse"])
    run = CliRunner().invoke(main.cli, ["help", "run", "--no-browse"])
    platform_menu = platform_browser.build_platform_browser([
        ("Vendor", "Device", "/catalog/Vendor/Device.svd"),
    ])

    assert result.exit_code == 0
    assert "platforms" in result.output
    assert "virtuals" in result.output
    assert "modifiers" in result.output
    assert "device-models" in result.output
    assert "machine" in result.output
    assert "memory" in result.output
    assert "firmware" in result.output
    assert "run" in result.output
    assert modifiers.exit_code == 0
    assert "register-indirect assignment" in modifiers.output
    assert models.exit_code == 0
    assert "passthrough" in models.output
    assert memory.exit_code == 0
    assert "primary file-backed RAM" in memory.output
    assert firmware.exit_code == 0
    assert "ELF firmware" in firmware.output
    assert run.exit_code == 0
    assert "fastdyn run -c configs/target.toml" in run.output
    platform_docs = platform_menu.advance(platform_menu.choices[-1][1])
    assert platform_docs.advance(platform_docs.choices[0][1]).path == "docs/PlatformBrowser.md"
