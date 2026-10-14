"""Runtime helper processes described by a FastDyn TOML config."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator
import logging

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    import tomli as tomllib

from . import fmu_build, profiling, timing

log = logging.getLogger(__name__)


class RuntimeConfigError(RuntimeError):
    pass


class HelperExitedError(RuntimeConfigError):
    """A helper process exited with a non-zero status while FastDyn was running."""


_ENV_PATTERN = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)
_TOML_LOCATION_PATTERN = re.compile(r"\(at line (?P<line>\d+), column (?P<column>\d+)\)")
_TOML_TABLE_PATTERN = re.compile(r"^\s*(?P<table>\[\[?[^\]]+\]?\])\s*(?:#.*)?$")


def _format_toml_error(path: Path, error: tomllib.TOMLDecodeError) -> str:
    """Turn parser-only TOML failures into a correction-oriented diagnostic."""
    parser_message = str(error)
    location = _TOML_LOCATION_PATTERN.search(parser_message)
    if location is None:
        return f"Invalid TOML configuration in {path}: {parser_message}"

    line_number = int(location.group("line"))
    column_number = int(location.group("column"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    line = lines[line_number - 1] if line_number <= len(lines) else ""

    table = ""
    for previous in reversed(lines[:line_number - 1]):
        match = _TOML_TABLE_PATTERN.match(previous)
        if match:
            table = f" in {match.group('table')}"
            break

    if "Cannot overwrite a value" in parser_message:
        key = line.partition("=")[0].strip() or "this key"
        return (
            f"Invalid TOML configuration: {path}:{line_number}:{column_number}: "
            f"duplicate key {key!r}{table}. TOML keys may be defined only once; "
            "remove the duplicate or edit the original setting."
        )
    return f"Invalid TOML configuration: {path}:{line_number}:{column_number}: {parser_message}"


def expand_env_defaults(value: str, env: dict[str, str] | None = None) -> str:
    """Expand ${VAR} and ${VAR:-default} in TOML strings."""

    merged = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        name = match.group("braced")
        default = match.group("default")
        current = merged.get(name)
        if current not in (None, ""):
            return current
        if default is not None:
            return default
        return ""

    return _ENV_PATTERN.sub(replace, value)


def _expand_value(value: object, env: dict[str, str]) -> object:
    if isinstance(value, str):
        return expand_env_defaults(value, env)
    if isinstance(value, list):
        return [_expand_value(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand_value(val, env) for key, val in value.items()}
    return value


@dataclass(frozen=True)
class RuntimeProcess:
    name: str
    command: str | list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    ready_message: str | None = None
    background: bool = True
    enabled: bool = True
    quiet: bool = False
    start_delay_sec: float = 0.0
    stop_on_exit: bool = True
    terminate_run_on_exit: bool = False
    shell: bool | None = None

    @property
    def uses_shell(self) -> bool:
        if self.shell is not None:
            return self.shell
        return isinstance(self.command, str)


@dataclass
class RuntimeProcessHandle:
    process_config: RuntimeProcess
    process: subprocess.Popen | None = None

    def terminate(self, timeout: float) -> None:
        if self.process is None or not self.process_config.stop_on_exit:
            return
        if self.process.poll() is not None:
            return

        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            self.process.terminate()

        try:
            self.process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            self.process.kill()
        self.process.wait(timeout=timeout)


def _load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RuntimeConfigError(f"Runtime config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeConfigError(_format_toml_error(path, exc)) from exc
    if not isinstance(data, dict):
        raise RuntimeConfigError(f"Runtime config must contain TOML tables: {path}")
    return data


def validate_config(config_path: str | Path) -> None:
    """Validate TOML syntax before a command changes its work directory."""
    _load_toml(Path(config_path).expanduser().resolve())


def _as_table(value: object, label: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{label} must be a TOML table")
    return value


def _as_bool(value: object, label: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise RuntimeConfigError(f"{label} must be true or false")


def _as_float(value: object, label: str, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    raise RuntimeConfigError(f"{label} must be a number")


def _as_str(value: object, label: str) -> str:
    if isinstance(value, str):
        return value
    raise RuntimeConfigError(f"{label} must be a string")


def _as_str_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise RuntimeConfigError(f"{label} must be a list of strings")


def _as_env(value: object, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{label} must be a TOML table")
    return {str(key): str(val) for key, val in value.items()}


def _expanded_env(value: object, label: str, env: dict[str, str]) -> dict[str, str]:
    raw = _as_env(value, label)
    expanded: dict[str, str] = {}
    for key, val in raw.items():
        expanded[key] = expand_env_defaults(val, {**env, **expanded})
    return expanded


def _repo_path(value: str | Path, repo_root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return repo_root / path


def _command_to_string(command: str | list[str]) -> str:
    if isinstance(command, str):
        return command
    return shlex.join(command)


def _check_cwd(process: RuntimeProcess) -> None:
    if not process.cwd.exists():
        raise RuntimeConfigError(f"helper '{process.name}' cwd does not exist: {process.cwd}")
    if not process.cwd.is_dir():
        raise RuntimeConfigError(f"helper '{process.name}' cwd is not a directory: {process.cwd}")


def _command_with_delay(
    command: str | list[str],
    shell: bool,
    start_delay_sec: float,
) -> tuple[str | list[str], bool]:
    if start_delay_sec <= 0:
        return command, shell

    delayed = f"sleep {start_delay_sec:g}; exec {_command_to_string(command)}"
    return delayed, True


def _iter_process_tables(run_table: dict[str, object]) -> list[tuple[str | None, dict[str, object]]]:
    processes = run_table.get("processes")
    if processes is None:
        return []
    if isinstance(processes, list):
        out = []
        for idx, item in enumerate(processes):
            if not isinstance(item, dict):
                raise RuntimeConfigError(f"[[Run.processes]] entry {idx} must be a table")
            out.append((None, item))
        return out
    if isinstance(processes, dict):
        out = []
        for name, item in processes.items():
            if not isinstance(item, dict):
                raise RuntimeConfigError(f"[Run.processes.{name}] must be a table")
            out.append((str(name), item))
        return out
    raise RuntimeConfigError("[Run.processes] must be named tables or an array of tables")


def _parse_process(
    name: str | None,
    values: dict[str, object],
    repo_root: Path,
    defaults: dict[str, object],
    global_env: dict[str, str],
) -> RuntimeProcess:
    inherited_env = {**os.environ, **global_env}
    process_env = _expanded_env(values.get("env"), f"{name or 'process'}.env", inherited_env)
    expansion_env = {**inherited_env, **process_env}

    command = values.get("command")
    if isinstance(command, str):
        parsed_command: str | list[str] = expand_env_defaults(command, expansion_env)
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        parsed_command = [expand_env_defaults(item, expansion_env) for item in command]
    else:
        label = f"[Run.processes.{name}].command" if name else "[[Run.processes]].command"
        raise RuntimeConfigError(f"{label} must be a string or list of strings")

    process_name = name or str(values.get("name") or "")
    if not process_name:
        process_name = _command_to_string(parsed_command).split(maxsplit=1)[0]

    cwd_value = values.get("cwd", defaults.get("cwd", "."))
    cwd_text = expand_env_defaults(_as_str(cwd_value, f"{process_name}.cwd"), expansion_env)
    cwd = _repo_path(cwd_text, repo_root)
    env = {**global_env, **process_env}
    shell = (
        _as_bool(values.get("shell"), f"{process_name}.shell", None)
        if "shell" in values
        else None
    )
    if shell and isinstance(parsed_command, list):
        parsed_command = _command_to_string(parsed_command)

    return RuntimeProcess(
        name=process_name,
        command=parsed_command,
        cwd=cwd,
        env=env,
        ready_message=(
            expand_env_defaults(values["ready_message"], expansion_env)
            if isinstance(values.get("ready_message"), str)
            else None
        ),
        background=_as_bool(values.get("background"), f"{process_name}.background", True),
        enabled=_as_bool(values.get("enabled"), f"{process_name}.enabled", True),
        quiet=_as_bool(values.get("quiet"), f"{process_name}.quiet", False),
        start_delay_sec=_as_float(
            values.get("start_delay_sec", values.get("start_delay")),
            f"{process_name}.start_delay_sec",
        ),
        stop_on_exit=_as_bool(values.get("stop_on_exit"), f"{process_name}.stop_on_exit", True),
        terminate_run_on_exit=_as_bool(
            values.get("terminate_run_on_exit", values.get("stop_run_on_exit")),
            f"{process_name}.terminate_run_on_exit",
            False,
        ),
        shell=shell,
    )


def _rumoca_process(
    rumoca: dict[str, object],
    repo_root: Path,
) -> RuntimeProcess | None:
    enabled = _as_bool(rumoca.get("enabled", rumoca.get("auto_start")), "[Rumoca].enabled", False)
    if not enabled:
        return None

    rumoca_env = _expanded_env(rumoca.get("env"), "[Rumoca.env]", os.environ.copy())
    expansion_env = {**os.environ, **rumoca_env}

    rumoca_dir = _repo_path(
        expand_env_defaults(_as_str(rumoca.get("cwd", str(fmu_build.RUMOCA_REL)), "[Rumoca].cwd"), expansion_env),
        repo_root,
    )
    if not (rumoca_dir / "Cargo.toml").exists():
        raise RuntimeConfigError(f"missing pinned Rumoca checkout: {rumoca_dir}. {fmu_build.SETUP_HINT}")

    config = rumoca.get("config")
    if not isinstance(config, str) or not config:
        raise RuntimeConfigError("[Rumoca].config must point at a rumoca lockstep TOML file")

    webviewer = _as_table(rumoca.get("webviewer"), "[Rumoca.webviewer]")
    command: list[str]
    binary = rumoca.get("binary")
    if binary:
        command = [expand_env_defaults(_as_str(binary, "[Rumoca].binary"), expansion_env)]
    else:
        command = [
            expand_env_defaults(_as_str(rumoca.get("cargo", "cargo"), "[Rumoca].cargo"), expansion_env),
            "run",
            "-p",
            "rumoca",
        ]
        features = _as_str_list(rumoca.get("features", ["lockstep"]), "[Rumoca].features")
        if features:
            command.extend(["--features", ",".join(features)])
        if _as_bool(rumoca.get("release"), "[Rumoca].release", True):
            command.append("--release")
        command.append("--")

    config = expand_env_defaults(config, expansion_env)
    command.extend(["lockstep", "run", "-c", str(_repo_path(config, repo_root))])

    model = rumoca.get("model")
    if model:
        model_text = expand_env_defaults(_as_str(model, "[Rumoca].model"), expansion_env)
        command.extend(["--model", str(_repo_path(model_text, repo_root))])

    scene = webviewer.get("scene", rumoca.get("scene"))
    if scene:
        scene_text = expand_env_defaults(_as_str(scene, "[Rumoca.webviewer].scene"), expansion_env)
        command.extend(["--scene", str(_repo_path(scene_text, repo_root))])

    http_port = expansion_env.get(
        "FASTDYN_RUMOCA_HTTP_PORT",
        webviewer.get("http_port", rumoca.get("http_port")),
    )
    if http_port is not None:
        if isinstance(http_port, str):
            http_port = expand_env_defaults(http_port, expansion_env)
        command.extend(["--http-port", str(int(_as_float(http_port, "[Rumoca.webviewer].http_port")))])

    ws_port = expansion_env.get(
        "FASTDYN_RUMOCA_WS_PORT",
        webviewer.get("ws_port", rumoca.get("ws_port")),
    )
    if ws_port is not None:
        if isinstance(ws_port, str):
            ws_port = expand_env_defaults(ws_port, expansion_env)
        command.extend(["--ws-port", str(int(_as_float(ws_port, "[Rumoca.webviewer].ws_port")))])

    if _as_bool(webviewer.get("debug", rumoca.get("debug")), "[Rumoca.webviewer].debug", False):
        command.append("--debug")

    command.extend(_as_str_list(rumoca.get("extra_args"), "[Rumoca].extra_args"))
    viewer_url = webviewer.get("url")
    if viewer_url is not None:
        viewer_url = expand_env_defaults(_as_str(viewer_url, "[Rumoca.webviewer].url"), expansion_env)
    else:
        viewer_url = f"http://127.0.0.1:{int(_as_float(http_port, '[Rumoca.webviewer].http_port', 8080))}"

    return RuntimeProcess(
        name=_as_str(rumoca.get("name", "rumoca"), "[Rumoca].name"),
        command=command,
        cwd=rumoca_dir,
        env=rumoca_env,
        ready_message=f"Rumoca web viewer: {viewer_url}",
        background=_as_bool(rumoca.get("background"), "[Rumoca].background", True),
        enabled=True,
        quiet=_as_bool(rumoca.get("quiet"), "[Rumoca].quiet", False),
        start_delay_sec=_as_float(
            rumoca.get("start_delay_sec", rumoca.get("start_delay")),
            "[Rumoca].start_delay_sec",
        ),
        stop_on_exit=_as_bool(rumoca.get("stop_on_exit"), "[Rumoca].stop_on_exit", True),
        terminate_run_on_exit=_as_bool(
            rumoca.get("terminate_run_on_exit", rumoca.get("stop_run_on_exit")),
            "[Rumoca].terminate_run_on_exit",
            False,
        ),
        shell=False,
    )


def _profiling_env(run_table: dict[str, object], work_dir: str | Path) -> dict[str, str]:
    profiling_table = _as_table(run_table.get("profiling"), "[Run.profiling]")
    work_dir_path = Path(work_dir).expanduser().resolve()

    timing_enabled = _as_bool(profiling_table.get("timing"), "[Run.profiling].timing", True)
    timing_echo = _as_bool(profiling_table.get("timing_echo"), "[Run.profiling].timing_echo", True)
    python_profile = _as_bool(
        profiling_table.get("python", profiling_table.get("cprofile")),
        "[Run.profiling].python",
        False,
    )
    fmu_profile = _as_bool(profiling_table.get("fmu"), "[Run.profiling].fmu", timing_enabled)

    perf_raw = profiling_table.get("perf", profiling_table.get("perf_mode", "off"))
    if isinstance(perf_raw, bool):
        perf_mode = "stat" if perf_raw else "off"
    elif isinstance(perf_raw, str):
        perf_mode = perf_raw.strip().lower()
    else:
        raise RuntimeConfigError("[Run.profiling].perf must be a string or boolean")
    if perf_mode in ("", "none", "false", "no", "0"):
        perf_mode = "off"
    if perf_mode not in ("off", "stat", "record"):
        raise RuntimeConfigError("[Run.profiling].perf must be one of: off, stat, record")

    events_raw = profiling_table.get("perf_events")
    if events_raw is None:
        perf_events = ""
    elif isinstance(events_raw, str):
        perf_events = events_raw.strip()
    elif isinstance(events_raw, list) and all(isinstance(item, str) for item in events_raw):
        perf_events = ",".join(item.strip() for item in events_raw if item.strip())
    else:
        raise RuntimeConfigError("[Run.profiling].perf_events must be a string or list of strings")

    freq = profiling_table.get("perf_frequency_hz", profiling_table.get("perf_freq_hz"))
    if freq is None:
        perf_freq = "99"
    elif isinstance(freq, (int, float)) and not isinstance(freq, bool):
        perf_freq = f"{float(freq):g}"
    else:
        raise RuntimeConfigError("[Run.profiling].perf_frequency_hz must be a number")

    return {
        "FASTDYN_TIMING": "1" if timing_enabled else "0",
        "FASTDYN_TIMING_ECHO": "1" if timing_echo else "0",
        "FASTDYN_TIMING_FILE": str(work_dir_path / "fastdyn_timing.jsonl"),
        "FASTDYN_PYTHON_PROFILE": "1" if python_profile else "0",
        "FASTDYN_FMU_PROFILE": "1" if fmu_profile else "0",
        "FASTDYN_PERF_MODE": perf_mode,
        "FASTDYN_PERF_EVENTS": perf_events,
        "FASTDYN_PERF_FREQ_HZ": perf_freq,
    }


def configure_run_environment(config_path: str | Path, work_dir: str | Path) -> dict[str, str]:
    config_path = Path(config_path).expanduser().resolve()
    work_dir_path = Path(work_dir).expanduser().resolve()
    data = _load_toml(config_path)
    run_table = _as_table(data.get("Run"), "[Run]")

    env = {
        "FASTDYN_CONFIG": str(config_path),
        "FASTDYN_WORK_DIR": str(work_dir_path),
        **_profiling_env(run_table, work_dir_path),
    }
    os.environ.update(env)
    timing.configure(
        file=env["FASTDYN_TIMING_FILE"],
        enabled_value=env["FASTDYN_TIMING"] == "1",
        echo_value=env["FASTDYN_TIMING_ECHO"] == "1",
        process="fastdyn",
    )
    return env


def load_processes(config_path: str | Path, repo_root: Path | None = None) -> list[RuntimeProcess]:
    config_path = Path(config_path).expanduser().resolve()
    repo_root = repo_root or fmu_build.find_repo_root(config_path)
    data = _load_toml(config_path)

    processes: list[RuntimeProcess] = []
    rumoca = _as_table(data.get("Rumoca"), "[Rumoca]")
    rumoca_proc = _rumoca_process(rumoca, repo_root) if rumoca else None
    if rumoca_proc is not None:
        processes.append(rumoca_proc)

    run_table = _as_table(data.get("Run"), "[Run]")
    if run_table:
        global_env = _as_env(run_table.get("env"), "[Run.env]")
        defaults = {"cwd": run_table.get("cwd", ".")}
        for name, values in _iter_process_tables(run_table):
            processes.append(_parse_process(name, values, repo_root, defaults, global_env))

    return [process for process in processes if process.enabled]


class RuntimeProcessManager:
    def __init__(
        self,
        processes: list[RuntimeProcess],
        config_path: str | Path,
        work_dir: str | Path,
        stop_timeout_sec: float = 5.0,
    ):
        self.processes = processes
        self.config_path = Path(config_path).expanduser().resolve()
        self.work_dir = Path(work_dir).expanduser().resolve()
        self.stop_timeout_sec = stop_timeout_sec
        self.handles: list[RuntimeProcessHandle] = []
        self._terminator_stop = threading.Event()
        self._terminator_thread: threading.Thread | None = None
        self._terminator_exit: tuple[RuntimeProcessHandle, int] | None = None
        self._terminator_error: Exception | None = None

    def _env(self, process: RuntimeProcess) -> dict[str, str]:
        env = os.environ.copy()
        exe_dir = str(Path(sys.executable).parent)
        env["PATH"] = os.pathsep.join([exe_dir, env.get("PATH", "")])
        env.update(
            {
                "FASTDYN_CONFIG": str(self.config_path),
                "FASTDYN_WORK_DIR": str(self.work_dir),
                "FASTDYN_TIMING_FILE": str(self.work_dir / "fastdyn_timing.jsonl"),
                "FASTDYN_TIMING_PROCESS": process.name,
            }
        )
        env.update(process.env)
        return env

    def _run_foreground(self, process: RuntimeProcess) -> None:
        _check_cwd(process)
        if process.start_delay_sec > 0:
            with timing.phase(f"helper.{process.name}.start_delay", seconds=process.start_delay_sec):
                time.sleep(process.start_delay_sec)
        log.info("Running configured helper '%s': %s", process.name, _command_to_string(process.command))
        if process.ready_message:
            log.info(process.ready_message)
        command, shell = profiling.wrap_python_profile_command(
            process.command,
            name=process.name,
            cwd=process.cwd,
            work_dir=self.work_dir,
            shell=process.uses_shell,
        )
        with timing.phase(f"helper.{process.name}.foreground", command=_command_to_string(command)):
            subprocess.run(
                command,
                cwd=process.cwd,
                env=self._env(process),
                shell=shell,
                stdout=subprocess.DEVNULL if process.quiet else None,
                stderr=subprocess.DEVNULL if process.quiet else None,
                check=True,
            )

    def _start_background(self, process: RuntimeProcess) -> None:
        _check_cwd(process)
        command, shell = profiling.wrap_python_profile_command(
            process.command,
            name=process.name,
            cwd=process.cwd,
            work_dir=self.work_dir,
            shell=process.uses_shell,
        )
        command, shell = _command_with_delay(command, shell, process.start_delay_sec)
        log.info("Starting configured helper '%s': %s", process.name, _command_to_string(process.command))
        try:
            with timing.phase(
                f"helper.{process.name}.popen",
                command=_command_to_string(command),
                start_delay_sec=process.start_delay_sec,
            ):
                proc = subprocess.Popen(
                    command,
                    cwd=process.cwd,
                    env=self._env(process),
                    shell=shell,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL if process.quiet else None,
                    stderr=subprocess.DEVNULL if process.quiet else None,
                )
        except FileNotFoundError as exc:
            raise RuntimeConfigError(f"helper '{process.name}' command was not found: {process.command}") from exc
        self.handles.append(RuntimeProcessHandle(process, proc))
        timing.mark(f"helper.{process.name}.pid", child_pid=proc.pid)
        if process.ready_message:
            log.info(process.ready_message)

    def start(self) -> None:
        with timing.phase("helpers.start", count=len(self.processes)):
            for process in self.processes:
                if process.background:
                    self._start_background(process)
                else:
                    self._run_foreground(process)

    def stop(self) -> None:
        self.stop_terminator_watcher()
        with timing.phase("helpers.stop", count=len(self.handles)):
            for handle in reversed(self.handles):
                with timing.phase(f"helper.{handle.process_config.name}.terminate", echo=False):
                    handle.terminate(self.stop_timeout_sec)
            self.handles.clear()

    def start_terminator_watcher(
        self,
        on_exit: Callable[[RuntimeProcessHandle, int], None],
    ) -> None:
        if self._terminator_thread is not None:
            return
        if not any(handle.process_config.terminate_run_on_exit for handle in self.handles):
            return

        self._terminator_stop.clear()

        def watch() -> None:
            while not self._terminator_stop.is_set():
                for handle in list(self.handles):
                    if not handle.process_config.terminate_run_on_exit or handle.process is None:
                        continue
                    exit_code = handle.process.poll()
                    if exit_code is None:
                        continue
                    self._terminator_exit = (handle, exit_code)
                    timing.mark(
                        f"helper.{handle.process_config.name}.exited",
                        echo=True,
                        exit_code=exit_code,
                    )
                    try:
                        on_exit(handle, exit_code)
                    except Exception as exc:  # pragma: no cover - defensive cleanup path.
                        self._terminator_error = exc
                    return
                self._terminator_stop.wait(0.2)

        self._terminator_thread = threading.Thread(
            target=watch,
            name="fastdyn-helper-terminator",
            daemon=True,
        )
        self._terminator_thread.start()

    def stop_terminator_watcher(self) -> None:
        if self._terminator_thread is None:
            return
        self._terminator_stop.set()
        self._terminator_thread.join(timeout=1.0)
        self._terminator_thread = None

    def raise_for_terminator_failure(self) -> None:
        if self._terminator_error is not None:
            raise RuntimeConfigError(f"terminator callback failed: {self._terminator_error}") from self._terminator_error
        if self._terminator_exit is None:
            return
        handle, exit_code = self._terminator_exit
        if exit_code != 0:
            raise HelperExitedError(
                f"helper '{handle.process_config.name}' exited with status {exit_code}"
            )


@contextmanager
def launch_from_config(
    config_path: str | Path,
    work_dir: str | Path,
    skip: bool = False,
) -> Iterator[RuntimeProcessManager | None]:
    if skip:
        yield None
        return

    with timing.phase("helpers.load_config"):
        processes = load_processes(config_path)
    if not processes:
        yield None
        return

    manager = RuntimeProcessManager(processes, config_path, work_dir)
    manager.start()
    try:
        yield manager
    finally:
        manager.stop()
