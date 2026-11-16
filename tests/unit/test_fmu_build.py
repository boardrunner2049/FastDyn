from pathlib import Path
import xml.etree.ElementTree as ET

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


def test_partial_kinematics_group_reports_missing_outputs(tmp_path):
    build = make_build(tmp_path, {})
    write_model_description(build.output)
    path = build.output / "modelDescription.xml"
    xml = ET.parse(path)
    variable = ET.SubElement(xml.find("ModelVariables"), "Float64", name="alpha_frd",
                             valueReference="88", causality="output")
    ET.SubElement(variable, "Dimension", start="3")
    xml.write(path)
    with pytest.raises(fmu_build.FmuConfigError, match="pos_ned"):
        fmu_build.value_references(build)


def test_scalar_kinematics_cannot_be_read_as_vector(tmp_path):
    build = make_build(tmp_path, {})
    write_model_description(build.output, ("alpha_frd",))
    with pytest.raises(fmu_build.FmuConfigError, match="shape"):
        fmu_build.value_references(build)


def test_copter_rotation_requires_two_dimensions(tmp_path):
    build = make_build(tmp_path, {})
    xml = ET.Element("fmiModelDescription")
    rotation = ET.SubElement(ET.SubElement(xml, "ModelVariables"), "Float64",
                             name="rotation", valueReference="1", causality="output")
    ET.SubElement(rotation, "Dimension", start="9")
    ET.ElementTree(xml).write(build.output / "modelDescription.xml")
    with pytest.raises(fmu_build.FmuConfigError, match=r"rotation.*\(3, 3\)"):
        fmu_build.value_references(build)
