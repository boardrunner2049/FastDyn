import ctypes
from pathlib import Path
import platform
import shutil
import zipfile

import pytest

from fastdyn.fmi3_package import package_sources
from fastdyn.fmu_build import FmuBuild, FmuConfigError


def source_build(tmp_path: Path) -> FmuBuild:
    build = FmuBuild(name='probe', model='Probe', model_file=tmp_path / 'Probe.mo',
                     source_root=tmp_path, extra_source_roots=(),
                     output=tmp_path / 'fmu', repo_root=tmp_path)
    root = build.output / build.model
    (root / 'sources').mkdir(parents=True)
    (root / 'modelDescription.xml').write_text(
        '<fmiModelDescription><CoSimulation modelIdentifier="Probe"/></fmiModelDescription>')
    headers = tmp_path / 'out/toolchains/fmi3/headers'
    headers.mkdir(parents=True)
    (headers / 'fmi3Functions.h').touch()
    return build


@pytest.mark.skipif(platform.system() != 'Linux' or shutil.which('cc') is None,
                    reason='requires Linux C toolchain')
def test_package_links_declared_translation_units(tmp_path, monkeypatch):
    monkeypatch.setenv('CC', 'cc')
    build = source_build(tmp_path)
    sources = build.output / build.model / 'sources'
    (sources / 'model.c').write_text(
        'extern double helper(void); double result(void) { return helper(); }')
    (sources / 'rmc_rows.c').write_text('double helper(void) { return 42.0; }')
    (sources / 'stale.c').write_text('#error This file is not in the build description')
    (sources / 'buildDescription.xml').write_text(
        '<fmiBuildDescription><BuildConfiguration modelIdentifier="Probe">'
        '<SourceFileSet language="C99"><SourceFile name="model.c"/>'
        '<SourceFile name="rmc_rows.c"/></SourceFileSet>'
        '</BuildConfiguration></fmiBuildDescription>')

    package_sources(build)

    with zipfile.ZipFile(build.fmu_path) as archive:
        binary = next(name for name in archive.namelist() if name.endswith('.so'))
        archive.extract(binary, tmp_path / 'unpacked')
    library = ctypes.CDLL(str(tmp_path / 'unpacked' / binary))
    library.result.restype = ctypes.c_double
    assert library.result() == 42.0


def test_package_rejects_missing_declared_source(tmp_path):
    build = source_build(tmp_path)
    sources = build.output / build.model / 'sources'
    (sources / 'buildDescription.xml').write_text(
        '<fmiBuildDescription><BuildConfiguration modelIdentifier="Probe">'
        '<SourceFileSet language="C99"><SourceFile name="missing.c"/></SourceFileSet>'
        '</BuildConfiguration></fmiBuildDescription>')
    with pytest.raises(FmuConfigError, match='invalid source'):
        package_sources(build)
