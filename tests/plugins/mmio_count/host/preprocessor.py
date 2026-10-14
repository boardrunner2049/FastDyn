"""Host half of the out-of-tree test plugin: validate settings, hand them to
the runtime half as an artifact."""

from __future__ import annotations

from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext,
    RunDefinition,
    RunPrepareResult,
    VirtualPreparationError,
    register_run_preprocessor,
)


class MmioCountPreprocessor:
    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        label = ctx.settings.get("label", "run")
        if not isinstance(label, str) or not label.isidentifier():
            raise VirtualPreparationError("[CPU.cpu0.plugins.mmio_count].label must be an identifier")
        settings = ctx.plugin_artifact_path("settings.txt")
        settings.write_text(f"{label}\n", encoding="utf-8")
        return RunPrepareResult(artifacts=[settings])


register_run_preprocessor(
    RunDefinition(
        name="mmio_count",
        prepare=MmioCountPreprocessor(),
        enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        help=ConfigurationHelp(
            "Test plugin: count MMIO accesses and translated blocks.",
            '[CPU.cpu0.plugins.mmio_count]\nenabled = true\nsource = "tests/plugins/mmio_count"',
            "docs/WritingVirtuals.md",
        ),
    )
)
