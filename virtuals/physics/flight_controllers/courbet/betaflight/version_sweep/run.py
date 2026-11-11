#!/usr/bin/env python3
"""Revalidate and run a pinned Betaflight sweep image without changing the default config."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
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


def check_hardware(log):
    markers = ['bf-hw: MPU6000 raw SPI samples active',
               'bf-hw: BMP280 raw I2C samples active',
               'hw-mpu-gyro-decode', 'hw-mpu-acc-decode', 'hw-bmp-compensate',
               'starting real accelerometer calibration']
    missing = [x for x in markers if x not in log]
    if missing or 'virtual gyro @' in log or 'hw-hardfault pc=' in log:
        raise ValueError(f'physical driver validation failed: {missing}')
    stats = re.search(r'bf-hw: summary MPU6000_SPI_reads=(\d+) BMP280_I2C_reads=(\d+) pressure_range=([\d.]+)\.\.([\d.]+)Pa max_gyro=([\d.]+)dps', log)
    if not stats or int(stats[1]) < 1000 or int(stats[2]) < 100 or float(stats[4])-float(stats[3]) < 100 or float(stats[5]) < 5:
        raise ValueError('missing changing sensor data and pressure response to altitude')
    return dict(mpu_reads=int(stats[1]), bmp_reads=int(stats[2]),
                min_pressure_pa=float(stats[3]), max_pressure_pa=float(stats[4]),
                peak_gyro_dps=float(stats[5]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    versions = [x['tag'] for x in json.loads((HERE/'plan.json').read_text())['versions']]
    parser.add_argument('version', choices=versions)
    parser.add_argument('--port', type=int, default=18500, help='HTTP port; reserves offsets +51,+52,+56,+60')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65475:
        parser.error('--port must be between 1024 and 65475')
    target = HERE/args.version
    record = json.loads((target/'abi.json').read_text())
    binary = next((target/'bin').glob('*.elf'))
    subprocess.run([sys.executable,str(HERE.parents[1]/'prepare_execution_configuration.py'),
                    '--profile',str(target/'profile.toml')],check=True,cwd=ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder = ROOT/'out/betaflight/version-sweep'/args.version/stamp
    folder.mkdir(parents=True)
    config = folder/'runtime.toml'
    config.write_text(runtime_text(args.version,args.port,target,binary))
    env = {'FASTDYN_BF_UART_IDS':','.join(str(record['constants'][name]) for name in
                                        ['SERIAL_PORT_USART1','SERIAL_PORT_USART2','SERIAL_PORT_USART3']),
           'FASTDYN_BF_GPS_LEGACY':'1' if args.version == '4.2.11' else '0',
           'FASTDYN_MAVLINK_FIRMWARE_PORT':str(args.port+51),
           'FASTDYN_MAVLINK_GCS_PORT':str(args.port+52),
           'FASTDYN_BF_MSP_PORT':str(args.port+60), 'FASTDYN_MAVCESIUM_PORT':str(args.port),
           'FASTDYN_BF_SMOKE_CONFIG':str(config), 'FASTDYN_BF_SMOKE_LOG':str(folder/'mission.log'),
           'FASTDYN_BF_SMOKE_WORK_DIR':str(folder/'work'), 'FASTDYN_BF_SMOKE_TIMEOUT_SEC':str(args.timeout)}
    print(f'Flight viewer: http://127.0.0.1:{args.port}/mavcesium/ (available during flight)', flush=True)
    print(f'Run artifacts: {folder}', flush=True)
    result = subprocess.run([sys.executable,str(HERE/'measure.py'),'--version',args.version,
                             '--phase','mission','--output',str(ROOT/'out/betaflight/version-sweep/evidence'),
                             '--','env',*(f'{key}={value}' for key,value in env.items()),
                             'tests/integration/betaflight_f405_smoke.sh'],cwd=ROOT)
    if result.returncode:
        print((folder/'mission.log').read_text()[-5000:], file=sys.stderr)
        return result.returncode
    stats = check_hardware((folder/'mission.log').read_text())
    (folder/'sensors.json').write_text(json.dumps(stats,indent=2)+'\n')
    print(f'PASS {args.version}: mission, physical drivers and changing sensor feedback')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
