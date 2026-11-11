"""Portable flight data validation and provenance shared by replay and recording."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess

try:
    import tomllib
except ImportError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[5]
SAMPLE_FIELDS = ('t', 'lat', 'lon', 'alt', 'roll', 'pitch', 'yaw', 'speed', 'climb')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_recording(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get('mission'), dict) or not isinstance(data.get('history'), list):
        raise ValueError('choose a FastDyn flight recording JSON object')
    history = data.get('history', [])
    home = data.get('mission', {}).get('home', [])
    if len(history) < 2 or not isinstance(home, list) or len(home) != 3 or not all(type(v) in (int, float) and math.isfinite(v) for v in home):
        raise ValueError('recording needs a home position and at least two flight samples')
    previous = -1.0
    for sample in history:
        if not isinstance(sample, dict) or not all(type(sample.get(k)) in (int, float) and math.isfinite(sample[k]) for k in SAMPLE_FIELDS):
            raise ValueError('recording contains an invalid flight sample')
        if sample['t'] < 0 or sample['t'] <= previous:
            raise ValueError('recording sample times must increase from zero or later')
        previous = sample['t']
    if data['mission'].get('phase') != 'COMPLETE':
        raise ValueError('replay currently requires a completed flight recording')
    return data


def repository_path(root, path):
    """Resolve a component within the checkout; manifests cannot select external commands."""
    root = Path(root).resolve()
    result = (root / path).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f'component path leaves the repository: {path}')
    return result


def capture_provenance(config_path, *, root=ROOT):
    root = Path(root).resolve()
    config_path = Path(config_path).resolve()
    config = tomllib.loads(config_path.read_text())
    components = []

    def add(role, path):
        absolute = repository_path(root, path)
        if absolute.is_file():
            components.append({'role': role, 'path': absolute.relative_to(root).as_posix(), 'sha256': digest(absolute)})

    add('runtime_config', config_path)
    cpu = config['CPU']['cpu0'][0]
    add('firmware', cpu['binary'])
    rules = repository_path(root, cpu['existing_config_path'])
    add('profile', rules.parent / 'profile.toml')
    add('sensor_rules', rules.parent / 'labeled_conf/virtuals.txt')
    add('hardware_substitutions', rules.parent / 'labeled_conf/modifiers.txt')
    # Release sweeps use separate ABI and guest-parameter artifacts alongside
    # the ELF/profile. Keep their source identity and settings in new captures.
    for role, filename in [('version_manifest','abi.json'),
                           ('runtime_parameters','parameters.txt'),
                           ('board_configuration','MTKS-MATEKF405CTR.config')]:
        if (rules.parent / filename).is_file():
            add(role, rules.parent / filename)
    for path in ['virtuals/physics/flight_controllers/betaflight/betaflight.c',
                 'virtuals/physics/flight_controllers/betaflight/hardware.c',
                 'virtuals/physics/flight_controllers/courbet/mavlink/mav_mission_betaflight.py',
                 'virtuals/physics/flight_controllers/courbet/profiles/betaflight/__init__.py']:
        add('adapter_source', path)
    model = config.get('FMU', {}).get('active')
    physics = config.get('FMU', {}).get('models', {}).get(model, {})
    if physics.get('model_file'):
        add('physics_source', physics['model_file'])

    checkouts = {}
    for name, folder in [('fastdyn', root), ('betaflight', root.parent / 'betaflight'),
                         ('board_configs', root.parent / 'betaflight/src/config'),
                         ('modelica_models', root / 'third_party/common/modelica_models')]:
        try:
            commit = subprocess.check_output(['git', '-C', str(folder), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
            dirty = subprocess.check_output(['git', '-C', str(folder), 'status', '--porcelain', '--untracked-files=no'], text=True, stderr=subprocess.DEVNULL)
            checkouts[name] = {'commit': commit, 'tracked_changes': bool(dirty)}
        except (OSError, subprocess.CalledProcessError):
            pass
    return {'captured_at_utc': datetime.now(timezone.utc).isoformat(),
            'config_path': config_path.relative_to(root).as_posix(),
            'components': components, 'checkouts_at_capture': checkouts,
            'physics': {'model': physics.get('model'), 'parameters': physics.get('parameters', {})},
            'timing': {'firmware': 'QEMU instruction-counted virtual time',
                       'host_pilot': 'monotonic wall time; mission reruns are not bit-for-bit deterministic'}}
