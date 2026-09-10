from pathlib import Path
from dataclasses import replace

import pytest

from fastdyn import fmu_build


def make_build(tmp_path: Path, parameters: dict[str, float]) -> fmu_build.FmuBuild:
    output = tmp_path / "fmu"
    output.mkdir()
    return fmu_build.FmuBuild(
        name="quadrotor",
        model="FastDyn.Copter",
        model_file=tmp_path / "Copter.mo",
        source_root=tmp_path,
        extra_source_roots=(),
        output=output,
        repo_root=tmp_path,
        package=False,
        parameters=parameters,
    )


def write_model_description(path: Path, extra_names: tuple[str, ...] = ()) -> None:
    names = (*fmu_build.REQUIRED_VALUE_REFERENCES, *extra_names)
    variables = "\n".join(
        f'    <Float64 name="{name}" valueReference="{idx}" />'
        for idx, name in enumerate(names, start=1)
    )
    (path / "modelDescription.xml").write_text(
        f"""
<fmiModelDescription>
  <ModelVariables>
{variables}
  </ModelVariables>
</fmiModelDescription>
""",
        encoding="utf-8",
    )


def test_value_references_include_configured_fmu_parameters(tmp_path):
    build = make_build(tmp_path, {"pwm_min": 1100.0, "mass": 2.5644001})
    write_model_description(build.output, ("pwm_min", "mass"))

    refs = fmu_build.value_references(build)

    assert refs["pwm_min"] == len(fmu_build.REQUIRED_VALUE_REFERENCES) + 1
    assert refs["mass"] == len(fmu_build.REQUIRED_VALUE_REFERENCES) + 2


def test_value_references_reject_missing_configured_parameter(tmp_path):
    build = make_build(tmp_path, {"not_in_fmu": 1.0})
    write_model_description(build.output)

    with pytest.raises(fmu_build.FmuConfigError, match="not_in_fmu"):
        fmu_build.value_references(build)


def test_calculated_parameter_override_fails_before_launch(tmp_path):
    build = make_build(tmp_path, {"derived_mass": 1.0})
    write_model_description(build.output, ("derived_mass",))
    xml = build.output / "modelDescription.xml"
    xml.write_text(xml.read_text().replace('name="derived_mass"',
        'name="derived_mass" causality="calculatedParameter"'))
    with pytest.raises(fmu_build.FmuConfigError, match="calculated and cannot be overridden"):
        fmu_build.value_references(build)


def test_prebuilt_compiler_does_not_invoke_cargo(tmp_path):
    build = replace(make_build(tmp_path, {}), compiler="rumoca", release=True)
    command = fmu_build.cargo_command(build)
    assert command[:2] == ["rumoca", "compile"]
    assert "cargo" not in command
    assert "--release" not in command
    assert command[command.index("--model") + 1] == "FastDyn.Copter"
    assert command[-2:] == ["--target", "fmi3"]


def test_compiler_from_toml_and_empty_compiler_rejected(tmp_path):
    path = tmp_path / "run.toml"
    template = '''[FMU]
compiler = {compiler}
model = "FastDyn.Copter"
model_file = "Copter.mo"
source_root = "."
'''
    path.write_text(template.format(compiler='"rumoca"'))
    assert fmu_build.resolve(path, repo_root=tmp_path).compiler == "rumoca"
    path.write_text(template.format(compiler='""'))
    with pytest.raises(fmu_build.FmuConfigError, match="compiler"):
        fmu_build.resolve(path, repo_root=tmp_path)
