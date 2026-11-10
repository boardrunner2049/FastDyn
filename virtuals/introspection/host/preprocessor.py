"""RTOS introspection's host-side preparation, colocated with its C runtime."""

from __future__ import annotations

from typing import Callable

from .activity_monitor import start_activity_monitor
from .introspect import introspect_rtos
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext,
    RunDefinition,
    RunPrepareResult,
    VIRTUAL_DEFINITIONS,
    VirtualDefinition,
    VirtualPreparationError,
    register_run_preprocessor,
    register_virtual,
)


class IntrospectionRunPreprocessor:
    """Prepare RTOS instrumentation and optional module-owned activity UI."""

    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        plan = introspect_rtos(
            ctx.binary,
            architecture=ctx.architecture,
            machine=ctx.machine,
            cpu=ctx.cpu,
            analysis=ctx.analysis,
        )
        schema_path = ctx.plugin_artifact_path("schema.txt")
        schema_path.write_text(plan.schema, encoding="utf-8")
        activity_path = ctx.plugin_artifact_path("activity.jsonl")
        activity_path.touch()
        cleanup: list[Callable[[], None]] = []

        monitor_config = ctx.settings.get("activity_monitor", False)
        if isinstance(monitor_config, dict):
            monitor_enabled = bool(monitor_config.get("enabled", True))
            host = str(monitor_config.get("host", "127.0.0.1"))
            port = int(monitor_config.get("port", 8765))
            open_browser = bool(monitor_config.get("open_browser", False))
        elif isinstance(monitor_config, bool):
            monitor_enabled = monitor_config
            host, port, open_browser = "127.0.0.1", 8765, False
        else:
            raise VirtualPreparationError(
                "[CPU.cpu0.plugins.introspection].activity_monitor must be a boolean or table"
            )
        if monitor_enabled:
            try:
                server, _thread, url = start_activity_monitor(
                    ctx.workdir, host=host, port=port, open_browser=open_browser
                )
            except OSError as exc:
                raise VirtualPreparationError(
                    f"introspection activity monitor could not bind {host}:{port}: {exc}"
                ) from exc
            ctx.logger.info("Activity monitor available at %s", url)
            cleanup.append(lambda: (server.shutdown(), server.server_close()))

        for virtual in plan.virtuals:
            if virtual.instruction not in VIRTUAL_DEFINITIONS:
                register_virtual(VirtualDefinition(name=virtual.instruction))
        return RunPrepareResult(
            virtuals=plan.virtuals,
            artifacts=[schema_path, activity_path],
            cleanup=cleanup,
        )


register_run_preprocessor(
    RunDefinition(
        name="introspection",
        prepare=IntrospectionRunPreprocessor(),
        enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        help=ConfigurationHelp("Detect and monitor a supported RTOS and its kernel resources.",
            '[CPU.cpu0.plugins.introspection]\nenabled = true\n\n[CPU.cpu0.plugins.introspection.activity_monitor]\nenabled = true',
            "docs/ActivityMonitor.md"),
    )
)
