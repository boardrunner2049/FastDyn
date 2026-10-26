#!/usr/bin/env python3
"""Run the real PX4 ELF through UART, MAVProxy, automatic mission and MAVCesium."""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from tornado.httpclient import AsyncHTTPClient
from tornado.websocket import websocket_connect

ROOT = Path(__file__).resolve().parents[5]


async def run(args):
    (ROOT / 'out').mkdir(exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix='px4-mission-', dir=ROOT / 'out'))
    logfile = artifacts / 'console.log'
    # Exercise real PARAM_REQUEST_READ/PARAM_SET/PARAM_VALUE for both wire types.
    params = artifacts / 'roundtrip.param'
    params.write_text('COM_RC_IN_MODE 4\nMPC_XY_CRUISE 5.0\n')
    env = dict(os.environ, FASTDYN_MAVLINK_FIRMWARE_PORT=str(args.base_port),
               FASTDYN_MAVLINK_GCS_PORT=str(args.base_port + 1),
               FASTDYN_MAVCESIUM_PORT=str(args.base_port + 2),
               FASTDYN_PARAM_FILE=str(params))
    env.pop('FASTDYN_BRIDGE_BOOT', None)
    env.pop('FASTDYN_MISSION_FILE', None)
    print(f'Artifacts: {artifacts}', flush=True)
    viewer = f'http://127.0.0.1:{args.base_port + 2}/mavcesium/'
    connection = pending = None
    positions = []
    attitudes = 0
    http_ok = False
    client = AsyncHTTPClient()
    with logfile.open('w') as log, (artifacts / 'viewer.jsonl').open('w') as messages:
        proc = subprocess.Popen([str(ROOT / 'fastdyn-env/bin/fastdyn'), 'run', '-c',
            'configs/px4_fmu_v6c_fire_p2.toml', '--no-build-fmu', '-o', str(artifacts / 'run')],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + args.timeout
            while proc.poll() is None and time.monotonic() < deadline:
                if connection is None:
                    try:
                        response = await client.fetch(viewer, request_timeout=1)
                        http_ok = response.code == 200
                        connection = await websocket_connect(viewer.replace('http:', 'ws:') + 'websocket/',
                                                             connect_timeout=1)
                    except (OSError, TimeoutError):
                        await asyncio.sleep(0.2)
                        continue
                if pending is None:
                    pending = connection.read_message()
                done, _ = await asyncio.wait([pending], timeout=0.2)
                if not done:
                    continue
                payload = pending.result()
                pending = None
                if payload is None:
                    connection.close()
                    connection = None
                    continue
                data = json.loads(payload)
                messages.write(json.dumps(data) + '\n')
                mav = data.get('mav_data', {})
                if mav.get('mavpackettype') == 'GLOBAL_POSITION_INT':
                    positions.append(mav)
                elif mav.get('mavpackettype') == 'ATTITUDE':
                    attitudes += 1
        finally:
            if connection is not None:
                connection.close()
            if pending is not None:
                pending.cancel()
            client.close()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
    output = logfile.read_text(errors='replace')
    altitude = max((p['relative_alt'] / 1000 for p in positions), default=0)
    distance = 0.0
    if positions:
        first = positions[0]
        distance = max(math.hypot((p['lat'] - first['lat']) * 1e-7 * 111319.5,
            (p['lon'] - first['lon']) * 1e-7 * 111319.5 * math.cos(math.radians(first['lat'] * 1e-7)))
            for p in positions)
    required = ['FASTDYN_PX4_READY\n', 'PX4 UART bridge: GCS peer',
                'PX4 system=1 component=1', 'upload accepted: 5 items',
                'AUTO.MISSION confirmed', 'arming confirmed', 'mission start accepted',
                'mission completed, landed and disarmed']
    missing = [value for value in required if value not in output]
    summary = dict(returncode=proc.returncode, viewer_http_ok=http_ok,
                   position_samples=len(positions), attitude_samples=attitudes,
                   max_relative_alt_m=altitude, max_horizontal_distance_m=distance,
                   missing=missing)
    (artifacts / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    if proc.returncode != 0 or missing or not http_ok or altitude < 10 or distance < 5 or attitudes < 50:
        raise SystemExit(f'PX4 mission smoke FAILED: {logfile}')
    print('PASS: real PX4 UART/UDP, typed parameters, mission, normal arming, flight, landing, viewer HTTP/WebSocket')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--base-port', type=int, default=19551,
                        help='Three ports: firmware UDP, GCS UDP, viewer TCP')
    args = parser.parse_args()
    if not 1 <= args.base_port <= 65533 or args.timeout <= 0:
        parser.error('invalid port range or timeout')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
