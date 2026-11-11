"""Exercise INAV firmware, native hooks, and the FMI 3 backend together."""
from pathlib import Path
import json
import os
import platform
import re
import signal
import socket
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile

import pytest

from fastdyn.fmu_build import REQUIRED_VALUE_REFERENCES
from fastdyn.targets.qemu_target import resolve_qemu_binary

ROOT = Path(__file__).resolve().parents[2]

MOCK = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <stdio.h>
typedef struct { double time; } State;
void *fmi3InstantiateCoSimulation(const char *n,const char *t,const char *r,bool v,bool l,bool e,bool x,const uint32_t *vr,size_t nr,void *env,void *cb,void *up) { return calloc(1,sizeof(State)); }
void fmi3FreeInstance(void *p) { free(p); }
int fmi3EnterInitializationMode(void *p,bool t,double tol,double start,bool s,double stop) { ((State *)p)->time=start; return 0; }
int fmi3ExitInitializationMode(void *p) { return 0; }
int fmi3SetFloat64(void *p,const uint32_t *vr,size_t nr,const double *v,size_t nv) {
  static bool recorded = false;
  if (!recorded && nr == 1 && vr[0] == 101 && nv == 4 && v[0] > 1100) {
    const char *path = getenv("FASTDYN_INAV_TEST_MARKER");
    if (path) { FILE *f = fopen(path, "w"); if (f) { fprintf(f, "%g", v[0]); fclose(f); recorded = true; } }
  }
  return 0;
}
int fmi3GetFloat64(void *p,const uint32_t *vr,size_t nr,double *v,size_t nv) {
  for(size_t i=0;i<nv;i++) v[i]=0;
  switch(vr[0]) {
  case 102:v[0]=40.414929;v[1]=-86.932387;v[2]=149;break;
  case 105:v[0]=0.21;v[2]=0.45;break;
  case 106:v[2]=-9.80665;break;
  case 108:v[0]=149;break; case 109:v[0]=99500;break; case 110:v[0]=21;break;
  default:break;
  } return 0;
}
int fmi3DoStep(void *p,double t,double h,bool n,bool *e,bool *term,bool *early,double *last) { *e=*term=*early=false; *last=((State *)p)->time=t+h; return 0; }
int fmi3Terminate(void *p) { return 0; }
'''


def _read_memory(sock, address, length):
    sock.write((json.dumps({'execute': 'human-monitor-command', 'arguments': {
        'command-line': f'xp /{length}bx {address:#x}'}}) + '\n').encode())
    response = json.loads(sock.readline())
    assert 'return' in response, response
    return bytes(int(x, 16) for x in re.findall(r'0x([0-9a-fA-F]{2})\b', response['return']))


def test_inav_firmware_reaches_sensor_and_motor_loop(tmp_path):
    binary = ROOT / 'fastdyn-env/bin/fastdyn'
    plugin = ROOT / 'build/libfastdyn.so'
    if not binary.exists() or not plugin.exists():
        pytest.skip('requires the FastDyn virtual environment and native plugin')
    try:
        qemu, _ = resolve_qemu_binary('', 'arm', str(plugin))
    except FileNotFoundError:
        pytest.skip('requires built patched QEMU')
    if not Path(qemu).is_file():
        pytest.skip('configured patched QEMU is missing')
    source = tmp_path / 'mock.c'
    library = tmp_path / 'mock.so'
    source.write_text(MOCK)
    subprocess.run(['cc', '-shared', '-fPIC', str(source), '-o', str(library)], check=True)
    model = ET.Element('fmiModelDescription', fmiVersion='3.0', instantiationToken='inav-test')
    ET.SubElement(model, 'CoSimulation', modelIdentifier='mock')
    variables = ET.SubElement(model, 'ModelVariables')
    for number, name in enumerate(REQUIRED_VALUE_REFERENCES, 101):
        ET.SubElement(variables, 'Float64', name=name, valueReference=str(number))
    architecture = platform.machine()
    if architecture not in ('x86_64', 'aarch64'):
        pytest.skip(f'FMI runtime does not support {architecture}')
    with zipfile.ZipFile(tmp_path / 'Mock.fmu', 'w') as z:
        z.writestr('modelDescription.xml', ET.tostring(model))
        z.write(library, f'binaries/{architecture}-linux/mock.so')
    qmp = tmp_path / 'qmp.sock'
    config = (ROOT / 'configs/inav_matekf405.toml').read_text().split('[FMU]')[0]
    config = config.replace('/tmp/fastdyn-inav.qmp', str(qmp))
    config += f'''\n[FMU]
active = "mock"
auto_build = false
[FMU.models.mock]
model = "Mock"
model_file = "{ROOT}/modelica/FastDyn/Copter.mo"
source_root = "{ROOT}"
output = "{tmp_path}"
build = true
'''
    path = tmp_path / 'inav.toml'
    path.write_text(config)
    log = tmp_path / 'run.log'
    with log.open('w') as output:
        proc = subprocess.Popen([str(binary), '-q', 'run', '-c', str(path),
                                 '-o', str(tmp_path / 'work')], cwd=ROOT,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                env={**os.environ, "FASTDYN_INAV_TEST_MARKER": str(tmp_path / "pwm.txt")})
        try:
            deadline = time.monotonic() + 45
            messages = ('MPU6500 sensor connected', 'motor outputs connected',
                        'magnetometer connected', 'barometer connected', 'GPS connected')
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f'INAV exited early: {log.read_text()}')
                if qmp.exists() and all(message in log.read_text() for message in messages):
                    break
                time.sleep(.1)
            else:
                pytest.fail(f'INAV did not reach all bridge paths: {log.read_text()}')
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(qmp))
                stream = connection.makefile('rwb', buffering=0)
                json.loads(stream.readline())
                stream.write(b'{"execute":"qmp_capabilities"}\n')
                assert 'return' in json.loads(stream.readline())
                # Calibration and mixer are initialized by the actual guest code.
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    complete = _read_memory(stream, 0x10001290, 1)
                    motors = _read_memory(stream, 0x10004f70, 8)
                    fix = _read_memory(stream, 0x2000f014 + 9, 2)
                    if complete == b'\x01' and motors == b'\xe8\x03' * 4 and fix == b'\x02\x0c':
                        break
                    time.sleep(.1)
                else:
                    pytest.fail(f'guest did not calibrate or initialize: {complete=} {motors=} {fix=} {log.read_text()}')
                # Verify values after INAV's real sensor alignment and filtering.
                accel = struct.unpack('<3f', _read_memory(stream, 0x10001cb4 + 36, 12))
                assert accel[0] == pytest.approx(0, abs=.03)
                assert accel[1] == pytest.approx(0, abs=.03)
                assert accel[2] == pytest.approx(1, abs=.03)
                pressure = int.from_bytes(_read_memory(stream, 0x20013aa8 + 36, 4), 'little', signed=True)
                assert pressure == 99500
                temperature = int.from_bytes(_read_memory(stream, 0x20013aa8 + 32, 4), 'little', signed=True)
                assert temperature == 2100
                magnetic = struct.unpack('<3h', _read_memory(stream, 0x20013e58 + 54, 6))
                assert magnetic == (215, 0, 461)
                latitude, longitude, altitude = struct.unpack('<3i', _read_memory(stream, 0x2000f014 + 12, 12))
                assert (latitude, longitude, altitude) == (404149290, -869323870, 14900)
                # Send a continuous receiver signal: establish a low arm switch,
                # then raise AUX1, then raise throttle without interrupting frames.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                    # Oversized frames must not activate the receiver or arm.
                    bad = b'FDRC' + struct.pack('<8H', 1500, 1500, 1000, 1500,
                                                 1900, 1000, 1000, 1000) + b'x'
                    end = time.monotonic() + .5
                    while time.monotonic() < end:
                        receiver.sendto(bad, ('127.0.0.1', 14590))
                        time.sleep(.01)
                    assert not (int.from_bytes(_read_memory(stream, 0x100010b4, 4), 'little') & 4)
                    def send(arm, throttle):
                        packet = b'FDRC' + struct.pack('<8H', 1500, 1500, throttle, 1500,
                                                       arm, 1000, 1000, 1000)
                        receiver.sendto(packet, ('127.0.0.1', 14590))
                    def hold(arm, throttle, seconds):
                        end = time.monotonic() + seconds
                        while time.monotonic() < end:
                            send(arm, throttle)
                            time.sleep(.01)
                    hold(1000, 1000, 2)
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        hold(1900, 1000, .1)
                        if int.from_bytes(_read_memory(stream, 0x100010b4, 4), 'little') & 4:
                            break
                    else:
                        pytest.fail(f'INAV did not arm through its receiver: {log.read_text()}')
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        hold(1900, 1500, .1)
                        motors = struct.unpack('<4h', _read_memory(stream, 0x10004f70, 8))
                        if all(value > 1100 for value in motors):
                            break
                    else:
                        pytest.fail(f'INAV did not respond to throttle: {motors=} {log.read_text()}')
                    deadline = time.monotonic() + 3
                    value = ''
                    while time.monotonic() < deadline:
                        if (tmp_path / 'pwm.txt').exists():
                            value = (tmp_path / 'pwm.txt').read_text()
                            if value:
                                break
                        hold(1900, 1500, .1)
                    assert value and float(value) > 1100
                    # Stop sending and verify the firmware observes receiver loss.
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        flags = int.from_bytes(_read_memory(stream, 0x100010b4, 4), 'little')
                        if flags & (1 << 7):  # ARMING_DISABLED_FAILSAFE_SYSTEM
                            break
                        time.sleep(.05)
                    else:
                        pytest.fail(f'INAV did not enter receiver failsafe: {flags:#x}')
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
