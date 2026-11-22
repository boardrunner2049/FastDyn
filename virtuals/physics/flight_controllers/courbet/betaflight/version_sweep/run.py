#!/usr/bin/env python3
"""Run a Betaflight release with the hardware-sensor configuration and flight viewer."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[5]


def runtime_text(version, port, target, binary):
    text = (ROOT/'configs/betaflight_f405.toml').read_text()
    text = re.sub(r'monitor_port = \d+', f'monitor_port = {port+56}', text)
    text = text.replace('/tmp/qmp_betaflight_matekf405ctr.sock', f'/tmp/qmp_bf_sweep_{version}_{port}.sock')
    base = 'virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr'
    replacement = str(target.relative_to(ROOT))
    text = text.replace(base+'/bin/betaflight_STM32F405_MATEKF405CTR.elf', replacement+'/bin/'+binary.name)
    text = text.replace(base+'/unlabeled_conf', replacement+'/unlabeled_conf')
    options = ['--heartbeat-autopilot', '0']
    if version != '2026.6.2':
        options += ['--angle-mode-id', '0']
    if version.startswith('4.'):
        options += ['--rc-msp', f'127.0.0.1:{port+60}', '--legacy-position-velocity']
    if version in ['4.4.3','4.3.2','4.2.11']:
        options += ['--angle-response', 'linear', '--angle-limit', '55']
    return text.replace('"--connect",', ', '.join(json.dumps(x) for x in options)+',\n    "--connect",')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    versions = sorted(path.parent.name for path in HERE.glob('*/profile.toml'))
    parser.add_argument('version', choices=versions)
    parser.add_argument('--port', type=int, default=18500, help='HTTP port; reserves offsets +51,+52,+56,+60')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65475:
        parser.error('--port must be between 1024 and 65475')
    candidates = [Path(sys.executable).with_name('fastdyn'), ROOT/'fastdyn-env/bin/fastdyn']
    installed = shutil.which('fastdyn')
    if installed:
        candidates.append(Path(installed))
    executable = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if executable is None:
        parser.error('FastDyn is not installed; run setup.sh first')
    target = HERE/args.version
    binary = next((target/'bin').glob('*.elf'))
    subprocess.run([sys.executable,str(HERE.parents[1]/'prepare_execution_configuration.py'),
                    '--profile',str(target/'profile.toml')],check=True,cwd=ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder = ROOT/'out/betaflight/version-sweep'/args.version/stamp
    folder.mkdir(parents=True)
    config = folder/'runtime.toml'
    config.write_text(runtime_text(args.version,args.port,target,binary))
    env = {**os.environ,
           'PATH':str(executable.parent)+os.pathsep+os.environ.get('PATH', ''),
           'FASTDYN_BF_UART_IDS':'0,1,2' if args.version.startswith('4.') else '51,52,53',
           'FASTDYN_BF_GPS_LEGACY':'1' if args.version == '4.2.11' else '0',
           'FASTDYN_MAVLINK_FIRMWARE_PORT':str(args.port+51),
           'FASTDYN_MAVLINK_GCS_PORT':str(args.port+52),
           'FASTDYN_BF_MSP_PORT':str(args.port+60), 'FASTDYN_MAVCESIUM_PORT':str(args.port)}
    print(f'Flight viewer: http://127.0.0.1:{args.port}/mavcesium/', flush=True)
    os.chdir(ROOT)
    os.execve(executable, [str(executable), 'run', '-c', str(config), '-o', str(folder/'work')], env)


if __name__ == '__main__':
    raise SystemExit(main())
