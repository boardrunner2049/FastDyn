'''
Main file is responsible for kicking the qemu command.
'''
import logging
import click
import os, shutil
import json
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

from . import logs
from fastdyn.__init__ import __version__
# from .machine import Machine, CPUConfig, VirtualInstruction, InstructionModifier
from . import toml_parser
from . import runtime_config
from . import paths
from . import runner
from . import timing
from .utils import parse_config as parse_helper
from . import platform_browser
from .virtual_preprocessing import VirtualPreparationError
import fastdyn.targets.qemu_target as qemu_target
log = logging.getLogger(__name__)


def _auto_build_fmu(config, fmu_name, skip_build):
    # CLI adapter (used by swarm): FMU problems become CLI errors.
    try:
        runner.auto_build_fmu(config, fmu_name, skip_build, report=click.echo)
    except runner.RunError as exc:
        raise click.ClickException(str(exc)) from exc

def _log_settings(verbose, quiet, log_levels):
    """Turn -v/-q/--log-level into a default level and per-logger levels."""
    if verbose and quiet:
        raise click.UsageError("-v/--verbose and -q/--quiet cannot be combined.")
    default = "DEBUG" if verbose else "WARNING" if quiet else None
    overrides = {}
    for option in log_levels:
        name, separator, level = option.rpartition("=")
        try:
            logs.level_number(level)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--log-level") from exc
        if separator:
            overrides[name] = level
        else:
            default = level
    return default, overrides


@click.group()
@click.version_option(prog_name="Fastdyn Framework",version=__version__)
@click.option('-v', '--verbose', is_flag=True, help='Show debug messages.')
@click.option('-q', '--quiet', is_flag=True, help='Show only warnings and errors.')
@click.option('--log-level', 'log_levels', multiple=True, metavar='[LOGGER=]LEVEL',
              help='Set the log level, or one logger\'s level (e.g. fastdyn.targets=DEBUG). Repeatable. '
                   'The default comes from FASTDYN_LOG_LEVEL, else INFO.')
@click.option('--log-file', type=click.Path(dir_okay=False, writable=True), default=None,
              help='Also write every log message (DEBUG and above) to this file.')
def cli(verbose, quiet, log_levels, log_file):
    default, overrides = _log_settings(verbose, quiet, log_levels)
    logs.configure(level=default, levels=overrides, log_file=log_file)
    log.debug('****** Fastdyn Framework {0} *******'.format(__version__ ))


def _show_platform_selection(selected):
    if isinstance(selected, platform_browser.ArchitectureEntry):
        click.echo(f'Selected architecture target: {selected.name}')
        click.echo('  [[CPU.cpu0]]')
        click.echo(f'  arch = "{selected.architecture}"')
        click.echo(f'  machine = "{selected.machine}"')
        click.echo(f'  cpu = "{selected.cpu}"')
    elif isinstance(selected, platform_browser.PlatformEntry):
        click.echo(f'Selected platform: {selected.name}')
        click.echo(f'  [Machine] platform = "{selected.name}"')
        click.echo(f'  SVD: {selected.path}')
    elif isinstance(selected, platform_browser.DocumentationEntry):
        click.echo(f'Documentation: {selected.path}')
        click.echo(selected.description)


def _show_feature_selection(selected):
    if isinstance(selected, platform_browser.DocumentationEntry):
        click.echo(f'Documentation: {selected.path}')
        click.echo(selected.description)
        return
    click.echo(f'Selected {selected.kind.lower()}: {selected.name}')
    click.echo(selected.description)
    click.echo('')
    click.echo(selected.toml)
    click.echo(f'\nDocumentation: {selected.documentation}')


@cli.group('help', invoke_without_command=True)
@click.option('--browse/--no-browse', default=None,
              help='Force or disable the interactive configuration-help menu.')
@click.option('-s', '--svd', default='third_party/common/cmsis-svd-data',
              type=click.Path(resolve_path=True, exists=True), metavar='PATH',
              help='CMSIS-SVD file or catalog directory used by the top-level browser.')
@click.pass_context
def help_command(ctx, browse, svd):
    """Browse configuration choices and obtain ready-to-copy TOML."""
    if ctx.invoked_subcommand is None:
        if browse is None:
            browse = sys.stdin.isatty() and sys.stdout.isatty()
        if not browse:
            click.echo(ctx.get_help())
            return
        from . import feature_browser, help_browser
        try:
            selected = help_browser.browse_help(svd)
        except (RuntimeError, parse_helper.SvdResolutionError) as exc:
            raise click.ClickException(str(exc)) from exc
        if isinstance(selected, (platform_browser.ArchitectureEntry, platform_browser.PlatformEntry)):
            _show_platform_selection(selected)
        elif isinstance(selected, feature_browser.FeatureEntry):
            _show_feature_selection(selected)
        elif isinstance(selected, platform_browser.DocumentationEntry):
            _show_platform_selection(selected)


@help_command.command(
    'platforms',
    help=(
        'Browse FastDyn architecture presets and CMSIS-SVD platform names. '
        'On an interactive terminal, opens an inline vendor/platform browser. '
        'Pass a vendor or platform fragment for plain-text output, for example: fastdyn help platforms STM32.'
    ),
)
@click.argument('query', required=False, metavar='VENDOR_OR_PLATFORM')
@click.option(
    '--browse/--no-browse',
    default=None,
    help='Force or disable the inline platform browser.',
)
@click.option(
    '-s', '--svd',
    type=click.Path(resolve_path=True, exists=True),
    default='third_party/common/cmsis-svd-data',
    show_default=True,
    metavar='PATH',
    help='CMSIS-SVD file or catalog directory to inspect.',
)
def platforms(query, browse, svd):
    """Make valid SVD ``platform`` values discoverable from the CLI."""
    try:
        all_platforms = parse_helper.list_svd_platforms(svd)
    except parse_helper.SvdResolutionError as exc:
        raise click.ClickException(str(exc)) from exc

    if browse is None:
        browse = not query and sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = platform_browser.browse_platforms(all_platforms)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        _show_platform_selection(selected)
        return

    click.echo('FastDyn architecture presets used by the bundled configurations:')
    click.echo('  arm      Cortex-M and Cortex-A QEMU targets')
    click.echo('  riscv64  QEMU virt / rv64 target')
    click.echo('  x86_64   QEMU base_generic / qemu64 target')
    click.echo('')

    matches = parse_helper.find_svd_platforms(svd, query) if query else all_platforms
    if query:
        if not matches:
            click.echo(f"No SVD platforms match {query!r}.")
            click.echo('Available vendors:')
            vendors = sorted({vendor for vendor, _platform, _path in all_platforms}, key=str.casefold)
            click.echo(f"  {', '.join(vendors)}")
            return

        grouped = defaultdict(list)
        for vendor, platform, _path in matches:
            grouped[vendor].append(platform)

        click.echo(f"{len(matches)} SVD platform(s) match {query!r}:")
        for vendor in sorted(grouped, key=str.casefold):
            click.echo(f"  {vendor} ({len(grouped[vendor])}):")
            for platform in grouped[vendor]:
                click.echo(f"    {platform}")
        return

    grouped = defaultdict(list)
    for vendor, platform, _path in all_platforms:
        grouped[vendor].append(platform)

    unique_platforms = {platform.casefold() for _vendor, platform, _path in all_platforms}
    click.echo(
        f"CMSIS-SVD catalog: {len(unique_platforms)} unique platform identifier(s) "
        f"from {len(all_platforms)} SVD file(s) across {len(grouped)} vendor(s)."
    )
    click.echo('Vendors (platform files):')
    for vendor in sorted(grouped, key=str.casefold):
        click.echo(f"  {vendor}: {len(grouped[vendor])}")
    click.echo('')
    click.echo('Browse interactively with: fastdyn help platforms --browse')
    click.echo('List a family or vendor with: fastdyn help platforms STM32')


@help_command.command(
    'virtuals',
    help=(
        'Browse user-configurable FastDyn virtual instructions and run-wide plugins. '
        'An interactive terminal opens an inline picker with ready-to-copy TOML.'
    ),
)
@click.option(
    '--browse/--no-browse',
    default=None,
    help='Force or disable the inline virtual/plugin browser.',
)
def virtuals(browse):
    """Make virtual and plugin TOML configuration discoverable from the CLI."""
    from . import feature_browser

    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = feature_browser.browse_features()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if selected:
            _show_feature_selection(selected)
        return

    entries = feature_browser.all_entries()
    for kind in ("Virtual instruction", "Run-wide plugin"):
        grouped = [entry for entry in entries if entry.kind == kind]
        click.echo(f'{kind}s:')
        for entry in grouped:
            click.echo(f'  {entry.name:<22} {entry.description}')
        click.echo('')
    click.echo('Browse interactively with: fastdyn help virtuals --browse')


help_command.add_command(virtuals, 'plugins')


@help_command.command(
    'modifiers',
    help='Browse modifier forms and show ready-to-copy TOML examples.',
)
@click.option(
    '--browse/--no-browse',
    default=None,
    help='Force or disable the inline modifier browser.',
)
def modifiers(browse):
    """Make modifier TOML configuration discoverable from the CLI."""
    from . import feature_browser

    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = feature_browser.browse_modifiers()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if selected:
            _show_feature_selection(selected)
        return

    click.echo('Modifier forms:')
    for entry in feature_browser.modifier_entries():
        click.echo(f'  {entry.name:<30} {entry.description}')
    click.echo('\nBrowse interactively with: fastdyn help modifiers --browse')


@help_command.command(
    'device-models',
    help='Browse peripheral device-model forms and show ready-to-copy TOML examples.',
)
@click.option(
    '--browse/--no-browse',
    default=None,
    help='Force or disable the inline device-model browser.',
)
def device_models(browse):
    """Make peripheral device-model TOML configuration discoverable from the CLI."""
    from . import feature_browser

    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = feature_browser.browse_device_models()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if selected:
            _show_feature_selection(selected)
        return

    click.echo('Device models:')
    for entry in feature_browser.device_model_entries():
        click.echo(f'  {entry.name:<14} {entry.description}')
    click.echo('\nBrowse interactively with: fastdyn help device-models --browse')


@help_command.command('machine', help='Browse QEMU and run-wide [Machine] settings.')
@click.option('--browse/--no-browse', default=None,
              help='Force or disable the inline machine-settings browser.')
def machine_settings(browse):
    """Make essential Machine TOML settings discoverable from the CLI."""
    from . import feature_browser
    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = feature_browser.browse_machine()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if selected:
            _show_feature_selection(selected)
        return
    click.echo('Machine settings:')
    for entry in feature_browser.machine_entries():
        click.echo(f'  {entry.name:<28} {entry.description}')
    click.echo('\nBrowse interactively with: fastdyn help machine --browse')


@help_command.command('memory', help='Browse primary and additional memory-bank TOML settings.')
@click.option('--browse/--no-browse', default=None,
              help='Force or disable the inline memory browser.')
def memory(browse):
    """Make essential memory TOML settings discoverable from the CLI."""
    from . import feature_browser
    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = feature_browser.browse_memory()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if selected:
            _show_feature_selection(selected)
        return
    click.echo('Memory settings:')
    for entry in feature_browser.memory_entries():
        click.echo(f'  {entry.name:<28} {entry.description}')
    click.echo('\nBrowse interactively with: fastdyn help memory --browse')


@help_command.command('firmware', help='Browse firmware binary and CPU startup TOML settings.')
@click.option('--browse/--no-browse', default=None,
              help='Force or disable the inline firmware/CPU browser.')
def firmware(browse):
    """Make firmware and CPU startup TOML settings discoverable from the CLI."""
    from . import feature_browser
    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        try:
            selected = feature_browser.browse_firmware()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if selected:
            _show_feature_selection(selected)
        return
    click.echo('Firmware and CPU settings:')
    for entry in feature_browser.firmware_entries():
        click.echo(f'  {entry.name:<28} {entry.description}')
    click.echo('\nBrowse interactively with: fastdyn help firmware --browse')


@help_command.command(
    'run',
    help='Show the FastDyn run command, its primary flags, and the resulting work directory.',
)
@click.option('--browse/--no-browse', default=None,
              help='Force or disable the inline run-workflow browser.')
def run_help(browse):
    """Make the required ``fastdyn run -c CONFIG`` workflow discoverable."""
    from . import feature_browser
    if browse is None:
        browse = sys.stdin.isatty() and sys.stdout.isatty()
    if browse:
        selected = feature_browser.browse_run()
        if selected:
            _show_feature_selection(selected)
        return
    click.echo('Run FastDyn:')
    for entry in feature_browser.run_entries():
        click.echo(f'  {entry.name:<34} {entry.description}')
        click.echo(textwrap.indent(entry.toml, '    '))
    click.echo('\nBrowse interactively with: fastdyn help run --browse')


@cli.command('run',help= 'Runs the firmware on QEMU using the passed config file.')
@click.option('-c','--config',required = True, type= click.Path(resolve_path=True,exists=True),
                        help='The Path to the config file.',
                        metavar= 'PATH')
@click.option(
    '-m', '--map-file', #TODO: Remove this and from the codebase, deadcode
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the symbol map file.',
    default=None,
    metavar='PATH'
)
@click.option('-o','--work-dir',default="./fastdyn_work",metavar='PATH',
        show_default=True,
        type=click.Path(resolve_path=True,writable=True),
        help='Path to the work directory.')
@click.option(
    '-s', '--svd',
    type=click.Path(resolve_path=True, exists=True),
    default=None,
    metavar='PATH',
    help='Optional path to an SVD file or directory.'
)
@click.option(
    '-p', '--persist-work-dir',
    is_flag=True,
    default=False,
    help='Optional Flag to persist the existing work directory.'
)
@click.option(
    '--fmu',
    default=None,
    metavar='NAME',
    help='Override [FMU].active for automatic FMU builds.'
)
@click.option(
    '--no-build-fmu',
    is_flag=True,
    default=False,
    help='Skip automatic FMU build from the [FMU] config.'
)
@click.option(
    '--no-run-processes',
    is_flag=True,
    default=False,
    help='Do not start helper processes from [Rumoca] or [Run.processes].'
)
def run(config, map_file, work_dir, svd, persist_work_dir, fmu, no_build_fmu,
        no_run_processes):
    try:
        runner.run_config(
            config,
            work_dir,
            svd=svd,
            persist_work_dir=persist_work_dir,
            fmu=fmu,
            build_fmu=not no_build_fmu,
            run_processes=not no_run_processes,
            report=click.echo,
        )
    except (runner.RunError, runtime_config.RuntimeConfigError, VirtualPreparationError,
            qemu_target.QemuExitError) as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        log.info("Run interrupted by user.")
        raise SystemExit(130)  # conventional exit status for SIGINT


@cli.command('loop', help='Like run, but restarts automatically: after a clean exit with a fresh work directory, after QEMU exits with an error or a helper process dies with the work directory kept for inspection. Configuration and setup errors stop the loop.')
@click.option('-c','--config',required = True, type= click.Path(resolve_path=True,exists=True),
                        help='The Path to the config file.',
                        metavar= 'PATH')
@click.option(
    '-m', '--map-file',
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the symbol map file.',
    default=None,
    metavar='PATH'
)
@click.option('-o','--work-dir',default="./fastdyn_work",metavar='PATH',
        show_default=True,
        type=click.Path(resolve_path=True,writable=True),
        help='Path to the work directory.')
@click.option(
    '-s', '--svd',
    type=click.Path(resolve_path=True, exists=True),
    default=None,
    metavar='PATH',
    help='Optional path to an SVD file or directory.'
)
@click.option(
    '-p', '--persist-work-dir',
    is_flag=True,
    default=False,
    help='Optional Flag to persist the existing work directory on the first run.'
)
@click.option(
    '--fmu',
    default=None,
    metavar='NAME',
    help='Override [FMU].active for automatic FMU builds.'
)
@click.option(
    '--no-build-fmu',
    is_flag=True,
    default=False,
    help='Skip automatic FMU build from the [FMU] config.'
)
@click.option(
    '--no-run-processes',
    is_flag=True,
    default=False,
    help='Do not start helper processes from [Rumoca] or [Run.processes].'
)
def loop(config, map_file, work_dir, svd, persist_work_dir, fmu, no_build_fmu, no_run_processes):
    # Restart only when a run failed (QEMU exited with an error, or a helper
    # died mid-run), keeping the work dir for inspection. A clean exit starts
    # the next iteration with a fresh work dir. Anything else is a setup or
    # configuration problem that would fail again, so the loop stops.
    persist = persist_work_dir
    first = True
    while True:
        try:
            runner.run_config(
                config,
                work_dir,
                svd=svd,
                persist_work_dir=persist,
                fmu=fmu,
                build_fmu=first and not no_build_fmu,  # FMUs are built once
                run_processes=not no_run_processes,
                report=click.echo,
                phase_name="fastdyn.loop.iteration",
            )
            log.info("Run finished; restarting with a fresh work directory.")
            persist = False
        except (qemu_target.QemuExitError, runtime_config.HelperExitedError) as exc:
            log.error(f"Run failed: {exc}. Restarting with the work directory preserved.")
            persist = True
        except (runner.RunError, runtime_config.RuntimeConfigError, VirtualPreparationError) as exc:
            raise click.ClickException(str(exc)) from exc
        except KeyboardInterrupt:
            log.info("Loop interrupted by user.")
            raise SystemExit(130)  # conventional exit status for SIGINT
        first = False


def _fastdyn_executable() -> str:
    executable = shutil.which("fastdyn") or sys.argv[0]
    path = Path(executable).expanduser()
    return str(path.resolve()) if path.exists() else executable


@cli.command(
    'swarm',
    help='Run multiple isolated FastDyn instances with per-worker ports and work directories.',
)
@click.option('-c', '--config', required=True, type=click.Path(resolve_path=True, exists=True),
              help='The path to the config file.', metavar='PATH')
@click.option('-n', '--instances', default=1, show_default=True, type=int,
              help='Number of FastDyn instances to launch.')
@click.option('-o', '--work-dir-root', default="./fastdyn_swarm", show_default=True,
              type=click.Path(resolve_path=True, writable=True), metavar='PATH',
              help='Root directory for worker work directories.')
@click.option('--base-port', default=15000, show_default=True, type=int,
              help='First worker monitor port. Other ports are assigned from this base.')
@click.option('--port-stride', default=20, show_default=True, type=int,
              help='Port spacing between workers.')
@click.option('--jobs', default=None, type=int,
              help='Maximum workers to run at once. Defaults to --instances.')
@click.option('--loop', 'use_loop', is_flag=True, default=False,
              help='Run each worker with `fastdyn loop` instead of one `fastdyn run`.')
@click.option('--fmu', default=None, metavar='NAME',
              help='Override [FMU].active for automatic FMU builds.')
@click.option('--no-build-fmu', is_flag=True, default=False,
              help='Skip the one-time FMU build before launching workers.')
@click.option('--no-run-processes', is_flag=True, default=False,
              help='Do not start helper processes from [Rumoca] or [Run.processes] in workers.')
@click.option('--timeout-sec', default=None, type=float,
              help='Terminate the swarm if it runs longer than this many seconds.')
@click.option('--smoke-sec', default=None, type=float,
              help='Launch all workers, require them to stay alive for this many seconds, then stop them cleanly.')
@click.option('--stop-on-failure', is_flag=True, default=False,
              help='Stop launching new workers after the first non-zero worker exit.')
@click.option('--skip-port-check', is_flag=True, default=False,
              help='Skip the preflight check for TCP/UDP port conflicts.')
@click.option('--dry-run', is_flag=True, default=False,
              help='Print worker commands and port assignments without launching them.')
def swarm(
    config,
    instances,
    work_dir_root,
    base_port,
    port_stride,
    jobs,
    use_loop,
    fmu,
    no_build_fmu,
    no_run_processes,
    timeout_sec,
    smoke_sec,
    stop_on_failure,
    skip_port_check,
    dry_run,
):
    # Feature-only import, kept here so other commands do not depend on swarm.
    from . import swarm as swarm_runner
    runner = "loop" if use_loop else "run"
    jobs = instances if jobs is None else jobs

    try:
        plans = swarm_runner.build_worker_plans(
            config=config,
            root_dir=work_dir_root,
            instances=instances,
            base_port=base_port,
            port_stride=port_stride,
            fastdyn_executable=_fastdyn_executable(),
            runner=runner,
            fmu=fmu,
            no_run_processes=no_run_processes,
        )
        if not skip_port_check and not dry_run:
            swarm_runner.check_port_availability(plans)
    except swarm_runner.SwarmError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"FastDyn swarm: instances={instances} jobs={jobs} runner={runner} "
        f"root={Path(work_dir_root).expanduser().resolve()}"
    )
    for plan in plans:
        click.echo(
            f"worker {plan.index:03d}: monitor={plan.ports.monitor} "
            f"mavlink={plan.ports.mavlink_firmware}/{plan.ports.mavlink_gcs} "
            f"mavcesium={plan.ports.mavcesium_url} "
            f"rumoca={plan.ports.rumoca_http}/{plan.ports.rumoca_ws} "
            f"memory={plan.env['FASTDYN_QEMU_MEMORY_DIR']}"
        )
        if dry_run:
            click.echo(f"  command: {' '.join(plan.command)}")

    if dry_run:
        return

    if not no_build_fmu:
        with timing.phase("fastdyn.swarm.fmu_auto_build"):
            _auto_build_fmu(config, fmu, skip_build=False)

    try:
        if smoke_sec is not None:
            swarm_runner.smoke_worker_plans(
                plans,
                jobs=jobs,
                smoke_sec=smoke_sec,
                echo=click.echo,
            )
            results = []
        else:
            results = swarm_runner.run_worker_plans(
                plans,
                jobs=jobs,
                timeout_sec=timeout_sec,
                stop_on_failure=stop_on_failure,
                echo=click.echo,
            )
    except swarm_runner.SwarmError as exc:
        raise click.ClickException(str(exc)) from exc

    failed = [result for result in results if result.returncode != 0]
    if failed:
        details = ", ".join(f"{result.plan.index:03d}:rc={result.returncode}" for result in failed)
        raise click.ClickException(f"{len(failed)} worker(s) failed: {details}")


@cli.command('build-model', help='Compile device model sources into shared libraries (one per .c file).')
@click.argument('sources', nargs=-1, required=True, type=click.Path(exists=True), metavar='PATH...')
@click.option('-o', '--out-dir', required=True, type=click.Path(file_okay=False), metavar='DIR',
              help='Where to write <name>.so.')
@click.option('-r', '--recursive', is_flag=True, help='Build every .c file under the given folders.')
@click.option('--name', metavar='NAME', help='Build the given files as one model called NAME.')
@click.option('-j', '--jobs', type=click.IntRange(min=1), default=None, metavar='N',
              help='Parallel builds (default: number of CPUs).')
@click.option('-k', '--keep-going', is_flag=True, help='Keep building the other models after a failure.')
@click.option('--exclude', multiple=True, metavar='PATTERN', help='Skip matching files (repeatable).')
@click.option('--no-vio', is_flag=True, help='Build without LibVIO.')
@click.option('--pkg', 'pkgs', multiple=True, metavar='NAME',
              help='Add a pkg-config package (repeatable); "fastdyn" links the fuzzer hooks.')
@click.option('--json', 'as_json', is_flag=True, help='Print a machine-readable report.')
def build_model(sources, out_dir, recursive, name, jobs, keep_going, exclude, no_vio, pkgs, as_json):
    # Imported here so other commands do not pay for it.
    from . import model_build

    try:
        if name is not None:
            inputs = model_build.find_models(sources, name=name)
        else:
            inputs = list(sources)
        report = model_build.build_models(
            inputs, out_dir, jobs=jobs, keep_going=keep_going, vio=not no_vio,
            pkgs=pkgs, exclude=exclude, recursive=recursive,
        )
    except model_build.ModelBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        labels = {"built": "built", "up-to-date": "up to date", "failed": "FAILED", "skipped": "skipped"}
        for result in report.results:
            line = f"{labels[result.status]:<11} {result.name}"
            if result.status in ("built", "up-to-date"):
                line += f" -> {result.output}"
            elif result.status == "failed":
                line += f": {result.first_error}"
            click.echo(line)
        click.echo(report.summary())
    if not report.ok:
        raise SystemExit(1)


@cli.command('timing-summary', help='Summarize a FastDyn timing JSONL file.')
@click.argument('timing_file', type=click.Path(resolve_path=True, exists=True), metavar='PATH')
@click.option('--top', default=30, show_default=True, type=int, help='Number of slowest phases to show.')
def timing_summary(timing_file, top):
    import json

    phases = []
    marks = 0
    with open(timing_file, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == "phase_end":
                phases.append(record)
            elif record.get("event") == "mark":
                marks += 1

    phases.sort(key=lambda item: float(item.get("duration_s", 0.0)), reverse=True)
    click.echo(f"Timing file: {timing_file}")
    click.echo(f"Completed phases: {len(phases)}  marks: {marks}")
    click.echo("")
    click.echo(f"{'duration':>10}  {'process':<16}  phase")
    click.echo(f"{'-' * 10}  {'-' * 16}  {'-' * 60}")
    for record in phases[:top]:
        duration = float(record.get("duration_s", 0.0))
        process = str(record.get("process", ""))[:16]
        phase = record.get("phase", "")
        status = record.get("status", "ok")
        suffix = "" if status == "ok" else f" ({status})"
        click.echo(f"{duration:9.3f}s  {process:<16}  {phase}{suffix}")


@cli.command(
    'generate',
    help='Generates the LLM Prompt using the hardware log passed by the user. '
         '[Disclaimer: Use this when generating a model for the first time]'
)
@click.option(
    '-hw', '--hardware-log',
    required=False,
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the log file generated when running the firmware on hardware.',
    metavar='PATH'
)
@click.option(
    '-sm', '--slave-model',
    is_flag=True,
    help='Generate a slave model instead of the main model.'
)
@click.option(
    '-rm', '--reference-model',
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the reference model file.',
    metavar='PATH'
)
@click.option(
    '-fc', '--firmware-code',
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the firmware source code for slave model.',
    metavar='PATH'
)
@click.option(
    '-b', '--board',
    type=str,
    required=True,
    help='Name of the platform on which the firmware is running.'
)
@click.option(
    '-p', '--peripheral',
    type=str,
    multiple=True,
    required=True,
    help='Name of the peripheral targeted for verification.'
)
@click.option(
    '-mname', '--model-name',
    type=str,
    required=False,
    help='Name of the generated model you want to pass to LLM.'
)
@click.option(
    '--method',
    default='ngram',
    show_default=True,
    type=click.Choice(['ngram', 'window', 'other'], case_sensitive=False),
    help='Context minimization method.'
)
@click.option(
    '--n',
    type=int,
    default=2,
    show_default=True,
    help='Value of n for n-gram method.'
)
@click.option(
    '--isr-window',
    type=int,
    default=10000000,
    show_default=True,
    help='ISR window size.'
)
@click.option(
    '-o', '--work-dir',
    default="./fastdyn_work",
    show_default=True,
    metavar='PATH',
    type=click.Path(resolve_path=True, writable=True),
    help='Path to the work directory.'
)
@click.option(
    '-s', '--svd',
    type=click.Path(resolve_path=True, exists=True),
    default=None,
    metavar='PATH',
    help='Optional path to an SVD file or directory.'
)
@click.option(
    '-ms', '--model-source',
    'model_sources',
    type=str,
    multiple=True,
    required=False,
    help='Peripheral(s) whose trace data is the primary source for the target model. '
         'Required when --model-name does not match or contain any peripheral name. '
         'Example: -ms DMA1 -ms DMAMUX1'
)
@click.option(
    '--no-vio',
    is_flag=True,
    default=False,
    help='Ablation mode: strip VIO API definitions from the generated prompt, '
         'providing only base QEMU/FastDyn APIs (raw MMIO callbacks).'
)
@click.option(
    '--no-encoder',
    is_flag=True,
    default=False,
    help='Ablation mode: skip the Encoder (context minimizer). Feed the raw I/O trace '
         'directly to the LLM prompt instead of the compact automaton.'
)
def generate(hardware_log, slave_model, reference_model, firmware_code, board, peripheral, model_name,
             method, n, isr_window, work_dir, svd, model_sources, no_vio, no_encoder):
    """Generates the LLM Prompt using the hardware log passed by the user."""
    # Feature-only imports: keeping them here means core commands such as
    # `fastdyn run` do not need these modules or their dependencies (scipy).
    from .verifier import context_minimizer as cm
    from .verifier import prompt_gen as pg
    if slave_model:
        if reference_model is None:
            raise click.UsageError("--slave-model requires --reference-model.")
        if firmware_code is None:
            raise click.UsageError("--slave-model requires --firmware-code.")

        if work_dir is not None:
            if not os.path.isdir(work_dir):
                log.warning(f"The output directory: {work_dir} passed by the user does not exist.")
        else:
            work_dir = "fastdyn_work"

        #generate the prompt
        peripheral_str = ", ".join(peripheral)
        slave_type = "spi" if any("spi" in p.lower() for p in peripheral) else "i2c"
        pg_path = pg.slave_model_gen(
            peripheral_name=peripheral_str,
            platform_name=board,
            out_dir=work_dir,
            slave_firmware_path=firmware_code,
            reference_model_path=reference_model,
            slave_type=slave_type
        )

    else:
        if hardware_log is None:
            raise click.UsageError("Generating the model required --hardware-log is required.")

        if work_dir is not None:
            if not os.path.isdir(work_dir):
                log.warning(f"The output directory: {work_dir} passed by the user does not exist.")
        else:
            work_dir = "fastdyn_work"

        if os.path.exists(work_dir):
            log.info(f"The output directory already exists at Path {os.path.abspath(work_dir)}. Deleting it!")
            shutil.rmtree(work_dir)

        log.info(f"Creating output directory at path: {os.path.abspath(work_dir)}")

        os.makedirs(work_dir)

        #discover svd
        svd_input = svd if svd is not None else paths.default_svd_dir()
        platform = (board or "").strip()
        if not platform and (svd is None):
            raise click.UsageError(
                f"Machine platform is required when CMSIS SVD file path is not explicitly given. "
                "Pass --board explicitly."
            )

        if not platform and (svd_input and os.path.isdir(os.path.expanduser(svd_input))):
            raise click.UsageError(
                f"Machine platform is required when CMSIS SVD path points to a directory. "
                "Pass --svd <file> explicitly"
                "OR just Pass --board explicitly."
            )

        try:
            svd_file, svd_key = parse_helper.resolve_svd(
                platform_or_board=platform or "unused",
                svd=svd_input,          # user explicitly passed it here
                default_dir=None,       # no default in this path
                auto_discover=False,    # don’t do repo search when user already gave a path
            )
        except parse_helper.SvdResolutionError as e:
            raise click.ClickException(str(e)) from e

        log.info(f"Using SVD: {svd_file} (key='{svd_key}')")
        svd_device = parse_helper.get_svd_device(svd_file)

        if no_encoder:
            # Ablation A1: Skip the Encoder entirely. Generate prompt from raw I/O trace.
            log.info("Ablation mode: --no-encoder enabled. Skipping Encoder, using raw I/O trace.")
            from fastdyn.verifier.prompt_gen import generate_prompt_no_encoder
            final_prompt = generate_prompt_no_encoder(
                hardware_log=hardware_log,
                peripheral_name=peripheral[0],
                platform_name=platform,
                no_vio=no_vio,
                model_name=model_name,
                model_sources=model_sources,
            )
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            output_path = Path(work_dir) / "initial_prompt.txt"
            output_path.write_text(final_prompt + "\n", encoding="utf-8")
            log.info(f"Ablation prompt (no encoder) written to {output_path}")
        else:
            #minimize the context -- since all the other peripherals are also part of the model, we dont need to do it multiple times
            cm_path = cm.minimize_context(
                out_dir=work_dir,
                log_file=hardware_log,
                platform=platform,
                method=method,
                peripheral=peripheral[0],
                n=n,
                svd_device=svd_device,
                isr_window=isr_window,
                cm_dir_name="out_cm"
                )

            #generate the prompt - we will generate a prompt such that it covers all the peripherals requested by the user
            #TODO: Refactor and clean later
            if len(peripheral) > 1:
                for periph in peripheral:
                    periph_path = os.path.join(cm_path, periph)
                    if not os.path.exists(periph_path):
                        log.error(f"Requested Peripheral {periph} does not exist!")
                        sys.exit(1)

                # Validate model_sources if explicitly provided
                if model_sources:
                    for ms in model_sources:
                        if ms not in peripheral:
                            log.error(
                                f"--model-source '{ms}' is not in the peripheral list {list(peripheral)}. "
                                f"Only peripherals passed with -p can be used as a model source."
                            )
                            sys.exit(1)

                pg_path = pg.initial_prompt_gen_multiple_periphs(
                    analysis_dir=cm_path,
                    model_name=model_name,
                    peripherals=peripheral,
                    model_sources=model_sources or None,   # pass None so the function applies its heuristic
                    out_dir=work_dir,
                    no_vio=no_vio,
                    user_obs=''
                )
            else:
                pg_path = pg.initial_prompt_gen(
                    analysis_dir=cm_path,
                    peripheral=peripheral[0],
                    out_dir=work_dir,
                    no_vio=no_vio
                )

@cli.command(
    'verifier',
    help=(
        'Verifies the model passed by the user for a given peripheral. '
        'Generates a prompt in case of failure. '
        '[Disclaimer: Use this when iterating a generated model for correction]'
    )
)
@click.option(
    '-hw', '--hardware-log',
    required=True,
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the log file generated when running the firmware on hardware.',
    metavar='PATH'
)
@click.option(
    '-em', '--emulation-log',
    required=True,
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the log file generated when running the firmware on the elder model using emulation.',
    metavar='PATH'
)
@click.option(
    '-d', '--device-model',
    'device_models',
    type=str,
    multiple=True,
    required=True,
    help='Model name and path as NAME:PATH. '
         'Example: -d ADC1:model.c -d DMA_with_DMAMUX1:model2.c'
)
@click.option(
    '-b', '--board',
    type=str,
    required=True,
    help='Name of the platform on which the firmware is running.'
)
@click.option(
    '-p', '--peripheral',
    type=str,
    multiple=True,
    required=True,
    help='Name of the peripheral targeted for verification.'
)
@click.option(
    '-mname', '--model-name',
    'model_names',
    type=str,
    multiple=True,
    required=True,
    help='Name(s) of the model(s) being verified.'
)
@click.option(
    '--method',
    default='ngram',
    show_default=True,
    type=click.Choice(['ngram', 'window', 'other'], case_sensitive=False),
    help='Context minimization method.'
)
@click.option(
    '--n',
    type=int,
    default=2,
    show_default=True,
    help='Value of n for n-gram method.'
)
@click.option(
    '--isr-window',
    type=int,
    default=10000000,
    show_default=True,
    help='ISR window size.'
)
@click.option(
    '-o', '--work-dir',
    default="./fastdyn_work",
    show_default=True,
    metavar='PATH',
    type=click.Path(resolve_path=True, writable=True),
    help='Path to the work directory.'
)
@click.option(
    '-s', '--svd',
    type=click.Path(resolve_path=True, exists=True),
    default=None,
    metavar='PATH',
    help='Optional path to an SVD file or directory.'
)
@click.option(
    '--no-rca',
    is_flag=True,
    default=False,
    help='Ablation mode: on mismatch, generate a generic retry prompt instead of '
         'the targeted RCA-based revised_prompt.txt.'
)
@click.option(
    '--no-encoder',
    is_flag=True,
    default=False,
    help='Ablation mode: on mismatch, generate a correction prompt using raw I/O '
         'trace data instead of the Encoder\'s structured analysis.'
)
@click.option(
    '--no-vio',
    is_flag=True,
    default=False,
    help='Ablation mode: strip VIO API definitions from the correction prompt.'
)
@click.option(
    '--no-verifier',
    is_flag=True,
    default=False,
    help='Ablation mode: on mismatch, provide only the encoded hardware trace and '
         'the broken model source code. No emulation trace feedback — the LLM must '
         'self-diagnose by comparing its code against the hardware evidence.'
)
@click.option(
    '--ms-map',
    'ms_map',
    type=str,
    multiple=True,
    metavar='MNAME=PERIPH1,PERIPH2,...',
    help='Per-model source-peripheral mapping. Required for --no-rca and '
         '--no-verifier in compositional (multi --mname) mode so each model '
         'is corrected in isolation. Repeat once per --mname. '
         'Example: --ms-map "ADC=ADC1,ADC2,ADC12_Common" --ms-map "DMA_with_DMAMUX1=DMA1,DMAMUX1"'
)
def verifier(hardware_log, emulation_log, device_models, model_names, board,
             peripheral, method, n, isr_window, work_dir, svd,
             no_rca, no_encoder, no_vio, no_verifier, ms_map):
    """Verifies the model against hardware and emulation logs."""
    # Feature-only imports: keeping them here means core commands such as
    # `fastdyn run` do not need these modules or their dependencies (scipy).
    from .verifier import context_minimizer as cm
    from .verifier import prompt_gen as pg
    from .verifier import verifier as verify
    log.info("Running Verifier")

    # ── Work directory setup ─────────────────────────────────────────────────
    if work_dir is not None:
        if not os.path.isdir(work_dir):
            log.warning(f"The output directory: {work_dir} passed by the user does not exist.")
    else:
        work_dir = "fastdyn_work"

    if os.path.exists(work_dir):
        log.info(f"The output directory already exists at Path {os.path.abspath(work_dir)}. Deleting it!")
        shutil.rmtree(work_dir)

    log.info(f"Creating output directory at path: {os.path.abspath(work_dir)}")
    os.makedirs(work_dir)

    # ── Parse --ms-map MNAME=PERIPH1,PERIPH2,... entries into a dict ─────────
    model_sources_map = {}
    for entry in ms_map:
        if '=' not in entry:
            log.error(
                f"--ms-map '{entry}' must be in MNAME=PERIPH1,PERIPH2,... format.\n"
                f"Example: --ms-map \"ADC=ADC1,ADC2,ADC12_Common\""
            )
            sys.exit(1)
        mname, sources_csv = entry.split('=', 1)
        mname = mname.strip()
        sources = [s.strip() for s in sources_csv.split(',') if s.strip()]
        if not sources:
            log.error(f"--ms-map entry for '{mname}' has no peripherals listed.")
            sys.exit(1)
        model_sources_map[mname] = sources

    # ── Parse and validate NAME:PATH device model entries ───────────────────
    model_to_path = {}
    for entry in device_models:
        if ':' not in entry:
            log.error(
                f"--device-model '{entry}' must be in NAME:PATH format.\n"
                f"Example: -d ADC1:boardrunner/model.c"
            )
            sys.exit(1)
        name, path = entry.split(':', 1)
        name, path = name.strip(), path.strip()
        if not os.path.exists(path):
            log.error(f"Model file for '{name}' not found: {path}")
            sys.exit(1)
        model_to_path[name] = path

    for mname in model_names:
        if mname not in model_to_path:
            log.error(
                f"No -d entry found for model '{mname}'.\n"
                f"Add: -d {mname}:/path/to/model.c"
            )
            sys.exit(1)

    # ── SVD discovery ────────────────────────────────────────────────────────
    svd_input = svd if svd is not None else paths.default_svd_dir()
    platform = (board or "").strip()

    if not platform and (svd is None):
        raise click.UsageError(
            "Machine platform is required when CMSIS SVD file path is not explicitly given. "
            "Pass --board explicitly."
        )

    if not platform and (svd_input and os.path.isdir(os.path.expanduser(svd_input))):
        raise click.UsageError(
            "Machine platform is required when CMSIS SVD path points to a directory. "
            "Pass --svd <file> explicitly OR just Pass --board explicitly."
        )

    try:
        svd_file, svd_key = parse_helper.resolve_svd(
            platform_or_board=platform or "unused",
            svd=svd_input,
            default_dir=None,
            auto_discover=False,
        )
    except parse_helper.SvdResolutionError as e:
        raise click.ClickException(str(e)) from e

    log.info(f"Using SVD: {svd_file} (key='{svd_key}')")
    svd_device = parse_helper.get_svd_device(svd_file)

    # ── Context minimization — hardware ──────────────────────────────────────
    cm_path_hardware = cm.minimize_context(
        out_dir=work_dir,
        log_file=hardware_log,
        platform=platform,
        method=method,
        peripheral=peripheral[0],
        n=n,
        svd_device=svd_device,
        isr_window=isr_window,
        cm_dir_name="out_cm"
    )

    for periph in peripheral:
        periph_path = os.path.join(cm_path_hardware, periph)
        if not os.path.exists(periph_path):
            log.error(f"Requested peripheral '{periph}' not found in hardware context: {periph_path}")
            sys.exit(1)

    # ── Context minimization — emulation ─────────────────────────────────────
    cm_path_emulation = cm.minimize_context(
        out_dir=work_dir,
        log_file=emulation_log,
        platform=platform,
        method=method,
        peripheral=peripheral[0],
        n=n,
        svd_device=svd_device,
        isr_window=isr_window,
        cm_dir_name="emulation"
    )

    for periph in peripheral:
        periph_path = os.path.join(cm_path_emulation, periph)
        if not os.path.exists(periph_path):
            log.warning(f"Requested peripheral '{periph}' not found in emulation context: {periph_path}. Emulation likely crashed early.")

    # ── Verification and prompt generation ───────────────────────────────────
    if len(peripheral) == 1:
        not_match, diff_obj = verify.verify_automata(
            automata1=cm_path_hardware,
            automata2=cm_path_emulation,
            peripheral=peripheral[0]
        )

        if not_match:
            if no_verifier:
                # Ablation: No Verifier feedback. The LLM receives the original
                # encoder-based prompt (HW trace structured) + the broken model
                # source code + generic "fix it". No emulation trace, no diffs,
                # no runtime feedback at all — the LLM must self-diagnose.
                log.warning("Log mismatch! --no-verifier: generating blind retry prompt.")

                periph_dir = os.path.join(cm_path_hardware, peripheral[0])
                if os.path.isdir(periph_dir):
                    original_prompt = pg.generate_prompt(periph_dir)
                else:
                    original_prompt = ""
                    log.warning("Could not find encoder output for --no-verifier fallback.")

                model_source = ""
                model_path = list(model_to_path.values())[0]
                try:
                    with open(model_path, "r", encoding="utf-8") as mf:
                        model_source = mf.read()
                except Exception as e:
                    log.warning(f"Could not read model source for --no-verifier prompt: {e}")

                generic_prompt = (
                    original_prompt +
                    "\n\n## Current Model (Failed Verification)\n"
                    "The following model was generated but failed verification against the hardware trace.\n"
                    "```c\n" + model_source + "\n```\n\n"
                    "## Correction Required\n"
                    "The model above does not correctly reproduce the hardware behavior shown in the trace data. "
                    "Please analyze the hardware trace and the model source code, identify the bug, "
                    "and generate a corrected model.\n"
                )
                prompt_path = os.path.join(work_dir, "initial_prompt.txt")
                with open(prompt_path, "w", encoding="utf-8") as f:
                    f.write(generic_prompt)
                log.info("No-verifier retry prompt written to %s", prompt_path)
            elif no_rca:
                # Ablation A2: No RCA. The Verifier still runs and produces diffs
                # (HW vs EM encoded traces compared). The LLM sees the Verifier's
                # comparison data + broken model source + generic "fix it", but NOT
                # RCA's targeted diagnostic narrative or SEARCH/REPLACE strategy.
                # Written as initial_prompt.txt so LLM does full model regeneration.
                log.warning("Log mismatch! --no-rca: generating prompt with Verifier diffs but no RCA analysis.")

                pg_path = pg.iteration_prompt_gen(
                    diff_obj=diff_obj,
                    device_model_path=list(model_to_path.values())[0],
                    peripheral=peripheral[0],
                    out_dir=work_dir,
                    no_vio=no_vio,
                    no_rca=True,
                )
            elif no_encoder:
                # Ablation A1 correction: use raw traces instead of structured
                # encoder analysis, but keep the SEARCH/REPLACE correction strategy.
                log.warning("Log mismatch! --no-encoder: generating correction prompt with raw traces.")

                # Build peripheral address ranges from SVD for trace filtering
                peripheral_ranges = []
                for p in svd_device.peripherals:
                    if p.name.upper() == peripheral[0].upper():
                        size = 0x400
                        if hasattr(p, 'address_block') and p.address_block:
                            size = p.address_block.size
                        peripheral_ranges.append((p.base_address, p.base_address + size))

                pg_path = pg.iteration_prompt_gen(
                    diff_obj=diff_obj,
                    device_model_path=list(model_to_path.values())[0],
                    peripheral=peripheral[0],
                    out_dir=work_dir,
                    no_encoder=True,
                    hardware_log=hardware_log,
                    emulation_log=emulation_log,
                    peripheral_ranges=peripheral_ranges,
                    no_vio=no_vio,
                )
            else:
                log.warning("Log mismatch! Generating prompt.")
                pg_path = pg.iteration_prompt_gen(
                    diff_obj=diff_obj,
                    device_model_path=list(model_to_path.values())[0],
                    peripheral=peripheral[0],
                    out_dir=work_dir,
                    no_vio=no_vio,
                )
        else:
            log.info("Both hardware log and emulation log matched!")

    else:
        pg_path = pg.iteration_prompt_gen_multiple_periph(
            cm_path_hardware=cm_path_hardware,
            cm_path_emulation=cm_path_emulation,
            peripherals=peripheral,
            model_names=model_names,
            model_to_path=model_to_path,
            out_dir=work_dir,
            no_encoder=no_encoder,
            no_rca=no_rca,
            no_verifier=no_verifier,
            no_vio=no_vio,
            hardware_log=hardware_log,
            emulation_log=emulation_log,
            model_sources_map=model_sources_map or None,
        )

@cli.command(
    'fuzz',
    help='Performs the fuzzing using the hardware trace and toml configuration.'
         'Takes the hardware trace to find the data registers and then uses toml configuration to run in the elder mode for fuzzing'
)
@click.option('-c','--config',required = True, type= click.Path(resolve_path=True,exists=True),
                        help='The Path to the config file.',
                        metavar= 'PATH')
@click.option(
    '-hw', '--hardware-log',
    required=True,
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the log file generated when running the firmware on hardware.',
    metavar='PATH'
)
@click.option(
    '-p', '--peripheral',
    type=str,
    required=True,
    help='Name of the peripheral targeted for verification.'
)
@click.option(
    '-b', '--board',
    type=str,
    required=True,
    help='Name of the platform on which the firmware is running.'
)
@click.option(
    '--method',
    default='ngram',
    show_default=True,
    type=click.Choice(['ngram', 'window', 'other'], case_sensitive=False),
    help='Context minimization method.'
)
@click.option(
    '--n',
    type=int,
    default=2,
    show_default=True,
    help='Value of n for n-gram method.'
)
@click.option(
    '--isr-window',
    type=int,
    default=10000000,
    show_default=True,
    help='ISR window size.'
)
@click.option(
    '-o', '--work-dir',
    default="./fastdyn_work",
    show_default=True,
    metavar='PATH',
    type=click.Path(resolve_path=True, writable=True),
    help='Path to the work directory.'
)
@click.option(
    '-s', '--svd',
    type=click.Path(resolve_path=True, exists=True),
    default=None,
    metavar='PATH',
    help='Optional path to an SVD file or directory.'
)
def fuzz(config, hardware_log, peripheral, board, method, n, isr_window, work_dir, svd):
    """Performs Fuzzing using the context minimizer for data register and then runs using the toml config"""
    # Feature-only imports: keeping them here means core commands such as
    # `fastdyn run` do not need these modules or their dependencies (scipy).
    from .fuzzer import fuzzer
    from .verifier import context_minimizer as cm

    if work_dir is not None:
        if not os.path.isdir(work_dir):
            log.warning(f"The output directory: {work_dir} passed by the user does not exist.")
    else:
        work_dir = "fastdyn_work"

    if os.path.exists(work_dir):
        log.info(f"The output directory already exists at Path {os.path.abspath(work_dir)}. Deleting it!")
        shutil.rmtree(work_dir)

    log.info(f"Creating output directory at path: {os.path.abspath(work_dir)}")
    os.makedirs(work_dir)

    #minimize the context
    cm_path = cm.minimize_context(
        out_dir=work_dir,
        log_file=hardware_log,
        platform=board,
        method=method,
        peripheral=peripheral,
        n=n,
        isr_window=isr_window,
        svd_path=svd_path,
        cm_dir_name="out_cm"
        )

    #takes the peripheral and the minimized context path to find the entropy.txt and generate all the anchor virtual instructions
    anchors_lst = fuzzer.generate_vi(
        cm_path     = cm_path,
        peripheral  = peripheral,
    )

    #It will parse the config and create a handle using fastdyn.py apis that has all the info about the machines and cpus listed in the toml
    fastdyn_handle = toml_parser.parser(machine_name="machine0",toml_config=config, svd_path=svd_path)

    #TODO: For initial verification of fuzzer, the cpu is hardcoded to be zero, update this once complete fuzzer is added
    for machine in fastdyn_handle.machines:
        curr_machine = fastdyn_handle.machines[machine]
        for cpu in curr_machine.cpus:
            cpu.add_virtual_instruction(anchors_lst)
            fastdyn_log.info(f"Virtual Instructions for {cpu}: {cpu.virtuals}")

    #run all the machines requested by the user
    for idx, machine in enumerate(fastdyn_handle.machines):
        fastdyn_handle.run(machine_name=f"machine{idx}",
                           target="qemu",
                           out_path=work_dir
                           )


@cli.command(
    'llm',
    help=(
        'Sends a prompt from a work directory to an LLM provider and processes '
        'the response. For initial_prompt.txt: extracts C code and writes the model. '
        'For revised_prompt.txt: parses SEARCH/REPLACE blocks and patches the model.'
    )
)
@click.option(
    '-d', '--work-dir',
    required=True,
    type=click.Path(resolve_path=True, exists=True),
    help='Path to the FastDyn work directory containing initial_prompt.txt or revised_prompt.txt.',
    metavar='PATH'
)
@click.option(
    '-o', '--output',
    required=False,
    multiple=True,
    type=click.Path(resolve_path=True),
    help=(
        'Path to the model .c file(s). Can be specified multiple times. '
        'If omitted, attempts to auto-detect from work_dir/analysis.json.'
    ),
    metavar='PATH'
)
@click.option(
    '--model',
    default='gpt-4o',
    show_default=True,
    type=str,
    help='Model name to use.'
)
@click.option(
    '--model-provider',
    default='openai',
    show_default=True,
    type=click.Choice(['openai', 'ollama'], case_sensitive=False),
    help='LLM provider backend.'
)
@click.option(
    '--env-file',
    default='~/.fastdyn.env',
    show_default=True,
    type=str,
    help='Path to .env file containing OPENAI_API_KEY.'
)
@click.option(
    '--temperature',
    default=0.2,
    show_default=True,
    type=float,
    help='Sampling temperature. For Ollama, the default is 0.1 unless this option is explicitly set.'
)
@click.option(
    "--reasoning-effort",
    type=click.Choice(["none", "minimal", "low", "medium", "high", "xhigh"]),
    default=None,
    help="Reasoning effort for supported gpt-5 and o-series models."
)
@click.option(
    '--stateless/--no-stateless',
    default=True,
    show_default=True,
    help='Keep (--stateless) or strip (--no-stateless) the conversation reset line in prompts.'
)
@click.option(
    '--compile/--no-compile',
    default=False,
    show_default=True,
    help='Compile the model after writing/patching using the boardrunner SDK.'
)
@click.option(
    '--sdk-dir',
    default='boardrunner/boardrunner_sdk',
    show_default=True,
    type=click.Path(resolve_path=True),
    help='Path to the boardrunner SDK directory (for compilation).',
    metavar='PATH'
)
@click.option(
    '--max-retries',
    default=1,
    show_default=True,
    type=int,
    help='Maximum number of retry attempts on patch or compilation failure.'
)
@click.option(
    '--evaluate/--no-evaluate',
    default=False,
    show_default=True,
    help='Enable evaluation metrics logging. Writes per-call metrics to fastdyn_llm_history/metrics.jsonl.'
)
@click.option(
    '--ollama-url',
    default='http://127.0.0.1:11434',
    show_default=True,
    type=str,
    help='Base URL for the Ollama HTTP server. Use SSH port forwarding for remote servers.'
)
@click.option(
    '--ollama-num-ctx',
    default=262144,
    show_default=True,
    type=int,
    help='Ollama num_ctx option. Set to 0 to use the server/model default.'
)
@click.option(
    '--ollama-timeout',
    default=1800.0,
    show_default=True,
    type=float,
    help='Timeout in seconds for non-streaming Ollama requests.'
)
@click.pass_context
def llm(ctx, work_dir, output, model, model_provider, env_file, temperature,
        reasoning_effort, stateless, compile, sdk_dir, max_retries, evaluate,
        ollama_url, ollama_num_ctx, ollama_timeout):
    """Sends a prompt to the selected LLM provider and processes the response."""
    # Feature-only imports, kept here so core commands do not depend on the llm package.
    from .llm.handlers import llm_history_next
    from fastdyn.llm.llm_client import LLMClient, LLMClientError, load_api_key
    from fastdyn.llm.response_parser import (
        extract_c_code, parse_search_replace_blocks, ParsingError
    )
    from fastdyn.llm.patch import (
        apply_search_replace_patches, write_patched_file, write_model_file, PatchError
    )

    # -- Determine prompt type ------------------------------------------------
    initial_prompt_path = os.path.join(work_dir, "initial_prompt.txt")
    revised_prompt_path = os.path.join(work_dir, "revised_prompt.txt")
    prompt_txt_path = os.path.join(work_dir, "prompt.txt")

    if os.path.isfile(revised_prompt_path):
        prompt_path = revised_prompt_path
        prompt_type = "revised"
        log.info("Found revised_prompt.txt -- using SEARCH/REPLACE patch mode")
    elif os.path.isfile(initial_prompt_path):
        prompt_path = initial_prompt_path
        prompt_type = "initial"
        log.info("Found initial_prompt.txt -- using full code extraction mode")
    elif os.path.isfile(prompt_txt_path):
        prompt_path = prompt_txt_path
        # Detect routing-only prompts. Routed implementation prompts still use
        # SEARCH/REPLACE, even when they were generated from routing.json.
        _analysis_json = os.path.join(work_dir, "analysis.json")
        _is_diagnostic = False
        if os.path.isfile(_analysis_json):
            import json as _json
            with open(_analysis_json, "r") as _af:
                _ad = _json.load(_af)
            _is_diagnostic = (
                _ad.get("prompt_kind") == "diagnostic"
                and _ad.get("target_peripheral") is None
                and not _ad.get("routing_used", False)
            )
        if _is_diagnostic:
            prompt_type = "diagnostic"
            log.info("Found prompt.txt with no target_peripheral -- using DIAGNOSTIC routing mode (JSON output only)")
        else:
            prompt_type = "revised"
            log.info("Found prompt.txt -- using SEARCH/REPLACE patch mode")
    else:
        log.error(
            "No prompt file found in %s. "
            "Expected initial_prompt.txt, revised_prompt.txt, or prompt.txt.", work_dir
        )
        sys.exit(1)

    # -- Auto-detect output if omitted ----------------------------------------
    # In diagnostic mode (Mode B) there is no C file to write — the LLM emits
    # JSON only, so --output is not required.
    if prompt_type != "diagnostic":
        if not output:
            analysis_json = os.path.join(work_dir, "analysis.json")
            if os.path.isfile(analysis_json):
                import json
                with open(analysis_json, "r") as f:
                    analysis_data = json.load(f)
                    target_model_file = analysis_data.get("target_model_file")
                    if target_model_file:
                        if isinstance(target_model_file, list):
                            output = tuple(target_model_file)
                            log.info("Auto-detected target model outputs: %s", ", ".join(target_model_file))
                        else:
                            output = (target_model_file,)
                            log.info("Auto-detected target model output: %s", target_model_file)

            if not output:
                log.error("Error: --output PATH is required, but was omitted and could not be auto-detected from analysis.json.")
                sys.exit(1)

    # -- Read prompt ----------------------------------------------------------
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_text = f.read()

    if not prompt_text.strip():
        log.error("Prompt file is empty: %s", prompt_path)
        sys.exit(1)

    log.info("Read prompt from %s (%d characters)", prompt_path, len(prompt_text))

    # -- Persistent LLM history -----------------------------------------------
    # Saved in fastdyn_llm_history/ next to the work directory, never deleted.
    # Each invocation gets a new NNN prefix; retries within the same run get
    # separate response files (NNN_response_1.txt, NNN_response_2.txt, ...).
    history_dir = os.path.join(os.path.dirname(work_dir), "fastdyn_llm_history")
    history_iter = llm_history_next(history_dir)
    history_prefix = os.path.join(history_dir, f"{history_iter:03d}")
    with open(f"{history_prefix}_prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt_text)
    log.info("LLM history prompt saved to %s_prompt.txt (iteration %d)", history_prefix, history_iter)

    # -- Validate output file for revised prompts ----------------------------
    if prompt_type == "revised":
        for out_path in output:
            if not os.path.isfile(out_path):
                log.error(
                    "Revised prompt mode requires an existing model file to patch. "
                    "File not found: %s", out_path
                )
                sys.exit(1)

    model_provider = model_provider.lower()

    # -- Initialize LLM client ------------------------------------------------
    try:
        if model_provider == "ollama":
            from fastdyn.llm.ollama_client import OllamaClient

            if reasoning_effort and reasoning_effort != "none":
                log.warning(
                    "--reasoning-effort is ignored for --model-provider ollama. "
                    "Use Ollama model configuration for reasoning controls."
                )

            temperature_source = None
            if hasattr(ctx, "get_parameter_source"):
                temperature_source = ctx.get_parameter_source("temperature")
            ollama_temperature = 0.1 if str(temperature_source).endswith("DEFAULT") else temperature

            client = OllamaClient(
                model=model,
                base_url=ollama_url,
                temperature=ollama_temperature,
                num_ctx=ollama_num_ctx or None,
                timeout=ollama_timeout,
            )
        else:
            api_key = load_api_key(env_file)
            client = LLMClient(
                api_key=api_key,
                model=model,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
    except LLMClientError as e:
        log.error(str(e))
        sys.exit(1)

    # -- Evaluation metrics setup -----------------------------------------------
    metrics_path = None
    if evaluate:
        import json as _json
        metrics_path = os.path.join(history_dir, "metrics.jsonl")
        log.info("Evaluation mode enabled. Metrics will be written to %s", metrics_path)

    def _write_metrics(metrics, attempt_num, call_type, extra=None):
        """Append a metrics JSON line if --evaluate is enabled."""
        if not evaluate or metrics is None:
            return
        entry = metrics.to_dict()
        entry["iteration"] = history_iter
        entry["attempt"] = attempt_num
        entry["type"] = call_type
        entry["model_provider"] = model_provider
        entry["reasoning_effort"] = reasoning_effort
        if extra:
            entry.update(extra)
        with open(metrics_path, "a", encoding="utf-8") as mf:
            mf.write(_json.dumps(entry) + "\n")

    # -- Send prompt and process response -------------------------------------
    attempt = 0
    max_attempts = 1 + max_retries
    previous_response = None

    while attempt < max_attempts:
        attempt += 1
        is_retry = attempt > 1

        try:
            if is_retry and previous_response:
                log.info("Retry attempt %d/%d...", attempt - 1, max_retries)
                response_text, call_metrics = client.send_followup_prompt(
                    original_prompt=prompt_text,
                    previous_response=previous_response,
                    error_context=error_context,
                    stateless=stateless,
                )
            else:
                response_text, call_metrics = client.send_prompt(prompt_text, stateless=stateless)
        except LLMClientError as e:
            log.error("LLM request failed: %s", str(e))
            sys.exit(1)

        # Save raw response for debugging/auditing
        response_path = os.path.join(work_dir, "llm_response.txt")
        with open(response_path, "w", encoding="utf-8") as f:
            f.write(response_text)
        log.info("Raw LLM response saved to %s", response_path)

        # Persist to history (one file per attempt so retries are all kept)
        history_response_path = f"{history_prefix}_response_{attempt}.txt"
        with open(history_response_path, "w", encoding="utf-8") as f:
            f.write(response_text)
        log.info("LLM history response saved to %s", history_response_path)

        previous_response = response_text

        # -- Process based on prompt type -------------------------------------
        success = False
        error_context = ""

        from fastdyn.llm.handlers import (
            handle_initial_prompt,
            handle_revised_prompt,
            handle_compilation,
        )

        if prompt_type == "diagnostic":
            # Mode B: LLM is a pure router — parse the JSON routing block.
            from fastdyn.llm.response_parser import extract_routing_json, RoutingParseError
            try:
                routing = extract_routing_json(response_text)
                routing_path = os.path.join(work_dir, "routing.json")
                import json
                with open(routing_path, "w", encoding="utf-8") as f:
                    json.dump(routing, f, indent=2)
                log.info("Routing JSON written to %s", routing_path)
                log.info("LLM routing decision: %s", routing.get("reasoning", "(no reasoning)"))

                from fastdyn.llm.handlers import handle_routing
                success, error_context = handle_routing(routing, work_dir)
            except RoutingParseError as e:
                error_context = f"Failed to parse routing JSON from LLM response: {e}"
                log.error(error_context)

        elif response_text.lstrip().startswith("VETO:"):
            # Mode A VETO: parse the JSON routing block and surface it to the user.
            from fastdyn.llm.response_parser import extract_routing_json, RoutingParseError
            log.warning(
                "\n================= VETO =================\n"
                "The LLM has VETOED this request. Parsing routing JSON..."
            )
            try:
                routing = extract_routing_json(response_text)
                routing_path = os.path.join(work_dir, "routing.json")
                import json
                with open(routing_path, "w", encoding="utf-8") as f:
                    json.dump(routing, f, indent=2)
                log.warning(
                    "Reasoning: %s\nRouting JSON written to %s\n"
                    "========================================",
                    routing.get("reasoning", "(no reasoning)"), routing_path
                )

                from fastdyn.llm.handlers import handle_routing
                success, error_context = handle_routing(routing, work_dir)
            except RoutingParseError as e:
                log.warning("Could not parse routing JSON from VETO response: %s", e)
                success, error_context = False, str(e)

            # Exit either way because a VETO means the current generation path is dead
            if not success:
                sys.exit(1)
            sys.exit(0)

        elif prompt_type == "initial":
            success, error_context = handle_initial_prompt(
                response_text, output[0], work_dir
            )
        else:
            success, error_context = handle_revised_prompt(
                response_text, output, work_dir
            )

        if prompt_type == "diagnostic" and success:
            _write_metrics(call_metrics, attempt, "routing", {"result": "success"})
            log.info("LLM routing response processed successfully.")
            break

        if not success:
            call_type = "followup" if is_retry else ("revision" if prompt_type == "revised" else "initial")
            _write_metrics(call_metrics, attempt, call_type, {"result": "parse_fail"})
            if attempt < max_attempts:
                # Use builtin input() to avoid click.confirm TTY hangs
                user_reply = input("Send a follow-up request to the LLM with error context? [Y/n]: ").strip().lower()
                retry = (user_reply == "y" or user_reply == "")

                if not retry:
                    log.info("User chose not to retry. Exiting.")
                    sys.exit(1)
                continue
            else:
                log.error("All %d attempt(s) exhausted. Exiting.", max_attempts)
                sys.exit(1)

        # -- Optional compilation step ----------------------------------------
        if compile:
            compile_success, compile_error, is_setup_error = handle_compilation(sdk_dir)
            if not compile_success:
                call_type = "followup" if is_retry else ("revision" if prompt_type == "revised" else "initial")
                _write_metrics(call_metrics, attempt, call_type, {"result": "compile_fail"})
                if is_setup_error:
                    log.error("Aborting LLM loop due to compilation setup error.")
                    sys.exit(1)

                error_context = compile_error
                if attempt < max_attempts:
                    # Use builtin input() to avoid click.confirm TTY hangs
                    user_reply = input("Compilation failed. Send a follow-up request to the LLM? [Y/n]: ").strip().lower()
                    retry = (user_reply == "y" or user_reply == "")

                    if not retry:
                        log.info("User chose not to retry. Exiting.")
                        sys.exit(1)
                    continue
                else:
                    log.error("All %d attempt(s) exhausted. Exiting.", max_attempts)
                    sys.exit(1)

        # If we reach here, everything succeeded
        call_type = "followup" if is_retry else ("revision" if prompt_type == "revised" else "initial")
        _write_metrics(call_metrics, attempt, call_type, {"result": "success"})
        log.info("LLM processing completed successfully.")
        break


if __name__ == "__main__":
    cli()
