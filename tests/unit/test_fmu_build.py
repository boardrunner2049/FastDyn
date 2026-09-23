from pathlib import Path

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


def test_fire_examples_resolve_generated_sources():
    root = Path(__file__).resolve().parents[2]
    for shape, name in (("quad", "FIREQuad"), ("hexa", "FIREHexa")):
        build = fmu_build.resolve(root / f"configs/copter462_{shape}.toml", repo_root=root)
        assert build.model == name
        assert build.compose_config == root / f"configs/copter462_{shape}.toml"
        assert build.compose_table == f'FMU.models."{shape}".physics'
        assert build.model_file == root / f"out/fire/{shape}/{name}.mo"
        assert build.source_root == root / f"out/fire/{shape}/sources"


def test_fire_cache_tracks_config_and_generator_changes(tmp_path):
    from dataclasses import replace
    from fastdyn.physics_composition import FIRE_REL, input_digest

    config = tmp_path / "vehicle.toml"
    config.write_text('schema_version=2\n')
    generator = tmp_path / FIRE_REL / "tools/fire-compose/src/main.rs"
    generator.parent.mkdir(parents=True)
    generator.write_text('fn main() {}')
    build = replace(make_build(tmp_path, {}), compose_config=config, config_path=config)
    build.artifact_path.write_text('<fmiModelDescription/>')
    stamp = build.output / "fire-build.sha256"
    assert fmu_build.needs_build(build)
    stamp.write_text(input_digest(build))
    assert not fmu_build.needs_build(build)
    config.write_text('schema_version=2\nacquisition="continuous"\n')
    assert fmu_build.needs_build(build)
    stamp.write_text(input_digest(build))
    generator.write_text('fn main() { println!("changed"); }')
    assert fmu_build.needs_build(build)


def test_new_rumoca_source_package_metadata(tmp_path):
    build = make_build(tmp_path, {})
    nested = build.output / build.model.replace('.', '_')
    nested.mkdir()
    write_model_description(nested)
    assert build.artifact_path == nested / 'modelDescription.xml'
    assert set(fmu_build.value_references(build)) == set(fmu_build.REQUIRED_VALUE_REFERENCES)
    assert '--build' not in fmu_build.cargo_command(build, legacy_build=False)


def test_inline_physics_selection_and_cache(tmp_path):
    from dataclasses import replace
    from fastdyn.physics_composition import input_digest

    config = tmp_path / "vehicle.toml"
    config.write_text('''[FMU]
active = "quad"
[FMU.models.quad]
model = "Quad"
model_file = "Quad.mo"
source_root = "."
[FMU.models.quad.physics]
schema_version = 2
model_name = "Quad"
[FMU.models.quad.physics.geometry]
preset = "QuadX"
[FMU.models.hexa]
model = "Hexa"
model_file = "Hexa.mo"
source_root = "."
[FMU.models.hexa.physics]
schema_version = 2
model_name = "Hexa"
[FMU.models.hexa.physics.geometry]
preset = "HexaX"
''')
    quad = fmu_build.resolve(config, repo_root=tmp_path)
    hexa = fmu_build.resolve(config, "hexa", repo_root=tmp_path)
    assert quad.compose_table == 'FMU.models."quad".physics'
    assert hexa.compose_table == 'FMU.models."hexa".physics'
    assert quad.compose_config == hexa.compose_config == config
    before = input_digest(quad)
    assert input_digest(replace(quad, compose_table=hexa.compose_table)) != before
    config.write_text(config.read_text().replace('preset = "QuadX"', 'preset = "OctoX"'))
    assert input_digest(quad) != before


@pytest.mark.parametrize(("entry", "physics", "message"), [
    ('physics = "vehicle.toml"', '', 'physics must be a table'),
    ('compose = "vehicle.toml"', '[FMU.physics]\nmodel_name = "Quad"', 'either an inline physics table'),
    ('', '[FMU.physics]\nmodel_name = "Other"', 'physics.model_name must match model'),
])
def test_invalid_inline_physics_is_rejected(tmp_path, monkeypatch, entry, physics, message):
    monkeypatch.delenv("FASTDYN_FIRE_CONFIG", raising=False)
    config = tmp_path / "invalid.toml"
    config.write_text('[FMU]\nmodel = "Quad"\nmodel_file = "Quad.mo"\nsource_root = "."\n'
                      + entry + '\n' + physics + '\n')
    with pytest.raises(fmu_build.FmuConfigError, match=message):
        fmu_build.resolve(config, repo_root=tmp_path)


def test_single_fmu_inline_physics_and_external_override(tmp_path, monkeypatch):
    config = tmp_path / "inline.toml"
    config.write_text('''[FMU]
model = "Quad"
model_file = "Quad.mo"
source_root = "."
[FMU.physics]
schema_version = 2
model_name = "Quad"
''')
    inline = fmu_build.resolve(config, repo_root=tmp_path)
    assert inline.compose_table == "FMU.physics"
    assert inline.compose_config == config
    monkeypatch.setenv("FASTDYN_FIRE_CONFIG", "custom.toml")
    external = fmu_build.resolve(config, repo_root=tmp_path)
    assert external.compose_config == tmp_path / "custom.toml"
    assert external.compose_table is None


def test_external_compose_file_still_supported(tmp_path, monkeypatch):
    monkeypatch.delenv("FASTDYN_FIRE_CONFIG", raising=False)
    config = tmp_path / "external.toml"
    config.write_text('''[FMU]
model = "Quad"
model_file = "Quad.mo"
source_root = "."
compose = "vehicle.toml"
''')
    build = fmu_build.resolve(config, repo_root=tmp_path)
    assert build.compose_config == tmp_path / "vehicle.toml"
    assert build.compose_table is None
