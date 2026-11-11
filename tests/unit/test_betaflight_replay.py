"""Portable reference integrity, component identity and explicit rerun semantics."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / 'virtuals/physics/flight_controllers/courbet/mavlink'
sys.path.insert(0, str(HELPERS))
spec = importlib.util.spec_from_file_location('bf_replay', HELPERS / 'replay_betaflight.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / 'flight.json'
    sample = {'t': 0, 'lat': 40.4, 'lon': -86.9, 'alt': 149.1,
              'roll': 0, 'pitch': 0, 'yaw': 0, 'speed': 0, 'climb': 0}
    data = {'mission': {'phase': 'COMPLETE', 'home': [40.4, -86.9, 149.1],
                        'altitude': 15, 'waypoints': [[30, 0]], 'waypoint_index': 1, 'armed': False},
            'history': [sample, {**sample, 't': 2, 'alt': 164.1}, {**sample, 't': 4}]}
    source.write_text(json.dumps(data))
    (tmp_path / 'firmware.elf').write_bytes(b'firmware identity fixture')
    config = tmp_path / 'runtime.toml'
    config.write_text('''[[CPU.cpu0]]
binary = "firmware.elf"
existing_config_path = "rules/unlabeled_conf"
''')
    reference = replay.archive(source, tmp_path / 'reference', config, root=tmp_path)
    return tmp_path, source, reference, data


def test_archive_keeps_telemetry_and_frozen_configuration(workspace):
    root, original, reference, expected = workspace
    data, manifest, folder = replay.load_reference(reference)
    assert data == expected
    assert (reference / 'flight.json').read_bytes() == original.read_bytes()
    assert manifest['recording']['samples'] == 3
    assert manifest['result']['max_relative_altitude_m'] == 15
    assert manifest['result']['final_horizontal_distance_m'] == 0
    assert manifest['provenance']['captured_during_run'] is False
    assert replay.component_differences(manifest, root=root) == []
    assert folder == reference


@pytest.mark.parametrize('filename', ['flight.json', 'runtime.toml'])
def test_bundle_tampering_is_rejected(workspace, filename):
    _, _, reference, _ = workspace
    file = reference / filename
    file.write_text(file.read_text() + '\n')
    with pytest.raises(ValueError, match='checksum mismatch'):
        replay.load_reference(reference)


def test_manifest_cannot_read_files_outside_its_bundle(workspace):
    _, _, reference, _ = workspace
    path = reference / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['recording']['file'] = '../flight.json'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='leaves the bundle'):
        replay.load_reference(reference)


def test_changed_firmware_prevents_rerun_before_any_process_starts(workspace, monkeypatch):
    root, _, reference, _ = workspace
    (root / 'firmware.elf').write_bytes(b'different firmware')
    monkeypatch.setattr(replay.subprocess, 'run', lambda *a, **k: pytest.fail('started a process'))
    with pytest.raises(ValueError, match='components differ'):
        replay.rerun(reference, output=root / 'new-run', root=root)


def test_dry_run_uses_frozen_config_and_recorded_mission_without_starting_qemu(workspace, monkeypatch, capsys):
    root, _, reference, _ = workspace
    # The archived config remains usable if the user's default config changes.
    (root / 'runtime.toml').write_text('changed current default')
    monkeypatch.setattr(replay.subprocess, 'run', lambda *a, **k: pytest.fail('started a process'))
    assert replay.rerun(reference, output=root / 'new-run', dry_run=True, root=root) == 0
    launch = json.loads(capsys.readouterr().out)
    assert str(reference / 'runtime.toml') in launch['command']
    assert launch['mission_environment']['FASTDYN_BF_MISSION_ALT'] == '15'
    assert launch['mission_environment']['FASTDYN_BF_WAYPOINTS'] == '30,0'
    assert not (root / 'new-run').exists()


def test_references_are_not_overwritten(workspace):
    root, original, reference, _ = workspace
    with pytest.raises(FileExistsError):
        replay.archive(original, reference, root / 'runtime.toml', root=root)


def test_manifest_cannot_change_mission_independently_of_telemetry(workspace):
    _, _, reference, _ = workspace
    path = reference / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['mission']['altitude_m'] = 30
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='parameters do not match'):
        replay.load_reference(reference)
