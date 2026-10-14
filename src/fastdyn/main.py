'''
Main file is responsible for kicking the qemu command.
'''
import logging
import click
import shutil
import json
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

from . import logs
from fastdyn.__init__ import __version__
# from .machine import Machine, CPUConfig, VirtualInstruction, InstructionModifier
from . import runtime_config
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


if __name__ == "__main__":
    cli()
