"""Automatic firmware-MMIO discovery through the public run-preprocessor SDK."""

from __future__ import annotations

import json

from fastdyn import mmio_plan
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext,
    RunDefinition,
    RunPrepareResult,
    VirtualPreparationError,
    register_run_preprocessor,
)


INCOMPATIBLE_CAPABILITIES = frozenset({
    "coverage", "edge_coverage", "fuzzing", "finline", "twintrace",
    "hardware_trace", "exact_budget",
})


class MmioOptRunPreprocessor:
    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        if ctx.cpu_count != 1:
            raise VirtualPreparationError(
                "automatic MMIO optimization currently requires one serialized CPU"
            )
        if ctx.architecture.lower() != "arm" or ctx.machine.lower() not in (
            "cortexm", "cortexm7"
        ):
            raise VirtualPreparationError(
                "automatic MMIO optimization currently supports Cortex-M Thumb only"
            )

        plan = ctx.plugin_artifact_path("pending-plan.txt")
        discovery = ctx.plugin_artifact_path("discovery.json")
        installation = ctx.plugin_artifact_path("installation.json")
        incompatible = sorted(ctx.capabilities & INCOMPATIBLE_CAPABILITIES)
        if incompatible:
            plan.unlink(missing_ok=True)
            installation.write_text(json.dumps({
                "enabled": False,
                "prepared": 0,
                "installed": 0,
                "disabled_reason": "incompatible runtime capabilities: "
                                   + ", ".join(incompatible),
                "sites": [],
            }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            ctx.logger.warning("automatic MMIO substitution disabled with %s",
                               ", ".join(incompatible))
            return RunPrepareResult(artifacts=[installation])

        if not ctx.device_ranges:
            raise VirtualPreparationError(
                "automatic MMIO optimization needs configured device ranges"
            )
        mmio_plan.write_plan(
            plan, discovery, ctx.binary, ctx.device_ranges, analysis=ctx.analysis,
        )
        installation.write_text(json.dumps({
            "enabled": True,
            "status": "pending device initialization",
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        ctx.logger.info("discovered automatic MMIO candidates in %s", ctx.binary)
        return RunPrepareResult(artifacts=[plan, discovery, installation])


register_run_preprocessor(RunDefinition(
    name="mmio_opt",
    prepare=MmioOptRunPreprocessor(),
    enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
    help=ConfigurationHelp(
        "Derive pure elder-model reads and substitute matching Cortex-M loads.",
        "[CPU.cpu0.plugins.mmio_opt]\nenabled = true",
        "docs/AutomaticMMIOOptimization.md",
    ),
))
