#!/usr/bin/env python3
"""Archive, verify, replay or rerun a portable Betaflight reference flight."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

from flight_recording import ROOT, capture_provenance, digest, read_recording, repository_path, tomllib


DEFAULT_REFERENCE = ROOT / 'virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr/reference_flight'
FORMAT = 'fastdyn.betaflight.reference-flight'


def bundle_file(folder, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('reference file paths must be nonempty strings')
    folder = Path(folder).resolve()
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder):
        raise ValueError('reference file path leaves the bundle directory')
    return path


def load_reference(path):
    path = Path(path).resolve()
    if path.is_dir():
        path /= 'manifest.json'
    header = json.loads(path.read_text())
    if not isinstance(header, dict) or header.get('format') != FORMAT:
        return read_recording(path), None, path.parent
    if header.get('schema_version') != 1:
        raise ValueError('unsupported reference schema version')
    for key in ('recording', 'runtime'):
        record = header.get(key, {})
        if not isinstance(record, dict):
            raise ValueError(f'invalid {key} metadata')
        file = bundle_file(path.parent, record.get('file'))
        if digest(file) != record.get('sha256'):
            raise ValueError(f'{key} checksum mismatch: {file.name}')
    data = read_recording(bundle_file(path.parent, header['recording']['file']))
    if header.get('mission') != mission_parameters(data):
        raise ValueError('manifest mission parameters do not match the recording')
    return data, header, path.parent


def mission_parameters(data):
    mission = data['mission']
    altitude = mission.get('altitude')
    waypoints = mission.get('waypoints')
    if type(altitude) not in (float, int) or not math.isfinite(altitude) or altitude <= 0:
        raise ValueError('reference mission needs a positive target altitude')
    if not isinstance(waypoints, list) or any(not isinstance(p, list) or len(p) != 2 or
       any(type(v) not in (float, int) or not math.isfinite(v) for v in p) for p in waypoints):
        raise ValueError('reference mission needs finite north/east waypoint pairs')
    return {'altitude_m': altitude, 'waypoints_ne_m': waypoints, 'home_lat_lon_alt': mission['home']}


def archive(recording, destination, config, *, root=ROOT):
    root = Path(root).resolve()
    recording, destination, config = Path(recording).resolve(), Path(destination).resolve(), Path(config).resolve()
    if destination.exists():
        raise FileExistsError(f'reference directory already exists: {destination}')
    data = read_recording(recording)
    mission = mission_parameters(data)
    provenance = data.get('provenance') or {**capture_provenance(config, root=root), 'captured_during_run': False}
    components = provenance.get('components', [])
    recorded_config = next((c for c in components if c['role'] == 'runtime_config'), None)
    if recorded_config and recorded_config['sha256'] != digest(config):
        raise ValueError('selected configuration differs from the one recorded with this flight')
    parsed = tomllib.loads(config.read_text())
    cpu = parsed['CPU']['cpu0'][0]
    observed = recording.parent / 'virtuals/virtuals.txt'
    expected = repository_path(root, cpu['existing_config_path']) / 'virtuals.txt'
    provenance = dict(provenance)
    if observed.is_file() and expected.is_file():
        provenance['observed_slicing_rules_match'] = digest(observed) == digest(expected)
        if not provenance['observed_slicing_rules_match']:
            raise ValueError('recording work directory has different slicing rules from this configuration')
    home = mission['home_lat_lon_alt']
    last = data['history'][-1]
    north = (last['lat'] - home[0]) * 111320
    east = (last['lon'] - home[1]) * 111320 * math.cos(math.radians(home[0]))
    manifest = {
        'format': FORMAT, 'schema_version': 1,
        'title': Path(cpu['binary']).stem.rsplit('_', 1)[-1] + ' reference flight',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'recording': {'file': 'flight.json', 'sha256': digest(recording),
                      'samples': len(data['history']), 'duration_seconds': last['t'],
                      'position_source': 'firmware MAVLink GLOBAL_POSITION_INT',
                      'units': {'t': 'wall seconds since first recorded position', 'lat_lon': 'degrees',
                                'alt': 'metres WGS84 in this plant', 'attitude': 'radians', 'speed_climb': 'metres/second'}},
        'runtime': {'file': 'runtime.toml', 'sha256': digest(config),
                    'original_config_path': config.relative_to(root).as_posix()},
        'mission': mission,
        'result': {'waypoints_reached': data['mission'].get('waypoint_index'),
                   'planned_waypoints': len(mission['waypoints_ne_m']),
                   'max_relative_altitude_m': max(p['alt'] - home[2] for p in data['history']),
                   'final_relative_altitude_m': last['alt'] - home[2],
                   'final_horizontal_distance_m': math.hypot(north, east),
                   'armed_at_completion': data['mission'].get('armed')},
        'provenance': provenance,
        'reproduction': {'visual_replay': 'serve the saved flight samples; QEMU and the FMU are not started',
                         'mission_rerun': 'run the saved configuration and route with validated firmware/source components',
                         'determinism': 'host RC scheduling uses wall time; a rerun need not reproduce the same trajectory exactly'}
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.flight-reference-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        shutil.copyfile(recording, staging / 'flight.json')
        shutil.copyfile(config, staging / 'runtime.toml')
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        staging.rename(destination)
    return destination


def component_differences(manifest, *, root=ROOT):
    differences = []
    for component in manifest.get('provenance', {}).get('components', []):
        if component.get('role') == 'runtime_config':
            continue  # Reruns use the checksummed snapshot in the bundle.
        path = repository_path(root, component['path'])
        if not path.is_file() or digest(path) != component['sha256']:
            differences.append(component['path'])
    return differences


def play(data, *, port=15500, camera='overview'):
    from fastdyn_flight_view import FlightViewServer
    config = SimpleNamespace(APP_PREFIX='mavcesium/', SERVER_PORT=port, SERVER_INTERFACE='127.0.0.1')
    with tempfile.TemporaryDirectory(prefix='fastdyn-replay-') as work:
        server = FlightViewServer(config, work)
        server.recording.history = data['history']
        server.recording.mission = data['mission']
        server.recording.telemetry = data.get('telemetry', {})
        server.recording.provenance = data.get('provenance', {})
        server.recording.saved = True
        server.start()
        print(f'Replaying {len(data["history"])} saved flight samples; simulation is not running.', flush=True)
        print(f'Open http://127.0.0.1:{port}/mavcesium/?replay=1&camera={camera}', flush=True)
        print('For SSH, forward this TCP port from your local computer. Ctrl+C stops the replay server.', flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
            if server.thread:
                server.thread.join(timeout=5)


def rerun(reference, *, output, dry_run=False, allow_differences=False, root=ROOT):
    data, manifest, folder = load_reference(reference)
    if manifest is None:
        raise ValueError('mission rerun requires an archived reference manifest')
    differences = component_differences(manifest, root=root)
    if differences and not allow_differences:
        raise ValueError('reference components differ; review before using --allow-differences: ' + ', '.join(differences))
    if not any(c.get('role') == 'firmware' for c in manifest.get('provenance', {}).get('components', [])):
        raise ValueError('reference has no firmware identity')
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'rerun output already exists: {output}')
    config = bundle_file(folder, manifest['runtime']['file'])
    executable = Path(root) / 'fastdyn-env/bin/fastdyn'
    command = [str(executable) if executable.is_file() else 'fastdyn', 'run', '-c', str(config), '-o', str(output)]
    mission = mission_parameters(data)
    overrides = {'FASTDYN_BF_MISSION_ALT': str(mission['altitude_m']),
                 'FASTDYN_BF_WAYPOINTS': ';'.join(','.join(map(str, p)) for p in mission['waypoints_ne_m'])}
    print(json.dumps({'command': command, 'mission_environment': overrides,
                      'component_differences': differences}, indent=2), flush=True)
    if dry_run:
        return 0
    return subprocess.run(command, cwd=root, env={**os.environ, **overrides}).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    replay = sub.add_parser('play', help='view saved telemetry without starting QEMU')
    replay.add_argument('reference', nargs='?', type=Path, default=DEFAULT_REFERENCE)
    replay.add_argument('--port', type=int, default=15500)
    replay.add_argument('--camera', choices=('overview', 'top', 'follow'), default='overview')
    save = sub.add_parser('archive', help='save JSON, frozen runtime configuration and provenance for GitHub')
    save.add_argument('recording', type=Path)
    save.add_argument('--output', type=Path, required=True)
    save.add_argument('--config', type=Path, default=ROOT / 'configs/betaflight_f405.toml')
    check = sub.add_parser('verify', help='verify bundle checksums, optionally checking rerun components')
    check.add_argument('reference', nargs='?', type=Path, default=DEFAULT_REFERENCE)
    check.add_argument('--components', action='store_true')
    run = sub.add_parser('rerun', help='launch a fresh mission using the saved configuration and route')
    run.add_argument('reference', nargs='?', type=Path, default=DEFAULT_REFERENCE)
    run.add_argument('--output', type=Path, default=ROOT / 'out/betaflight/reproductions' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    run.add_argument('--dry-run', action='store_true')
    run.add_argument('--allow-differences', action='store_true')
    video = sub.add_parser('video', help='render the saved reference to MP4 using optional Playwright/Chrome')
    video.add_argument('reference', nargs='?', type=Path, default=DEFAULT_REFERENCE)
    video.add_argument('--output', type=Path, default=ROOT / 'out/betaflight/replays' / f'reference-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.mp4')
    video.add_argument('--camera', choices=('overview', 'top', 'follow'), default='overview')
    video.add_argument('--browser')
    args = parser.parse_args(argv)
    try:
        if args.action == 'archive':
            print(f'Saved portable reference: {archive(args.recording, args.output, args.config)}')
        elif args.action == 'rerun':
            return rerun(args.reference, output=args.output, dry_run=args.dry_run, allow_differences=args.allow_differences)
        elif args.action == 'video':
            from export_betaflight_video import export_video
            data, _, _ = load_reference(args.reference)
            export_video(data, args.output, camera=args.camera, browser_path=args.browser)
        else:
            data, manifest, _ = load_reference(args.reference)
            if args.action == 'play':
                if not 1 <= args.port <= 65535:
                    raise ValueError('port must be between 1 and 65535')
                play(data, port=args.port, camera=args.camera)
            else:
                if args.components and manifest is None:
                    raise ValueError('component verification requires an archived reference manifest')
                if args.components:
                    differences = component_differences(manifest)
                    if differences:
                        raise ValueError('reference components differ: ' + ', '.join(differences))
                print(f'Verified {len(data["history"])} flight samples, {data["history"][-1]["t"]:.1f} seconds')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f'Cannot {args.action} reference flight: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
