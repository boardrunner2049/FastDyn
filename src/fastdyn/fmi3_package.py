"""Build Rumoca's source FMI3 package using the official FMI3 C ABI."""
from pathlib import Path
import os
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile


def _source_files(root: Path, identifier: str) -> list[Path]:
    from .fmu_build import FmuConfigError

    source_root = root / 'sources'
    description = source_root / 'buildDescription.xml'
    if not description.is_file():
        return [source_root / 'model.c']
    metadata = ET.parse(description).getroot()
    configuration = next((entry for entry in metadata.findall('BuildConfiguration')
                          if entry.get('modelIdentifier') == identifier), None)
    if configuration is None:
        raise FmuConfigError(f'FMI3 build description has no configuration for {identifier}')
    sources = []
    for file_set in configuration.findall('SourceFileSet'):
        if file_set.get('language') not in ('C', 'C99'):
            raise FmuConfigError('Rumoca source FMU packaging requires C sources')
        for entry in file_set.findall('SourceFile'):
            source = (source_root / entry.get('name', '')).resolve()
            if not source.is_relative_to(source_root.resolve()) or not source.is_file():
                raise FmuConfigError(f'FMI3 build description names an invalid source: {source}')
            if source not in sources:
                sources.append(source)
    if not sources:
        raise FmuConfigError('FMI3 build description has no source files')
    return sources


def package_sources(build) -> None:
    from .fmu_build import FmuConfigError

    root = build.output / build.model.replace('.', '_')
    description = root / 'modelDescription.xml'
    if not description.is_file():
        raise FmuConfigError(f'Rumoca source FMU metadata missing: {description}')
    metadata = ET.parse(description).getroot()
    cs = metadata.find('CoSimulation')
    identifier = cs.get('modelIdentifier', '') if cs is not None else ''
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', identifier):
        raise FmuConfigError('FMI3 source package has no valid CoSimulation modelIdentifier')
    headers = build.repo_root / 'out/toolchains/fmi3/headers'
    if not (headers / 'fmi3Functions.h').is_file():
        raise FmuConfigError('FMI3 headers missing; run python3 utils/setup_fire_toolchain.py')
    architectures = {'x86_64': 'x86_64-linux', 'aarch64': 'aarch64-linux'}
    if platform.system() != 'Linux' or platform.machine() not in architectures:
        raise FmuConfigError('Source FMU binary packaging currently supports x86_64/aarch64 Linux')
    binary = root / 'binaries' / architectures[platform.machine()] / (identifier + '.so')
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c99', '-O2', '-fPIC', '-shared',
                    '-DFMI3_OVERRIDE_FUNCTION_PREFIX', '-I', str(headers),
                    '-Wl,--no-undefined',
                    *(str(source) for source in _source_files(root, identifier)),
                    '-lm', '-o', str(binary)], check=True)
    temporary = build.fmu_path.with_suffix('.fmu.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        temporary.replace(build.fmu_path)
    finally:
        temporary.unlink(missing_ok=True)
