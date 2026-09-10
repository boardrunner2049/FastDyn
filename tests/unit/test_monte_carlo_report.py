import importlib.util
import json
from pathlib import Path

import pytest

from fastdyn.mission_report import Telemetry

spec = importlib.util.spec_from_file_location(
    'monte_carlo_report', Path(__file__).resolve().parents[2] / 'utils/monte_carlo_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_failed_partial_and_missing_runs_are_retained(tmp_path, monkeypatch):
    waypoints = [dict(sequence=0, latitude_deg=40., longitude_deg=-86.),
                 dict(sequence=1, latitude_deg=40.001, longitude_deg=-86.001)]
    monkeypatch.setattr(report, 'mission_waypoints', lambda _: waypoints)
    positions = [dict(boot_time_s=float(i), latitude_deg=40.+i*.00001,
                      longitude_deg=-86., relative_altitude_m=float(i),
                      mission_item=i, ground_speed_mps=1.) for i in range(3)]
    monkeypatch.setattr(report, 'log_messages', lambda p:
        Telemetry(positions=positions[:1] if p.stem == 'failed' else positions, armed_time_s=0.))
    monkeypatch.setattr(report, 'read_telemetry', lambda messages: messages)
    runs = []
    for name, status, mass in [('reference', 'reference', 0.), ('passed', 'passed', .05),
                               ('failed', 'flight_failure', .16), ('missing', 'run_error', .1)]:
        path = tmp_path / f'{name}.tlog'
        if name != 'missing':
            path.touch()
        runs.append(dict(id=name, status=status, payload_mass_kg=mass, log=str(path)))
    data = report.collect(dict(study={'title': 'Test experiment'}, mission='mission.waypoints', runs=runs))
    assert [len(r['rows']) for r in data['runs']] == [3, 3, 1, 0]
    assert data['runs'][-1]['status'] == 'run_error'
    stats = report.write_report(data, tmp_path / 'report')
    assert stats == {'runs': 4, 'trajectories': 3, 'without_trajectory': 1}
    svg = (tmp_path / 'report/trajectories.svg').read_text()
    assert svg.count('id="trajectory-') == 3
    assert svg.count('id="altitude-') == 3
    stored = json.loads((tmp_path / 'report/trajectories.json').read_text())
    assert len(stored['runs']) == 4


@pytest.mark.parametrize('mass', [-.01, float('nan'), float('inf')])
def test_invalid_payload_masses_are_rejected(monkeypatch, mass):
    monkeypatch.setattr(report, 'mission_waypoints', lambda _: [
        dict(sequence=0, latitude_deg=40., longitude_deg=-86.)])
    with pytest.raises(ValueError, match='payload mass'):
        report.collect(dict(study={}, mission='mission', runs=[
            dict(id='bad', status='run_error', payload_mass_kg=mass)]))
