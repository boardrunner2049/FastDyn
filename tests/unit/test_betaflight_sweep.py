"""Release transport differences and pinned hardware sweep artifacts."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import socket
import struct
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SWEEP = ROOT/'virtuals/physics/flight_controllers/courbet/betaflight/version_sweep'


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


pilot = module(ROOT/'virtuals/physics/flight_controllers/courbet/mavlink/mav_mission_betaflight.py')
runner = module(SWEEP/'run.py')


def test_msp_receiver_frame_reaches_udp_with_wire_checksum_and_channel_order():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as receiver, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
        receiver.bind(('127.0.0.1',0)); receiver.settimeout(1)
        sender.connect(receiver.getsockname())
        pilot.send_rc(SimpleNamespace(fastdyn_msp_receiver=sender),0,200,
                      roll=1540,pitch=1420,thr=1620,yaw=1500,aux1=1800,aux2=1800)
        packet = receiver.recv(128)
    assert packet[:5] == b'$M<\x10\xc8'
    assert struct.unpack('<8H',packet[5:-1]) == (1540,1420,1620,1500,1800,1800,1000,1000)
    checksum=0
    for byte in packet[3:]: checksum ^= byte
    assert checksum == 0


def test_legacy_gps_velocity_uses_guest_microseconds_and_handles_timestamp_wrap():
    previous=(0xffff0000,40.,-86.,150.)
    msg=SimpleNamespace(time_boot_ms=(previous[0]+500000)&0xffffffff,lat=400000100,lon=-859999900,alt=150500)
    position, current=pilot.legacy_position(msg,previous)
    assert position[3] == pytest.approx(2.22639,rel=1e-4)
    assert position[4] == pytest.approx(2.22639*math.cos(math.radians(40)),rel=1e-4)
    assert position[5] == -1
    assert current[0] == msg.time_boot_ms


def test_old_angle_stick_mapping_matches_55_degree_linear_firmware():
    args=SimpleNamespace(max_speed=3,max_tilt=10,position_gain=.3,velocity_gain=6,
                         angle_limit=55,roll_sign=1,pitch_sign=1,angle_response='linear')
    roll,pitch=pilot.horizontal_sticks(30,0,0,0,args)
    assert roll == 1500
    assert (pitch-1500)/500*55 == pytest.approx(10,abs=.06)


@pytest.mark.parametrize('version',[v['tag'] for v in json.loads((SWEEP/'plan.json').read_text())['versions']])
def test_sweep_artifact_and_saved_evidence_are_for_exact_physical_image(version):
    folder=SWEEP/version
    abi=json.loads((folder/'abi.json').read_text());result=json.loads((folder/'result.json').read_text())
    binary=next((folder/'bin').glob('*.elf'))
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == abi['elf_sha256'] == result['elf_sha256']
    assert hashlib.sha256((folder/'parameters.txt').read_bytes()).hexdigest() == abi['parameters_sha256']
    log=(folder/'evidence/mission.log').read_text()
    stats=runner.check_hardware(log)
    assert stats == result['sensor_stats']
    assert 'landed and disarmed: mission complete; waypoints=3/3' in log
    recording=json.loads((folder/'evidence/flight.json').read_text())
    assert recording['mission']['phase']=='COMPLETE'
    text=runner.runtime_text(version,18500,folder,binary)
    assert f'--rc-msp' in text if version.startswith('4.') else '--rc-msp' not in text
    assert (f'--angle-response' in text) == (version in ['4.4.3','4.3.2','4.2.11'])
    assert str(binary.relative_to(ROOT)) in text


def test_physical_validation_rejects_a_viewer_without_changing_hardware_samples():
    with pytest.raises(ValueError):
        runner.check_hardware('landed and disarmed: mission complete; waypoints=3/3')
