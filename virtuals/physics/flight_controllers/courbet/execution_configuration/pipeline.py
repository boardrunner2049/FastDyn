"""Load a target descriptor, validate its ELF, and publish resolved configuration."""
from pathlib import Path
import tempfile

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from profiles import ardupilot, px4

from .elf import FirmwareELF
from .rules import compile_rules
from .validation import validate_target


FIRMWARES = {'ardupilot': ardupilot, 'px4': px4}


def prepare_execution_configuration(profile_path, *, elf_path=None, output_dir=None):
    """Resolve paths in TOML relative to that file; CLI overrides use the caller's cwd.

    All validation and rule compilation finish before any output is replaced.
    boot.nsh and simulator settings are runtime inputs, not generator outputs.
    """
    profile_path = Path(profile_path).resolve()
    if profile_path.is_dir():
        profile_path /= 'profile.toml'
    with profile_path.open('rb') as stream:
        profile = tomllib.load(stream)
    unknown = profile.keys() - {'schema_version', 'firmware', 'elf', 'labeled_conf',
                                'output_dir', 'validation'}
    if unknown:
        raise ValueError(f'unknown profile settings: {sorted(unknown)}')
    if profile.get('schema_version') != 1:
        raise ValueError('profile schema_version must be 1')
    firmware_name = profile.get('firmware')
    if not isinstance(firmware_name, str) or firmware_name not in FIRMWARES:
        raise ValueError('profile firmware must be ardupilot or px4')
    firmware = FIRMWARES[firmware_name]

    def relative_path(key, default=None):
        value = profile.get(key, default)
        if not isinstance(value, str) or not value:
            raise ValueError(f'profile {key} must be a nonempty path string')
        return (profile_path.parent / value).resolve()

    binary = relative_path('elf') if elf_path is None else Path(elf_path).resolve()
    source = relative_path('labeled_conf', 'labeled_conf')
    destination = (relative_path('output_dir', 'unlabeled_conf') if output_dir is None
                   else Path(output_dir).resolve())
    if destination == source:
        raise ValueError('output directory must differ from labeled_conf')
    checks = profile.get('validation', {})
    if not isinstance(checks, dict):
        raise ValueError('profile validation must be a table')

    elf = FirmwareELF(binary)
    validate_target(elf, checks)
    firmware.validate(elf)
    outputs = {f'{kind}.txt': compile_rules(source / f'{kind}.txt', kind, elf, firmware)
               for kind in ('virtuals', 'modifiers')}
    outputs['symbols.txt'] = elf.symbol_map()
    outputs['firmware.sha256'] = f'{elf.sha256}  {binary.name}\n'

    destination.mkdir(parents=True, exist_ok=True)
    # Stage complete files on the same filesystem before replacing outputs.
    with tempfile.TemporaryDirectory(prefix='.prepare-', dir=destination) as temp:
        staging = Path(temp)
        for name, data in outputs.items():
            (staging / name).write_text(data)
        for name in outputs:
            (staging / name).replace(destination / name)
    return destination
