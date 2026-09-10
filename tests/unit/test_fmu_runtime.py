"""Check portable FMU metadata and stale-native-library prevention."""

from pathlib import Path
import zipfile

import fmpy
import fmpy.build
import pytest

from fastdyn.fmu_runtime import prepare


def archive(path, *, contents=b"first binary", source=False, profile="CoSimulation"):
    with zipfile.ZipFile(path, "w") as output:
        output.writestr("modelDescription.xml", f'''<fmiModelDescription
          fmiVersion="3.0" instantiationToken="token-from-model-description">
          <{profile} modelIdentifier="ActualIdentifier"/>
        </fmiModelDescription>''')
        if source:
            output.writestr("sources/buildDescription.xml", "source build fixture")
        else:
            output.writestr(
                f"binaries/{fmpy.platform_tuple}/ActualIdentifier{fmpy.sharedLibraryExtension}",
                contents,
            )


def test_uses_xml_identity_and_reuses_binary(tmp_path, monkeypatch):
    path = tmp_path / "unrelated_archive_name.fmu"
    archive(path)
    monkeypatch.setattr(fmpy.build, "build_platform_binary", lambda *_: pytest.fail("rebuilt a binary FMU"))
    runtime = prepare(path)
    assert runtime.library.name == "ActualIdentifier" + fmpy.sharedLibraryExtension
    assert runtime.library.read_bytes() == b"first binary"
    assert runtime.instantiation_token == "token-from-model-description"
    assert runtime.resources.is_dir()
    modified = runtime.library.stat().st_mtime_ns
    assert prepare(path) == runtime
    assert runtime.library.stat().st_mtime_ns == modified


def test_replaces_native_cache_when_archive_content_changes(tmp_path):
    path = tmp_path / "vehicle.fmu"
    archive(path)
    runtime = prepare(path)
    archive(path, contents=b"other binary")
    assert prepare(path).library == runtime.library
    assert runtime.library.read_bytes() == b"other binary"


def test_builds_source_fmu_once_and_preserves_archive(tmp_path, monkeypatch):
    path = tmp_path / "vehicle.fmu"
    archive(path, source=True)
    original = path.read_bytes()
    calls = []

    def compile_source(directory):
        calls.append(directory)
        assert directory.is_absolute()
        native = directory / "binaries" / fmpy.platform_tuple
        native.mkdir(parents=True)
        (native / ("ActualIdentifier" + fmpy.sharedLibraryExtension)).write_bytes(b"compiled")

    monkeypatch.setattr(fmpy.build, "build_platform_binary", compile_source)
    assert prepare(path).library.read_bytes() == b"compiled"
    assert prepare(path).library.read_bytes() == b"compiled"
    assert len(calls) == 1
    assert path.read_bytes() == original


def test_failed_build_does_not_mark_archive_ready(tmp_path, monkeypatch):
    path = tmp_path / "vehicle.fmu"
    archive(path, source=True)

    def failed_build(_):
        raise RuntimeError("compiler failed")

    monkeypatch.setattr(fmpy.build, "build_platform_binary", failed_build)
    with pytest.raises(RuntimeError, match="compiler failed"):
        prepare(path)
    assert not path.with_suffix(".runtime").exists()


def test_rejects_model_exchange_only(tmp_path):
    path = tmp_path / "vehicle.fmu"
    archive(path, profile="ModelExchange")
    with pytest.raises(ValueError, match="Co-Simulation"):
        prepare(path)
