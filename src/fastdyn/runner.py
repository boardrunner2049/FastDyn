"""Run a FastDyn configuration end to end: the orchestration behind `fastdyn run`."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import fmu_build, paths, runtime_config, timing, toml_parser
from .fastdyn import Fastdyn

log = logging.getLogger(__name__)

DEFAULT_WORK_DIR = "fastdyn_work"

Reporter = Callable[[str], None]


class RunError(RuntimeError):
    """A run could not be prepared, e.g. an FMU auto-build failed."""


@dataclass(frozen=True)
class RunResult:
    """Outcome of one machine of a configuration.

    ``exit_code`` is QEMU's exit code. It is non-zero only when FastDyn itself
    stopped QEMU; an unexpected failure raises ``QemuExitError`` instead.
    ``work_dir`` holds every artifact of the run (``run-artifacts/<plugin>/``,
    ``qemu.log``, ``fastdyn_timing.jsonl``, ...); all machines of one
    configuration share it.
    """

    machine_name: str
    exit_code: int
    work_dir: Path


def prepare_work_dir(work_dir: Optional[str], persist_work_dir: bool) -> str:
    """Create the work directory, replacing an existing one unless it is persisted."""
    if work_dir is None:
        work_dir = DEFAULT_WORK_DIR

    if not persist_work_dir:
        if os.path.exists(work_dir):
            log.info(f"The output directory already exists at Path {os.path.abspath(work_dir)}. Deleting it!")
            shutil.rmtree(work_dir)
        log.info(f"Creating output directory at path: {os.path.abspath(work_dir)}")
        os.makedirs(work_dir)
    else:
        log.info("Running with existing work directory.")
    return work_dir


def configure_measurement(config, work_dir) -> dict[str, str]:
    """Set up timing and profiling for the run and log where their output goes."""
    env = runtime_config.configure_run_environment(config, work_dir)
    log.info(f"FastDyn timing log: {env['FASTDYN_TIMING_FILE']}")
    if env.get("FASTDYN_PYTHON_PROFILE") == "1" or env.get("FASTDYN_PERF_MODE") != "off":
        log.info(f"FastDyn profile output: {Path(work_dir).expanduser().resolve() / 'profiles'}")
    return env


def auto_build_fmu(config, fmu_name: Optional[str], skip_build: bool, report: Reporter = log.info) -> None:
    """Build the FMU named by the configuration's [FMU] table when it is out of date."""
    if skip_build:
        return
    try:
        build = fmu_build.resolve(Path(config), fmu_name)
    except fmu_build.NoFmuConfig:
        return
    except fmu_build.FmuConfigError as exc:
        raise RunError(str(exc)) from exc

    if not build.auto_build:
        return

    try:
        if not fmu_build.needs_build(build):
            report(f"FMU ready: {build.artifact_path}")
            return
        fmu_build.require_build_inputs(build)
        report(f"Building FMU '{build.name}' from {config}")
        fmu_build.build_fmu(build, update_submodules_first=False)
        if build.package:
            report(f"FMU ready: {build.fmu_path}")
        else:
            report(f"FMU source tree ready: {build.output}")
    except fmu_build.FmuConfigError as exc:
        raise RunError(f"FMU auto-build setup is incomplete: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise RunError(f"FMU build failed: {exc}") from exc


class Session:
    """A prepared configuration whose helper processes and machines have not started yet."""

    def __init__(self, config, work_dir: str, svd_path: str, fmu: Optional[str], run_processes: bool):
        self.config = config
        self.work_dir = work_dir
        self.svd_path = svd_path
        self.fmu = fmu
        self.run_processes = run_processes

    def run(self, adjust: Optional[Callable[[Fastdyn], None]] = None) -> list[RunResult]:
        """Start helper processes, parse the configuration, and run every machine.

        ``adjust`` receives the parsed :class:`Fastdyn` handle before any
        machine starts, so callers can change machines without a new TOML.
        """
        results = []
        with runtime_config.launch_from_config(
            self.config, self.work_dir, skip=not self.run_processes
        ) as process_manager:
            with timing.phase("fastdyn.parse_config"):
                handle = toml_parser.parser(
                    self.work_dir,
                    machine_name="machine0",
                    toml_config=self.config,
                    svd_path=self.svd_path,
                    fmu_name=self.fmu,
                )
            if adjust is not None:
                adjust(handle)

            if process_manager is not None:
                process_manager.start_terminator_watcher(
                    lambda _handle, _exit_code: handle.shutdown()
                )
            try:
                for idx, _machine in enumerate(handle.machines):
                    machine_name = f"machine{idx}"
                    with timing.phase(f"fastdyn.{machine_name}.run"):
                        exit_code = handle.run(
                            machine_name=machine_name,
                            target="qemu",
                            out_path=self.work_dir,
                        )
                    results.append(RunResult(machine_name, exit_code, Path(self.work_dir).resolve()))
            finally:
                if process_manager is not None:
                    process_manager.stop_terminator_watcher()
                    process_manager.raise_for_terminator_failure()
        return results


def _prepare(config, work_dir, persist_work_dir, validate) -> str:
    if validate:
        # Validate before prepare_work_dir can replace an existing directory.
        runtime_config.validate_config(config)
    work_dir = prepare_work_dir(work_dir, persist_work_dir)
    configure_measurement(config, work_dir)
    return work_dir


def load(
    config,
    work_dir: Optional[str] = DEFAULT_WORK_DIR,
    *,
    svd: Optional[str] = None,
    persist_work_dir: bool = False,
    fmu: Optional[str] = None,
    build_fmu: bool = True,
    run_processes: bool = True,
    validate: bool = True,
    report: Reporter = log.info,
) -> Session:
    """Validate the configuration, prepare the work directory and build FMUs.

    Helper processes are started, and the configuration is parsed, only by
    :meth:`Session.run`, in the same order as ``fastdyn run``.
    """
    work_dir = _prepare(config, work_dir, persist_work_dir, validate)
    with timing.phase("fastdyn.fmu_auto_build"):
        auto_build_fmu(config, fmu, not build_fmu, report)
    return Session(config, work_dir, svd or paths.default_svd_dir(), fmu, run_processes)


def run_config(
    config,
    work_dir: Optional[str] = DEFAULT_WORK_DIR,
    *,
    svd: Optional[str] = None,
    persist_work_dir: bool = False,
    fmu: Optional[str] = None,
    build_fmu: bool = True,
    run_processes: bool = True,
    validate: bool = True,
    adjust: Optional[Callable[[Fastdyn], None]] = None,
    report: Reporter = log.info,
    phase_name: str = "fastdyn.run.total",
) -> list[RunResult]:
    """Run a configuration exactly like ``fastdyn run`` and return one result per machine.

    ``phase_name`` names the enclosing timing phase (``fastdyn loop`` uses
    ``fastdyn.loop.iteration``).
    """
    work_dir = _prepare(config, work_dir, persist_work_dir, validate)
    with timing.phase(phase_name, config=config, work_dir=work_dir):
        with timing.phase("fastdyn.fmu_auto_build"):
            auto_build_fmu(config, fmu, not build_fmu, report)
        session = Session(config, work_dir, svd or paths.default_svd_dir(), fmu, run_processes)
        return session.run(adjust)
