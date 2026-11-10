"""Educational run preprocessor that instruments every selected function."""

from __future__ import annotations

from fnmatch import fnmatchcase

from fastdyn.machine import VirtualInstruction
from fastdyn.virtual_preprocessing import (
    ConfigurationHelp,
    RunContext,
    RunDefinition,
    RunPrepareResult,
    VirtualDefinition,
    VirtualPreparationError,
    register_run_preprocessor,
    register_virtual,
)


MAX_FUNCTIONS = 4096


def _patterns(settings: dict[str, object], key: str) -> tuple[str, ...]:
    value = settings.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise VirtualPreparationError(
            f"[CPU.cpu0.plugins.function_counter].{key} must be an array of glob patterns"
        )
    return tuple(value)


def _function_entries(ctx: RunContext) -> list[tuple[int, str]]:
    """Return one stable, executable entry point for each ELF function."""
    entries: dict[int, str] = {}
    try:
        for symbol in ctx.analysis.symbols.all(kind="function"):
            entries.setdefault(symbol.code_address, symbol.name)
    except OSError as exc:
        raise VirtualPreparationError(
            f"function_counter cannot read firmware ELF {ctx.binary}: {exc}"
        ) from exc
    return sorted(entries.items())


class FunctionCounterRunPreprocessor:
    """Generate one ``function_counter`` virtual for every selected symbol."""

    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        settings = dict(ctx.settings)
        include = _patterns(settings, "include")
        exclude = _patterns(settings, "exclude")
        limit = settings.get("max_functions", MAX_FUNCTIONS)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_FUNCTIONS:
            raise VirtualPreparationError(
                f"[CPU.cpu0.plugins.function_counter].max_functions must be 1..{MAX_FUNCTIONS}"
            )

        entries = _function_entries(ctx)
        selected = [
            (address, name) for address, name in entries
            if (not include or any(fnmatchcase(name, pattern) for pattern in include))
            and not any(fnmatchcase(name, pattern) for pattern in exclude)
        ]
        if not selected:
            if not entries:
                raise VirtualPreparationError(
                    f"function_counter found no executable ELF function symbols in {ctx.binary}. "
                    "Use an ELF with a symbol table, or choose another plugin for a raw binary."
                )
            examples = ", ".join(repr(name) for _address, name in entries[:8])
            filters = []
            if include:
                filters.append(f"include={list(include)!r}")
            if exclude:
                filters.append(f"exclude={list(exclude)!r}")
            raise VirtualPreparationError(
                f"function_counter matched no executable functions in {ctx.binary} "
                f"with {', '.join(filters) or 'the configured filters'}. "
                f"Try a matching glob such as one of: {examples}; "
                "or remove include to count all executable functions."
            )
        invalid_names = [
            name for _address, name in selected
            if any(character.isspace() for character in name) or len(name.encode("utf-8")) >= 256
        ]
        if invalid_names:
            raise VirtualPreparationError(
                "function_counter cannot serialize function symbol(s): "
                + ", ".join(repr(name) for name in invalid_names[:3])
            )
        if len(selected) > limit:
            raise VirtualPreparationError(
                f"function_counter selected {len(selected)} functions, exceeding max_functions={limit}; "
                "narrow include/exclude patterns or raise the limit"
            )

        manifest = ctx.plugin_artifact_path("functions.tsv")
        manifest.write_text(
            "address\tfunction\n" + "".join(
                f"0x{address:x}\t{name}\n" for address, name in selected
            ),
            encoding="utf-8",
        )
        counts = ctx.plugin_artifact_path("counts.tsv")
        counts.write_text("address\tfunction\tcalls\n", encoding="utf-8")
        ctx.logger.info("function_counter installed %d function-entry hooks", len(selected))
        return RunPrepareResult(
            virtuals=[
                VirtualInstruction(at=address, instruction="function_counter", args=[name])
                for address, name in selected
            ],
            artifacts=[manifest, counts],
        )


register_virtual(VirtualDefinition(name="function_counter"))
register_run_preprocessor(
    RunDefinition(
        name="function_counter",
        prepare=FunctionCounterRunPreprocessor(),
        enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        help=ConfigurationHelp("Instrument selected ELF function entries and write call counts.",
            '[CPU.cpu0.plugins.function_counter]\nenabled = true\ninclude = ["main", "my_api_*"]\nmax_functions = 64',
            "docs/FunctionCounterPlugin.md"),
    )
)
