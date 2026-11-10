"""Host preparation for the world_model FMI 3 co-simulation plugin.

Everything world-specific lives here: the plugin's TOML table is validated,
FMU archives are located, and a manifest is written for the native runtime.
One FastDyn configuration therefore describes both the emulated machine and
the physical world it is wired to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastdyn.host import AmbiguousSymbolError, SymbolNotFoundError
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

from .observer import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    generate_artifacts,
    start_observer,
    write_world_toml,
)

WORLD_ROOT = Path(__file__).resolve().parents[3] / "tools" / "world"

DEFAULT_STEP_NS = 100_000
PIN_KINDS = ("digital_out", "analog_in")

# Core-register names accepted in `register = "..."`, per architecture family.
# Indices match include/fastdyn/arch/arm32.h.
_ARM_REGISTERS = {f"r{index}": index for index in range(13)}
_ARM_REGISTERS.update({"sp": 13, "lr": 14, "pc": 15})


def _elf_symbols(ctx: RunContext):
    """Read the firmware's symbol table.

    FastDyn's generic symbol map is not populated on this path, so a plugin
    that accepts symbolic trigger names resolves them from the ELF itself.
    """
    try:
        return ctx.analysis.symbols
    except OSError as exc:
        raise VirtualPreparationError(
            f"world cannot read firmware ELF {ctx.binary}: {exc}"
        ) from exc


def _resolve_trigger(at: object, symbols, index: int) -> int:
    """Resolve one pin trigger. `symbols` is called only for a symbolic name,
    so a configuration using numeric addresses never needs a readable ELF."""
    if isinstance(at, int) and not isinstance(at, bool):
        return at
    if not isinstance(at, str) or not at:
        raise VirtualPreparationError(
            f"world pin #{index} needs at = \"<symbol or address>\""
        )
    token = at.strip()
    try:
        return int(token, 0)
    except ValueError:
        pass
    table = symbols()
    try:
        return table.lookup(token).code_address
    except (SymbolNotFoundError, AmbiguousSymbolError) as error:
        near = ", ".join(sorted({item.name for item in table.all()})[:8]) or "none"
        raise VirtualPreparationError(
            f"world pin trigger {token!r} cannot be resolved uniquely: {error}. "
            f"Known symbols include: {near}"
        ) from None


def _table(settings: dict, key: str) -> dict:
    value = settings.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VirtualPreparationError(
            f"[CPU.cpu0.plugins.world].{key} must be a table"
        )
    return value


def _resolve_register(name: object, architecture: str) -> int:
    if not isinstance(name, str):
        raise VirtualPreparationError("world pin 'register' must be a register name")
    if not architecture.lower().startswith("arm"):
        raise VirtualPreparationError(
            f"world pins currently support ARM cores; this CPU is {architecture!r}"
        )
    try:
        return _ARM_REGISTERS[name.strip().lower()]
    except KeyError:
        raise VirtualPreparationError(
            f"unknown ARM register {name!r}; use r0-r12, sp, lr or pc"
        ) from None


def _resolve_fmu(raw: object, model: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise VirtualPreparationError(
            f"[CPU.cpu0.plugins.world.models.{model}].path must be a path to an FMU"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        raise VirtualPreparationError(
            f"world model {model!r} FMU not found: {path}. "
            "Build it first (for the bundled RLC demo: "
            "cmake --build tools/world/build --target rlc_fmu)."
        )
    return path


def _float(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VirtualPreparationError(f"{where} must be a number")
    return float(value)


class WorldRunPreprocessor:
    """Turn the plugin's TOML table into a runtime manifest plus pin virtuals."""

    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        settings = dict(ctx.settings)

        step_ns = settings.get("step_ns", DEFAULT_STEP_NS)
        if isinstance(step_ns, bool) or not isinstance(step_ns, int) or step_ns <= 0:
            raise VirtualPreparationError(
                "[CPU.cpu0.plugins.world].step_ns must be a positive integer of nanoseconds"
            )

        models = _table(settings, "models")
        if not models:
            raise VirtualPreparationError(
                "[CPU.cpu0.plugins.world] needs at least one "
                "[CPU.cpu0.plugins.world.models.<name>] table with a path to an FMU"
            )

        endpoints = _table(settings, "endpoints")
        if not endpoints:
            raise VirtualPreparationError(
                "[CPU.cpu0.plugins.world].endpoints must name at least one "
                "world variable, for example: vcap = { target = \"rlc.output_voltage\", "
                "direction = \"out\" }"
            )

        lines: list[str] = [f"step_ns\t{step_ns}"]

        # Resolve FMU paths once. Both the runtime manifest and the observer's
        # generated world TOML need the absolute path: the latter is written to
        # a different directory, so a relative path would not resolve there.
        resolved_models: dict[str, dict] = {}
        for name, model in models.items():
            if not isinstance(model, dict):
                raise VirtualPreparationError(
                    f"[CPU.cpu0.plugins.world.models.{name}] must be a table"
                )
            fmu = _resolve_fmu(model.get("path"), name)
            parameters = {}
            for parameter, value in _table(model, "parameters").items():
                where = f"[CPU.cpu0.plugins.world.models.{name}.parameters].{parameter}"
                parameters[parameter] = _float(value, where)
            resolved_models[name] = {"path": str(fmu), "parameters": parameters}
            lines.append(f"model\t{name}\t{fmu}")

        for name, model in resolved_models.items():
            for parameter, value in model["parameters"].items():
                lines.append(f"param\t{name}\t{parameter}\t{value!r}")

        for source, target in _table(settings, "connections").items():
            if not isinstance(target, str):
                raise VirtualPreparationError(
                    f"[CPU.cpu0.plugins.world.connections].{source} must be an endpoint name"
                )
            lines.append(f"connect\t{source}\t{target}")

        endpoint_specs: dict[str, dict] = {}
        for alias, spec in endpoints.items():
            if not isinstance(spec, dict):
                raise VirtualPreparationError(
                    f"[CPU.cpu0.plugins.world.endpoints].{alias} must be a table with "
                    "'target' and 'direction'"
                )
            target = spec.get("target")
            direction = spec.get("direction")
            if not isinstance(target, str) or "." not in target:
                raise VirtualPreparationError(
                    f"world endpoint {alias!r} needs target = \"<model>.<variable>\""
                )
            if direction not in ("in", "out"):
                raise VirtualPreparationError(
                    f"world endpoint {alias!r} needs direction = \"in\" or \"out\""
                )
            model_name, _, variable = target.partition(".")
            if model_name not in models:
                raise VirtualPreparationError(
                    f"world endpoint {alias!r} targets unknown model {model_name!r}; "
                    f"known models: {', '.join(sorted(models)) or 'none'}"
                )
            endpoint_specs[alias] = {"target": target, "direction": direction}
            lines.append(f"endpoint\t{alias}\t{model_name}\t{variable}\t{direction}")

        # world_model routes FMU diagnostics and physical events through a log
        # callback. The runtime writes them here; the observer displays them.
        log_path = ctx.plugin_artifact_path("runtime.log")
        log_path.write_text("", encoding="utf-8")
        lines.append(f"log\t{log_path}")

        trace = _table(settings, "trace")
        artifacts = [log_path]
        trace_path = None
        trace_variables: list[str] = []
        if trace:
            output = trace.get("output")
            if not isinstance(output, str) or not output:
                raise VirtualPreparationError(
                    "[CPU.cpu0.plugins.world.trace].output must be a file name"
                )
            trace_path = ctx.plugin_artifact_path(output)
            lines.append(f"trace\t{trace_path}")
            for variable in trace.get("variables", []) or []:
                if not isinstance(variable, str):
                    raise VirtualPreparationError(
                        "[CPU.cpu0.plugins.world.trace].variables must be endpoint strings"
                    )
                trace_variables.append(variable)
                lines.append(f"trace_var\t{variable}")
            artifacts.append(trace_path)

        virtuals = self._pins(ctx, settings, {a: s["direction"] for a, s in endpoint_specs.items()})

        manifest = ctx.plugin_artifact_path("world.manifest")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        artifacts.insert(0, manifest)

        cleanup: list[Callable[[], None]] = []
        cleanup.append(self._runtime_absence_check(ctx, log_path, trace_path))
        observer_artifacts = self._observer(
            ctx, settings, resolved_models, endpoint_specs, _table(settings, "connections"),
            step_ns, trace_path, trace_variables, log_path, cleanup,
        )
        artifacts.extend(observer_artifacts)

        ctx.logger.info(
            "world configured %d model(s), %d endpoint(s), %d pin(s) at %d ns step",
            len(models), len(endpoints), len(virtuals), step_ns,
        )
        return RunPrepareResult(virtuals=virtuals, artifacts=artifacts, cleanup=cleanup)

    @staticmethod
    def _runtime_absence_check(ctx, log_path: Path, trace_path: Path | None):
        """Report a plugin that was configured but never ran.

        The native half of this plugin compiles in only when tools/world is
        built, and the host half cannot tell: FastDyn deliberately does not
        expose the plugin library to a preprocessor, and machine capabilities
        are not populated. So a plugin binary built without the world runtime
        leaves the manifest unread and every artifact empty, with nothing said
        about it. Checking after the run turns that into a clear diagnostic.
        """
        def check() -> None:
            wrote_log = log_path.exists() and log_path.stat().st_size > 0
            wrote_trace = (
                trace_path is not None
                and trace_path.exists()
                and trace_path.stat().st_size > 0
            )
            if wrote_log or wrote_trace:
                return
            ctx.logger.warning(
                "world was configured but its runtime never ran: no trace or log was "
                "written. The plugin library this run loaded was almost certainly built "
                "without the world runtime. Build tools/world first, then rebuild "
                "FastDyn (meson decides at configure time whether to compile the "
                "plugin in): cmake --build tools/world/build && make qemu_path=<...>"
            )
        return check

    def _observer(self, ctx, settings, models, endpoint_specs, connections,
                  step_ns, trace_path, trace_variables, log_path, cleanup) -> list[Path]:
        """Optionally serve world_model's read-only observer for this run."""
        config = settings.get("observer", False)
        if isinstance(config, bool):
            if not config:
                return []
            host, port, open_browser, stop_ns = DEFAULT_HOST, DEFAULT_PORT, False, 0
        elif isinstance(config, dict):
            if not bool(config.get("enabled", True)):
                return []
            host = str(config.get("host", DEFAULT_HOST))
            port = int(config.get("port", DEFAULT_PORT))
            open_browser = bool(config.get("open_browser", False))
            stop_ns = int(config.get("horizon_ns", 0))
        else:
            raise VirtualPreparationError(
                "[CPU.cpu0.plugins.world].observer must be a boolean or a table"
            )

        if trace_path is None:
            raise VirtualPreparationError(
                "the world observer plots a trace, so it needs a "
                "[CPU.cpu0.plugins.world.trace] table with an output file"
            )

        world_toml = ctx.plugin_artifact_path("observer/world.toml")
        out_dir = world_toml.parent / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        write_world_toml(
            world_toml,
            models=models, endpoints=endpoint_specs, connections=connections,
            step_ns=step_ns, stop_ns=stop_ns or step_ns * 2000,
            trace_output=trace_path, trace_variables=trace_variables,
            log_output=log_path,
            host=host, port=port, open_browser=open_browser,
        )
        manifest = generate_artifacts(WORLD_ROOT, world_toml, out_dir)
        server, url = start_observer(WORLD_ROOT, out_dir)
        cleanup.append(lambda: (server.shutdown(), server.server_close()))
        ctx.logger.info("World observer available at %s", url)
        # The native half of this plugin compiles in only when tools/world was
        # built before FastDyn. If it is missing, everything host-side still
        # works -- including this observer -- and the page simply never leaves
        # "waiting for trace". Say so here rather than leaving it a mystery.
        ctx.logger.info(
            "the page stays on 'waiting for trace' until the run logs "
            "'world ready'; if that never appears, the plugin was built without "
            "the world runtime -- build tools/world, then rebuild FastDyn"
        )
        return [world_toml, manifest, out_dir / "ui_config.json"]

    def _pins(
        self,
        ctx: RunContext,
        settings: dict,
        endpoint_directions: dict[str, str],
    ) -> list[VirtualInstruction]:
        raw_pins = settings.get("pins", []) or []
        if not isinstance(raw_pins, list):
            raise VirtualPreparationError(
                "[[CPU.cpu0.plugins.world.pins]] must be an array of tables"
            )

        cached = None

        def symbols():
            nonlocal cached
            if cached is None:
                cached = _elf_symbols(ctx)
            return cached

        virtuals: list[VirtualInstruction] = []
        seen_triggers: dict[int, object] = {}
        for index, pin in enumerate(raw_pins):
            if not isinstance(pin, dict):
                raise VirtualPreparationError(
                    f"world pin #{index} must be a table"
                )
            at = pin.get("at")
            address = _resolve_trigger(at, symbols, index)
            if address in seen_triggers:
                raise VirtualPreparationError(
                    f"world pins {seen_triggers[address]!r} and {at!r} resolve to the same "
                    f"address 0x{address:x}; the native dispatcher supports one callback per PC"
                )
            seen_triggers[address] = at
            trigger = f"0x{address:x}"

            kind = pin.get("kind")
            if kind not in PIN_KINDS:
                raise VirtualPreparationError(
                    f"world pin at {trigger!r} needs kind = one of {list(PIN_KINDS)}"
                )
            endpoint = pin.get("endpoint")
            if endpoint not in endpoint_directions:
                raise VirtualPreparationError(
                    f"world pin at {trigger!r} names unknown endpoint {endpoint!r}; "
                    f"declared endpoints: {', '.join(sorted(endpoint_directions)) or 'none'}"
                )
            register = _resolve_register(pin.get("register"), ctx.architecture)

            direction = endpoint_directions[endpoint]
            if kind == "digital_out":
                if direction != "in":
                    raise VirtualPreparationError(
                        f"world pin at {trigger!r} drives endpoint {endpoint!r}, which is "
                        "declared direction = \"out\"; a digital_out pin needs an input"
                    )
                low = _float(pin.get("low", 0.0), f"world pin at {trigger!r} 'low'")
                high = _float(pin.get("high", 3.3), f"world pin at {trigger!r} 'high'")
                args = [endpoint, str(register), repr(low), repr(high)]
                instruction = "world_digital_out"
            else:
                if direction != "out":
                    raise VirtualPreparationError(
                        f"world pin at {trigger!r} samples endpoint {endpoint!r}, which is "
                        "declared direction = \"in\"; an analog_in pin needs an output"
                    )
                scale = _float(pin.get("scale", 1.0), f"world pin at {trigger!r} 'scale'")
                args = [endpoint, str(register), repr(scale)]
                instruction = "world_analog_in"

            virtuals.append(
                VirtualInstruction(at=address, instruction=instruction, args=args)
            )
        return virtuals


register_virtual(VirtualDefinition(name="world_digital_out"))
register_virtual(VirtualDefinition(name="world_analog_in"))
register_run_preprocessor(
    RunDefinition(
        name="world",
        prepare=WorldRunPreprocessor(),
        enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
        help=ConfigurationHelp(
            "Co-simulate FMI 3 physics with the firmware and wire it to guest pins.",
            """[CPU.cpu0.plugins.world]
enabled = true
step_ns = 100000

[CPU.cpu0.plugins.world.models.rlc]
path = "tools/world/examples/rlc/out/RLC.fmu"

[CPU.cpu0.plugins.world.endpoints]
supply = { target = "rlc.voltage", direction = "in" }
vcap = { target = "rlc.output_voltage", direction = "out" }

[[CPU.cpu0.plugins.world.pins]]
at = "world_gpio_write"
kind = "digital_out"
endpoint = "supply"
register = "r0"
low = 0.0
high = 3.3""",
            "docs/WorldPlugin.md",
        ),
    )
)
