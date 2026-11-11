"""Reject unusable replay inputs before starting a browser or overwriting a video."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'virtuals/physics/flight_controllers/courbet/mavlink/export_betaflight_video.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('bf_video', SCRIPT)
video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video)


def recording():
    sample = {'t': 0.0, 'lat': 40.4, 'lon': -86.9, 'alt': 149.1,
              'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0, 'speed': 0.0, 'climb': 0.0}
    return {'mission': {'phase': 'COMPLETE', 'home': [40.4, -86.9, 149.1]},
            'history': [sample, {**sample, 't': 1.0}]}


def test_saved_completed_recording_is_read_without_optional_rendering_tools(tmp_path):
    path = tmp_path / 'flight.json'
    data = recording()
    path.write_text(json.dumps(data))
    assert video.read_recording(path) == data


@pytest.mark.parametrize('change', [
    lambda data: [],
    lambda data: {**data, 'mission': None},
    lambda data: {**data, 'history': []},
    lambda data: {**data, 'history': [None, None]},
    lambda data: {**data, 'mission': {**data['mission'], 'phase': 'NAV'}},
    lambda data: {**data, 'history': [{**data['history'][0], 't': -0.1}, data['history'][1]]},
    lambda data: {**data, 'history': [data['history'][0], data['history'][0]]},
    lambda data: {**data, 'history': [data['history'][0], {**data['history'][1], 'alt': float('nan')}]},
])
def test_invalid_recordings_are_rejected(tmp_path, change):
    path = tmp_path / 'flight.json'
    path.write_text(json.dumps(change(recording())))
    with pytest.raises(ValueError):
        video.read_recording(path)
