"""Build FMI artifacts described by a normal FastDyn TOML config."""

from __future__ import annotations

from dataclasses import dataclass
import os
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from . import timing

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    import tomli as tomllib


RUMOCA_REL = Path("third_party/common/rumoca")
MODELS_REL = Path("third_party/common/modelica_models")
SETUP_HINT = (
    "Run `source ./setup.sh --with-rumoca` from the FastDyn repository root "
    "to initialize the pinned Rumoca/modelica_models checkout."
)
REQUIRED_VALUE_REFERENCES = (
    "pwm",
    "gps",
    "yaw_deg",
    "gyro",
    "mag",
    "accel",
    "vel_ned",
    "baro_altitude_m",
    "baro_pressure_pa",
    "baro_temperature_c",
    "baro_climb_rate_mps",
    "lat0",
    "lon0",
    "ground_alt_wgs84",
    "earth_radius_m",
)


class FmuConfigError(RuntimeError):
    pass


class NoFmuConfig(FmuConfigError):
    pass


@dataclass(frozen=True)
class FmuBuild:
    name: str
    model: str
    model_file: Path
    source_root: Path
    extra_source_roots: tuple[Path, ...]
    output: Path
    repo_root: Path
    package: bool = True
    release: bool = False
    auto_build: bool = False
    parameters: dict[str, float | list] | None = None
    compiler: str | None = None

    @property
    def rumoca_dir(self) -> Path:
        return self.repo_root / RUMOCA_REL

    @property
    def fmu_path(self) -> Path:
        return self.output / f"{self.model.replace('.', '_')}.fmu"

    @property
    def artifact_path(self) -> Path:
        if self.package:
            return self.fmu_path
        return self.output / "modelDescription.xml"

    @property
    def source_roots(self) -> tuple[Path, ...]:
        return (self.source_root, *self.extra_source_roots)


@dataclass(frozen=True)
class FmuOverrides:
    model: str | None = None
    model_file: Path | None = None
    source_root: Path | None = None
    output: Path | None = None
    package: bool | None = None
    release: bool | None = None
    auto_build: bool | None = None


def find_repo_root(config_path: Path | None = None) -> Path:
    env_root = os.environ.get("FASTDYN_REPO_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    starts = [Path.cwd()]
    if config_path is not None:
        starts.append(config_path.expanduser().resolve().parent)

    for start in starts:
        for path in (start, *start.parents):
            if (path / ".git").exists() and (path / "third_party").exists():
                return path
    return Path.cwd().resolve()


def repo_relative(path: Path, repo_root: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path
    return repo_root / path


def load_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    if not isinstance(config, dict):
        raise FmuConfigError(f"FastDyn config must contain TOML tables: {path}")
    return config


def _bool(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise FmuConfigError(f"{label} must be true or false")


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise FmuConfigError(f"{name} must be true or false, got {value!r}")


def _required_table(config: dict[str, object], name: str) -> dict[str, object]:
    value = config.get(name)
    if value is None:
        raise NoFmuConfig(f"missing [{name}] table")
    if not isinstance(value, dict):
        raise FmuConfigError(f"[{name}] must be a table")
    return value


def _choose_model(fmu: dict[str, object], requested: str | None) -> tuple[str, dict[str, object]]:
    models = fmu.get("models")
    if models is None:
        if requested:
            raise FmuConfigError("--fmu requires [FMU.models.<name>] entries")
        return "default", fmu
    if not isinstance(models, dict):
        raise FmuConfigError("[FMU.models] must be a table")

    name = requested or os.environ.get("FASTDYN_FMI_NAME") or fmu.get("active")
    if not isinstance(name, str) or not name:
        available = ", ".join(sorted(models))
        raise FmuConfigError(f"set [FMU].active or pass --fmu. Available FMUs: {available}")

    selected = models.get(name)
    if not isinstance(selected, dict):
        available = ", ".join(sorted(models))
        raise FmuConfigError(f"unknown FMU {name!r}. Available FMUs: {available}")
    return name, selected


def _string(
    values: dict[str, object],
    key: str,
    env_name: str,
    override: str | None,
) -> str:
    value = override or os.environ.get(env_name) or values.get(key)
    if isinstance(value, str):
        return value
    if value is None:
        raise FmuConfigError(f"[FMU] model entry must set {key!r}")
    raise FmuConfigError(f"[FMU] {key} must be a string")


def _path(
    values: dict[str, object],
    key: str,
    env_name: str,
    override: Path | None,
    default: Path | None = None,
) -> Path:
    value = override or os.environ.get(env_name) or values.get(key) or default
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value).expanduser()
    if value is None:
        raise FmuConfigError(f"[FMU] model entry must set {key!r}")
    raise FmuConfigError(f"[FMU] {key} must be a path string")


def _path_list(
    values: dict[str, object],
    key: str,
    env_name: str,
    repo_root: Path,
) -> tuple[Path, ...] | None:
    value = os.environ.get(env_name) or values.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part for part in os.pathsep.split(value) if part]
        if not parts:
            raise FmuConfigError(f"[FMU] {key} must not be empty")
        return tuple(repo_relative(Path(part).expanduser(), repo_root) for part in parts)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        if not value:
            raise FmuConfigError(f"[FMU] {key} must not be empty")
        return tuple(repo_relative(Path(item).expanduser(), repo_root) for item in value)
    raise FmuConfigError(f"[FMU] {key} must be a path string or list of path strings")


def _flag(
    root_values: dict[str, object],
    model_values: dict[str, object],
    key: str,
    env_name: str,
    override: bool | None,
    default: bool,
) -> bool:
    if override is not None:
        return override
    env_value = _env_bool(env_name)
    if env_value is not None:
        return env_value
    if key in model_values:
        return _bool(model_values[key], f"[FMU] {key}")
    if key in root_values:
        return _bool(root_values[key], f"[FMU] {key}")
    return default


def parameter_shape(value):
    if isinstance(value, list):
        if not value:
            raise FmuConfigError("FMU parameter arrays must not be empty")
        shapes = [parameter_shape(item) for item in value]
        if any(shape != shapes[0] for shape in shapes):
            raise FmuConfigError("FMU parameter arrays must be rectangular")
        return (len(value), *shapes[0])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FmuConfigError("FMU parameters must contain finite numeric values")
    return ()


def parameter_values(value):
    """Flatten FMI arrays in row-major order, with the last index varying fastest."""
    if isinstance(value, list):
        return [number for item in value for number in parameter_values(item)]
    return [float(value)]


def parameter_argument(value):
    # Semicolons keep array entries separate from QEMU's comma-delimited options.
    return ";".join(f"{number:.17g}" for number in parameter_values(value))


def _parameters(values: dict[str, object]) -> dict[str, float | list]:
    raw = values.get("parameters", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FmuConfigError("[FMU.models.<name>.parameters] must be a table")

    parameters: dict[str, float | list] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise FmuConfigError("FMU parameter names must be non-empty strings")
        parameter_shape(value)
        parameters[name] = value if isinstance(value, list) else float(value)
    return parameters


def resolve(
    config_path: str | Path,
    fmu_name: str | None = None,
    overrides: FmuOverrides | None = None,
    repo_root: Path | None = None,
) -> FmuBuild:
    overrides = overrides or FmuOverrides()
    config_path = Path(config_path).expanduser().resolve()
    repo_root = repo_root or find_repo_root(config_path)

    fmu_root = _required_table(load_toml(config_path), "FMU")
    name, model_values = _choose_model(fmu_root, fmu_name)
    model = _string(model_values, "model", "FASTDYN_FMI_MODEL", overrides.model)
    identifier = model.replace(".", "_")
    source_roots = _path_list(
        model_values,
        "source_roots",
        "FASTDYN_FMI_SOURCE_ROOTS",
        repo_root,
    )
    if source_roots is None:
        source_root = repo_relative(
            _path(model_values, "source_root", "FASTDYN_FMI_SOURCE_ROOT", overrides.source_root),
            repo_root,
        )
        extra_source_roots: tuple[Path, ...] = ()
    else:
        if overrides.source_root is not None:
            source_roots = (repo_relative(overrides.source_root, repo_root), *source_roots)
        source_root = source_roots[0]
        extra_source_roots = source_roots[1:]

    output_default = Path("out") / "fmi3" / identifier
    compiler = fmu_root.get("compiler")
    if compiler is not None and (not isinstance(compiler, str) or not compiler.strip()):
        raise FmuConfigError("[FMU].compiler must be a non-empty executable path or name")
    return FmuBuild(
        name=name,
        model=model,
        model_file=repo_relative(
            _path(model_values, "model_file", "FASTDYN_FMI_MODEL_FILE", overrides.model_file),
            repo_root,
        ),
        source_root=source_root,
        extra_source_roots=extra_source_roots,
        output=repo_relative(
            _path(model_values, "output", "FASTDYN_FMI_OUTPUT", overrides.output, output_default),
            repo_root,
        ),
        repo_root=repo_root,
        package=_flag(
            fmu_root, model_values, "build", "FASTDYN_FMI_BUILD", overrides.package, True
        ),
        release=_flag(
            fmu_root, model_values, "release", "FASTDYN_FMI_RELEASE", overrides.release, False
        ),
        auto_build=_flag(
            fmu_root,
            model_values,
            "auto_build",
            "FASTDYN_FMI_AUTO_BUILD",
            overrides.auto_build,
            False,
        ),
        parameters=_parameters(model_values),
        compiler=compiler,
    )


def cargo_command(build: FmuBuild) -> list[str]:
    compiler = build.compiler
    if compiler:
        command = [compiler]
    else:
        command = ["cargo", "run", "--locked"]
        if build.release:
            command.append("--release")
        command.extend(["-p", "rumoca", "--"])

    command.extend(
        [
            "compile",
            str(build.model_file),
            "--model",
            build.model,
        ]
    )
    for source_root in build.source_roots:
        command.extend(["--source-root", str(source_root)])
    command.extend(
        [
            "--output",
            str(build.output),
            "--target",
            "fmi3",
        ]
    )
    return command


def update_submodules(repo_root: Path) -> None:
    with timing.phase("fmu.update_submodules"):
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "submodule",
                "update",
                "--init",
                "--recursive",
                str(RUMOCA_REL),
                str(MODELS_REL),
            ],
            check=True,
        )


def require_build_inputs(build: FmuBuild) -> None:
    compiler = build.compiler or "cargo"
    if shutil.which(compiler) is None:
        raise FmuConfigError(
            f"{compiler} was not found. Enter `nix develop`, set [FMU].compiler "
            "to a Rumoca executable, or install Rust/Cargo."
        )
    for label, path in (
        ("pinned Rumoca checkout", build.rumoca_dir / "Cargo.toml"),
        ("Modelica file", build.model_file),
    ):
        if not path.exists():
            raise FmuConfigError(f"missing {label}: {path}. {SETUP_HINT}")
    for source_root in build.source_roots:
        if not source_root.exists():
            raise FmuConfigError(f"missing source root: {source_root}. {SETUP_HINT}")


def _newest_model_mtime(build: FmuBuild) -> float:
    paths = [build.model_file]
    for source_root in build.source_roots:
        if source_root.is_dir():
            paths.extend(source_root.rglob("*.mo"))
        elif source_root.is_file():
            paths.append(source_root)
    return max(path.stat().st_mtime for path in paths if path.exists())


def needs_build(build: FmuBuild) -> bool:
    artifact = build.artifact_path
    if not artifact.exists():
        return True
    if not build.model_file.exists() or any(not path.exists() for path in build.source_roots):
        return False
    return artifact.stat().st_mtime < _newest_model_mtime(build)


def build_fmu(build: FmuBuild, update_submodules_first: bool = True) -> FmuBuild:
    if update_submodules_first:
        update_submodules(build.repo_root)
    require_build_inputs(build)
    with timing.phase("fmu.rumoca_compile", model=build.model, package=build.package):
        command = cargo_command(build)
        if build.package:
            subprocess.run(command, cwd=build.rumoca_dir, check=True)
            # Recent Rumoca exports portable source FMUs. Build their native
            # library using FMI build metadata; older binary FMUs are reused.
            from .fmu_runtime import prepare
            with timing.phase("fmu.prepare_native", model=build.model):
                prepare(build.fmu_path)
        else:
            templates = build.rumoca_dir / "crates/rumoca-phase-codegen/src/templates/fmi3"
            manifest_path = templates / "target.toml"
            with manifest_path.open("rb") as handle:
                source_package = "package" in tomllib.load(handle)
            if source_package:
                # New exporters always package portable source, without a C
                # build. Retain the flat inspection layout of --no-build too.
                subprocess.run(command, cwd=build.rumoca_dir, check=True)
                from fmpy import extract
                extract(str(build.fmu_path), unzipdir=str(build.output))
                return build
            # Rumoca selects packaging through target.toml, not a CLI flag.
            # Keep --no-build by rendering a private copy without that step.
            with tempfile.TemporaryDirectory(prefix="fastdyn-fmi3-") as temporary:
                target = Path(temporary) / "fmi3"
                shutil.copytree(templates, target)
                manifest = target / "target.toml"
                manifest.write_text(manifest.read_text().replace('build = "fmu"\n', ""))
                command[command.index("--target") + 1] = str(target)
                subprocess.run(command, cwd=build.rumoca_dir, check=True)
    return build


def _model_description_xml(build: FmuBuild) -> bytes:
    if build.package and build.fmu_path.is_file():
        with zipfile.ZipFile(build.fmu_path) as archive:
            try:
                return archive.read("modelDescription.xml")
            except KeyError as exc:
                raise FmuConfigError(
                    f"FMU is missing modelDescription.xml: {build.fmu_path}"
                ) from exc

    xml_path = build.output / "modelDescription.xml"
    if xml_path.is_file():
        return xml_path.read_bytes()

    raise FmuConfigError(
        f"FMU metadata not found for {build.name}: expected {xml_path} or {build.fmu_path}"
    )


def value_references(build: FmuBuild) -> dict[str, int]:
    """Read named FMI value references from the generated modelDescription.xml."""

    try:
        root = ET.fromstring(_model_description_xml(build))
    except ET.ParseError as exc:
        raise FmuConfigError(f"invalid FMU modelDescription.xml for {build.name}: {exc}") from exc

    requested = set(REQUIRED_VALUE_REFERENCES)
    requested.update((build.parameters or {}).keys())

    refs: dict[str, int] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Float64":
            continue
        name = element.attrib.get("name")
        raw_vr = element.attrib.get("valueReference")
        if name in requested and raw_vr is not None:
            if name in (build.parameters or {}):
                if element.attrib.get("causality") == "calculatedParameter":
                    raise FmuConfigError(
                        f"FMU parameter {name!r} is calculated and cannot be overridden; "
                        "set its independent source parameters, or change the Modelica "
                        "binding and rebuild the FMU"
                    )
                dimensions = [child.attrib.get("start") for child in element
                              if child.tag.rsplit("}", 1)[-1] == "Dimension"]
                if any(size is None for size in dimensions):
                    raise FmuConfigError(f"FMU parameter {name!r} has unsupported dynamic dimensions")
                expected = tuple(int(size) for size in dimensions)
                actual = parameter_shape(build.parameters[name])
                if actual != expected:
                    raise FmuConfigError(f"FMU parameter {name!r}: expected shape {expected}, got {actual}")
            try:
                refs[name] = int(raw_vr)
            except ValueError as exc:
                raise FmuConfigError(
                    f"FMU variable {name!r} has invalid valueReference {raw_vr!r}"
                ) from exc

    missing = [name for name in REQUIRED_VALUE_REFERENCES if name not in refs]
    if missing:
        raise FmuConfigError(
            f"FMU {build.name!r} is missing required variables: {', '.join(missing)}"
        )
    missing_parameters = sorted(
        name for name in (build.parameters or {}) if name not in refs
    )
    if missing_parameters:
        raise FmuConfigError(
            f"FMU {build.name!r} is missing configured parameter variables: "
            f"{', '.join(missing_parameters)}"
        )
    return refs
