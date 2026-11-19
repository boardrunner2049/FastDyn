"""Regression coverage for UUID-named FMUs and the native FMI 3 loader."""

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import pytest

from fastdyn import fmu_runtime
from fastdyn.fmu_build import FmuBuild, FmuConfigError, REQUIRED_VALUE_REFERENCES


TOKEN = "8604a8f9-35e4-44b7-b6a6-d486396d3ed2"
IDENTIFIER = "Rumoca_" + TOKEN.replace("-", "_")
REPO = Path(__file__).resolve().parents[2]


def description(token=TOKEN, offset=100):
    root = ET.Element("fmiModelDescription", fmiVersion="3.0", instantiationToken=token)
    ET.SubElement(root, "CoSimulation", modelIdentifier="Rumoca_" + token.replace("-", "_"))
    variables = ET.SubElement(root, "ModelVariables")
    for i, name in enumerate(REQUIRED_VALUE_REFERENCES, start=1):
        ET.SubElement(variables, "Float64", name=name, valueReference=str(offset+i))
    return ET.tostring(root)


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setattr(fmu_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fmu_runtime.platform, "machine", lambda: "x86_64")
    output = tmp_path / "export"
    output.mkdir()
    return FmuBuild(name="quad", model="ConfiguredQuad", model_file=tmp_path/"Quad.mo",
                    source_root=tmp_path, extra_source_roots=(), output=output,
                    repo_root=tmp_path)


def package(build, *, token=TOKEN, offset=100, binary=b"test binary", xml=None):
    identifier = "Rumoca_" + token.replace("-", "_")
    with zipfile.ZipFile(build.fmu_path, "w") as archive:
        archive.writestr("modelDescription.xml", description(token, offset) if xml is None else xml)
        archive.writestr(f"binaries/x86_64-linux/{identifier}.so", binary)
        archive.writestr("resources/data.txt", "resource belonging to this FMU")


def test_archive_metadata_wins_over_stale_build_tree(build, tmp_path):
    package(build)
    (build.output/"modelDescription.xml").write_bytes(description("stale", 900))
    stale = build.output/build.model
    stale.mkdir()
    (stale/"modelDescription.xml").write_bytes(description("also_stale", 800))
    runtime = fmu_runtime.prepare_runtime(build, tmp_path/"run")
    assert runtime.library_path.name == IDENTIFIER + ".so"
    assert runtime.library_path.read_bytes() == b"test binary"
    assert runtime.instantiation_token == TOKEN
    assert runtime.value_references["pwm"] == 101
    assert runtime.library_path.is_relative_to(tmp_path/"run")
    assert runtime.resource_path.endswith("/resources/")
    assert (Path(runtime.resource_path)/"data.txt").is_file()


def test_reexport_with_new_uuid_does_not_reuse_previous_binary_or_refs(build, tmp_path):
    package(build)
    old = fmu_runtime.prepare_runtime(build, tmp_path/"run")
    token = "442ac5eb-1e89-4aa0-a594-8e640463b68c"
    package(build, token=token, offset=200, binary=b"replacement")
    new = fmu_runtime.prepare_runtime(build, tmp_path/"run")
    assert new.instantiation_token == token
    assert new.value_references["pwm"] == 201
    assert new.library_path.read_bytes() == b"replacement"
    assert old.library_path.read_bytes() == b"test binary"
    assert new.library_path != old.library_path


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("platform_dir", ["x86_64-linux", "linux64", "aarch64-linux"])
def test_unpacked_layouts(build, tmp_path, monkeypatch, nested, platform_dir):
    build = replace(build, package=False)
    root = build.output/build.model if nested else build.output
    root.mkdir(exist_ok=True)
    (root/"modelDescription.xml").write_bytes(description())
    library = root/"binaries"/platform_dir/(IDENTIFIER+".so")
    library.parent.mkdir(parents=True)
    library.write_bytes(b"binary")
    if platform_dir == "aarch64-linux":
        monkeypatch.setattr(fmu_runtime.platform, "machine", lambda: "aarch64")
    runtime = fmu_runtime.prepare_runtime(build, tmp_path/"run")
    assert runtime.library_path == library
    assert runtime.resource_path is None
    assert runtime.instantiation_token == TOKEN


@pytest.mark.parametrize("xml,message", [
    (b"<broken", "Cannot read FMU metadata"),
    (b'<fmiModelDescription fmiVersion="2.0"/>', "requires FMI 3"),
    (b'<fmiModelDescription fmiVersion="3.0"/>', "CoSimulation modelIdentifier"),
    (b'<fmiModelDescription fmiVersion="3.0"><CoSimulation modelIdentifier="../bad"/></fmiModelDescription>', "modelIdentifier"),
    (b'<fmiModelDescription fmiVersion="3.0"><CoSimulation modelIdentifier="Probe"/></fmiModelDescription>', "instantiationToken"),
])
def test_invalid_metadata_fails_before_launch(build, tmp_path, xml, message):
    package(build, xml=xml)
    with pytest.raises(FmuConfigError, match=message):
        fmu_runtime.prepare_runtime(build, tmp_path/"run")


def test_missing_matching_binary_does_not_pick_another_so(build, tmp_path):
    package(build, xml=description("other"))
    with pytest.raises(FmuConfigError, match="binary for Rumoca_other is missing"):
        fmu_runtime.prepare_runtime(build, tmp_path/"run")


def test_rejects_archive_path_escape(build, tmp_path):
    package(build)
    with zipfile.ZipFile(build.fmu_path, "a") as archive:
        archive.writestr("../escaped", "unexpected")
    with pytest.raises(FmuConfigError, match="invalid path"):
        fmu_runtime.prepare_runtime(build, tmp_path/"run")
    assert not (tmp_path/"run/escaped").exists()


@pytest.fixture
def native_probe(tmp_path):
    if shutil.which("cc") is None:
        pytest.skip("requires C compiler")
    # A small FMI 3 implementation that rejects incorrect tokens and references.
    source = tmp_path/"probe.c"
    source.write_text(r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
typedef struct { double time; bool ready; } State;
void *fmi3InstantiateCoSimulation(const char *name, const char *token,
    const char *resources, bool visible, bool logging, bool events, bool early,
    const uint32_t *refs, size_t nrefs, void *env, void *logger, void *update) {
    if (strcmp(token, "8604a8f9-35e4-44b7-b6a6-d486396d3ed2")) return NULL;
    return calloc(1, sizeof(State));
}
void fmi3FreeInstance(void *s) { free(s); }
int fmi3EnterInitializationMode(void *s, bool td, double tol, double start,
    bool sd, double stop) { ((State *)s)->time = start; return 0; }
int fmi3ExitInitializationMode(void *s) { ((State *)s)->ready = true; return 0; }
int fmi3SetFloat64(void *s, const uint32_t *vr, size_t nr, const double *v, size_t nv) {
    return nr == 1 && vr[0] == 101 && nv == 4 && v[0] == 1000 ? 0 : 3;
}
int fmi3GetFloat64(void *s, const uint32_t *vr, size_t nr, double *v, size_t nv) {
    for (size_t k = 0; k < nv; ++k) v[k] = 0;
    if (vr[0] == 202 && nv == 3) v[0] = 12;
    if (vr[0] == 204 && nv == 4) v[0] = 1;
    return 0;
}
int fmi3DoStep(void *s, double t, double h, bool no_rollback, bool *event,
    bool *terminate, bool *early, double *last) {
    State *state = s;
    if (!state->ready || fabs(t-state->time) > 1e-12) return 3;
    *event = *terminate = *early = false;
    *last = state->time = t+h;
    return 0;
}
int fmi3Terminate(void *s) { return 0; }
""")
    library = tmp_path/"probe.so"
    subprocess.run(["cc", "-shared", "-fPIC", str(source), "-lm", "-o", str(library)], check=True)
    driver = tmp_path/"driver.c"
    driver.write_text(r"""
#include "physics_engines/fmu/fmu.h"
#include <stdio.h>
int main(int argc, char **argv) {
    fmu_init(argc-1, argv+1);
    if (!fmu_backend.init()) return 2;
    for (int i = 1; i <= 200; ++i) {
        if (!fmu_backend.advance_simulation(i * 0.001)) return 3;
    }
    fmu_backend.shutdown();
    puts("advanced=0.200");
    return 0;
}
""")
    driver_bin = tmp_path/"driver"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-I", str(REPO/"virtuals/physics"), str(driver),
                    str(REPO/"virtuals/physics/physics_engines/fmu/fmu.c"),
                    "-ldl", "-lm", "-o", str(driver_bin)], check=True)
    return driver_bin, library


def test_native_loader_uses_xml_token_and_advances(build, tmp_path, native_probe):
    driver, library = native_probe
    package(build, binary=library.read_bytes())
    runtime = fmu_runtime.prepare_runtime(build, tmp_path/"run")
    # Put fmu_token before fmu to catch prefix-only argument lookup regressions.
    args = [str(driver), f"fmu_token={runtime.instantiation_token}",
            f"fmu={runtime.library_path}", f"fmu_vr_pwm={runtime.value_references['pwm']}"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout+result.stderr
    assert "advanced=0.200" in result.stdout
    assert "truth_valid=0" in result.stdout
    diagnostic_args = ['fmu_vr_truth.p_w=202', 'fmu_vr_truth.v_w=203',
                       'fmu_vr_truth.q_wb=204', 'fmu_vr_truth.omega_b=205']
    result = subprocess.run(args + diagnostic_args, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'truth_valid=1' in result.stdout
    args[1] = "fmu_token=ConfiguredQuad-rumoca"
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "fmi3InstantiateCoSimulation failed" in result.stderr
    result = subprocess.run([args[0], *args[2:]], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "requires fmu_token=" in result.stderr
