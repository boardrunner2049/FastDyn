"""Real FIRE P0/P1 FMI 3 composition, packaging and stepping.

FASTDYN_TEST_RUMOCA_DIR=third_party/common/rumoca PYTHONPATH=src python3 -m unittest discover \
    -s tests/integration -p test_fire_propulsion_profiles.py
Requires the local Modelica library and FMI 3 headers used by the normal build.
"""
import ctypes as C
import math
import json
import os
from pathlib import Path
import tomllib
import xml.etree.ElementTree as ET

import tempfile
import unittest

from fastdyn.fmu_build import FmuBuild, build_fmu
from fastdyn.fmu_runtime import prepare_runtime


class Fmi3Probe:
    """Minimal array-aware CS host; every value reference comes from this artifact."""

    def __init__(self, runtime, description):
        self.library = C.CDLL(str(runtime.library_path))
        self.variables = {v.attrib['name']: v for v in description.find('ModelVariables')}
        self.bind('fmi3InstantiateCoSimulation', [C.c_char_p, C.c_char_p, C.c_char_p,
                  C.c_bool, C.c_bool, C.c_bool, C.c_bool, C.POINTER(C.c_uint32),
                  C.c_size_t, C.c_void_p, C.c_void_p, C.c_void_p], C.c_void_p)
        self.bind('fmi3EnterInitializationMode', [C.c_void_p, C.c_bool, C.c_double,
                                                C.c_double, C.c_bool, C.c_double])
        self.bind('fmi3ExitInitializationMode', [C.c_void_p])
        for name in ('fmi3SetFloat64', 'fmi3GetFloat64'):
            self.bind(name, [C.c_void_p, C.POINTER(C.c_uint32), C.c_size_t,
                             C.POINTER(C.c_double), C.c_size_t])
        self.bind('fmi3DoStep', [C.c_void_p, C.c_double, C.c_double, C.c_bool,
                  C.POINTER(C.c_bool), C.POINTER(C.c_bool), C.POINTER(C.c_bool), C.POINTER(C.c_double)])
        self.bind('fmi3FreeInstance', [C.c_void_p], None)
        self.instance = self.fmi3InstantiateCoSimulation(
            b'fire-profile-probe', runtime.instantiation_token.encode(),
            (runtime.resource_path or '').encode(), False, False, False, False,
            None, 0, None, None, None)
        assert self.instance

    def bind(self, name, arguments, result=C.c_int):
        function = getattr(self.library, name)
        function.argtypes, function.restype = arguments, result
        setattr(self, name, function)

    def size(self, name):
        return math.prod(int(d.attrib['start']) for d in self.variables[name].findall('Dimension'))

    def transfer(self, name, values=None):
        count = self.size(name)
        ref = (C.c_uint32 * 1)(int(self.variables[name].attrib['valueReference']))
        if values is None:
            data = (C.c_double * count)()
            status = self.fmi3GetFloat64(self.instance, ref, 1, data, count)
        else:
            assert len(values) == count
            data = (C.c_double * count)(*values)
            status = self.fmi3SetFloat64(self.instance, ref, 1, data, count)
        assert status <= 1, (name, status)
        return list(data)

    def initialize(self, pwm):
        assert self.fmi3EnterInitializationMode(self.instance, False, 0, 0, False, 0) <= 1
        self.transfer('pwm', pwm)
        assert self.fmi3ExitInitializationMode(self.instance) <= 1

    def step(self, time, dt):
        event, terminate, early, last = C.c_bool(), C.c_bool(), C.c_bool(), C.c_double()
        status = self.fmi3DoStep(self.instance, time, dt, True, C.byref(event),
                               C.byref(terminate), C.byref(early), C.byref(last))
        assert status <= 1
        assert not (event.value or terminate.value or early.value)

    def close(self):
        self.fmi3FreeInstance(self.instance)


def check_fire_profile_fmi3_response(tmp_path, config_name, voltage_scale):
    repo = Path(__file__).resolve().parents[2]
    fire = repo / 'third_party/courbet_deps/fire_modelica_models'
    raw = (fire / 'configs' / config_name).read_text()
    if voltage_scale != 1:
        raw = raw.replace('dcVoltage = 16.0', f'dcVoltage = {16*voltage_scale}')
    config = tomllib.loads(raw)
    source = tmp_path / 'physics.toml'
    source.write_text(raw)
    model = config['model_name']
    build = FmuBuild(name='probe', model=model, model_file=tmp_path/'composed'/f'{model}.mo',
                     source_root=tmp_path/'composed/sources', extra_source_roots=(),
                     output=tmp_path/'fmi3', repo_root=repo, compose_config=source,
                     compiler_root=Path(os.environ['FASTDYN_TEST_RUMOCA_DIR']).resolve())
    build_fmu(build, update_submodules_first=False)
    manifest = json.loads(build.model_file.with_suffix('.manifest.json').read_text())
    assert manifest['profile'] == 'vio'
    assert manifest['input_adapter']['class'] == 'fire_modelica_models.Adapters.VirtualIO.PwmInput'
    assert manifest['input_adapter']['output_contract'] == 'normalized_pwm_command_v1'
    runtime = prepare_runtime(build, tmp_path/'runtime')
    description = ET.parse(runtime.library_path.parents[2]/'modelDescription.xml').getroot()
    variables = list(description.find('ModelVariables'))
    assert {v.attrib['name'] for v in variables if v.get('causality') == 'input'} == {'pwm'}
    host = Fmi3Probe(runtime, description)
    try:
        mapping = config['geometry']['actuatorIndex']
        channels = config['geometry']['nActuators']
        assert host.size('pwm') == channels
        assert host.size('rotorSpeed') == len(mapping)
        raw_command = [-0.1, 0, 0.25, 1.1, 0.4, 0.6, 0.7, 0.8][:channels]
        demand = [min(1, max(0, u)) for u in raw_command]
        limits = config['interface']
        host.initialize([limits['pwm_min']+(limits['pwm_max']-limits['pwm_min'])*u
                         for u in raw_command])
        dt, steps = 0.0001, 800
        for tick in range(steps):
            host.step(tick*dt, dt)
        motor = config['motor']
        p1 = config.get('models', {}).get('rotor') == 'voltage_driven'
        gain = motor['Kv'] if p1 else motor['omegaMax']
        tau = motor['tauOmega'] if p1 else motor['tau']
        expected = []
        for i, channel in enumerate(mapping):
            speed_gain = gain[i] if isinstance(gain, list) else gain
            voltage = config['power']['dcVoltage'] if p1 else 1
            expected.append(speed_gain*voltage*demand[channel-1]*(1-math.exp(-steps*dt/tau)))
        actual = host.transfer('rotorSpeed')
        assert all(abs(a-b)<1e-3 for a,b in zip(actual,expected)), (actual,expected)
        for name in ('accel', 'gyro', 'gps', 'vel_ned', 'baro_altitude_m'):
            assert all(math.isfinite(v) for v in host.transfer(name))
        if p1:
            assert abs(host.transfer('plant.dcVoltage')[0]-config['power']['dcVoltage'])<1e-9
    finally:
        host.close()


@unittest.skipUnless(os.environ.get('FASTDYN_TEST_RUMOCA_DIR'), 'requires Rumoca and FMI3 headers')
class FirePropulsionFmi3Tests(unittest.TestCase):
    def check_profile(self, name, voltage_scale=1):
        with tempfile.TemporaryDirectory(prefix='fire-fmi3-') as work:
            check_fire_profile_fmi3_response(Path(work), name, voltage_scale)

    def test_p0_quad(self):
        self.check_profile('quad.toml')

    def test_p1_quad(self):
        self.check_profile('quad_p1.toml')

    def test_p1_quad_half_voltage(self):
        self.check_profile('quad_p1.toml', 0.5)

    def test_p1_hexa(self):
        self.check_profile('hex_p1.toml')
