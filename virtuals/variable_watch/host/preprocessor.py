"""DWARF/raw-range preparation for VariableWatch."""
from __future__ import annotations

from dataclasses import dataclass

from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext, RunDefinition, RunPrepareResult, VirtualPreparationError,
    register_run_preprocessor,
)

try:  # Direct-import fallback keeps the utility independently testable.
    from _fastdyn_virtual_utils.object_access_analysis import ObjectAccessAnalysis
except ModuleNotFoundError:
    from virtuals.utils.object_access_analysis import ObjectAccessAnalysis


@dataclass(frozen=True)
class WatchTarget:
    name: str
    start: int
    size: int
    type_name: str
    parent: str


def _resolve_variable(ctx: RunContext, expression: str) -> WatchTarget:
    parent, *fields = expression.split(".")
    if not parent or any(not item for item in fields):
        raise VirtualPreparationError("variable_watch.variable must be NAME or NAME.field")
    if ctx.analysis.elf.has_dwarf_info():
        result = ctx.analysis.dwarf.global_variable(expression)
        if result is not None and result.type.size is not None:
            type_name = result.type.name or result.type.kind
            return WatchTarget(expression, result.address, result.type.size, type_name, parent)
    if not fields:
        matches = ctx.analysis.symbols.find(parent, kind="object")
        sized = [item for item in matches if item.size]
        if len({(item.address, item.size) for item in sized}) == 1:
            symbol = sized[0]
            return WatchTarget(parent, symbol.address, symbol.size, "unknown", parent)
    raise VirtualPreparationError(f"variable_watch could not resolve fixed-address variable {expression!r}")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise VirtualPreparationError(f"variable_watch.{name} must be an integer")
    try:
        result = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise VirtualPreparationError(f"variable_watch.{name} must be an integer") from exc
    if result < 0:
        raise VirtualPreparationError(f"variable_watch.{name} must be non-negative")
    return result


class VariableWatchPreprocessor:
    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        settings = dict(ctx.settings)
        variable, address = settings.get("variable"), settings.get("address")
        if (variable is None) == (address is None):
            raise VirtualPreparationError("variable_watch needs exactly one of variable or address")
        if variable is not None:
            if not isinstance(variable, str) or not variable:
                raise VirtualPreparationError("variable_watch.variable must be a non-empty string")
            target = _resolve_variable(ctx, variable)
        else:
            size = _integer(settings.get("size"), "size")
            if not size:
                raise VirtualPreparationError("variable_watch.size must be positive")
            start = _integer(address, "address")
            target = WatchTarget(f"0x{start:x}", start, size, "raw", "<raw>")
        access = settings.get("access", "read_write")
        if not isinstance(access, str) or access not in {"read", "write", "read_write"}:
            raise VirtualPreparationError("variable_watch.access must be read, write, or read_write")
        changes_only = settings.get("changes_only", False)
        if not isinstance(changes_only, bool):
            raise VirtualPreparationError("variable_watch.changes_only must be true or false")
        target_path = ctx.plugin_artifact_path("watch.tsv")
        events_path = ctx.plugin_artifact_path("events.tsv")
        candidates_path = ctx.plugin_artifact_path("candidate_accesses.tsv")
        target_path.write_text(
            "name\tstart\tsize\ttype\tparent\taccess\tchanges_only\n"
            f"{target.name}\t0x{target.start:x}\t{target.size}\t{target.type_name}\t{target.parent}\t{access}\t{int(changes_only)}\n",
            encoding="utf-8",
        )
        events_path.write_text("name\tpc\tfunction\taccess\taddress\tsize\told\tnew\ttype\n", encoding="utf-8")
        candidates = ObjectAccessAnalysis(ctx.analysis).candidate_accesses()
        candidates_path.write_text("pc\tfunction\n" + "".join(
            f"0x{item.pc:x}\t{(function.name if (function := ctx.analysis.symbols.containing_function(item.pc)) else '')}\n"
            for item in candidates
        ), encoding="utf-8")
        ctx.logger.info("variable_watch prepared %s [0x%x, +%d) with %d conservative candidates",
                        target.name, target.start, target.size, len(candidates))
        return RunPrepareResult(artifacts=[target_path, events_path, candidates_path])


register_run_preprocessor(RunDefinition(
    name="variable_watch", prepare=VariableWatchPreprocessor(),
    enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
    help=ConfigurationHelp("Log read/write access to a source variable or raw memory range.",
        '[CPU.cpu0.plugins.variable_watch]\nenabled = true\nvariable = "motor_state.temperature"\naccess = "write"',
        "docs/VariableWatch.md"),
))
