#!/usr/bin/env python3
"""Boot the real PX4 ELF, read sensors, and exercise a simulated motor."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

COURBET = Path(__file__).resolve().parents[1]
PROFILE = COURBET / 'px4/fmu_v6c'
ROOT = COURBET.parents[3]
BRIDGE_TOPICS = ('sensor_accel', 'sensor_gyro', 'sensor_baro', 'sensor_mag',
                 'sensor_gps', 'actuator_outputs_sim')
COMMANDS = """sleep 3
listener sensor_accel -n 1
listener sensor_baro -n 1
listener sensor_mag -n 1
listener sensor_gps -n 1
actuator_test set -m 1 -v 0.1 -t 2
sleep 3
listener actuator_outputs_sim -n 1
ekf2 status
echo FASTDYN_PX4_SMOKE_OK
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--auto-boot', action='store_true',
                        help='Check direct fastdyn run with the TOML boot default and no environment override')
    args = parser.parse_args()
    (ROOT / 'out').mkdir(exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix='px4-smoke-', dir=ROOT / 'out'))
    logfile = artifacts / 'console.log'
    env = dict(os.environ, FASTDYN_DEBUG_PWM='1')
    if args.auto_boot:
        env.pop('FASTDYN_BRIDGE_BOOT', None)
        command = [str(ROOT / 'fastdyn-env/bin/fastdyn'), 'run', '-c',
                   'configs/px4_fmu_v6c_fire_p2.toml']
        marker = 'FASTDYN_PX4_READY'
    else:
        boot = artifacts / 'smoke.nsh'
        boot.write_text((PROFILE / 'boot.nsh').read_text() + COMMANDS)
        env['FASTDYN_BRIDGE_BOOT'] = str(boot)
        command = [str(PROFILE / 'run.sh')]
        marker = 'FASTDYN_PX4_SMOKE_OK'
    completed = False
    with logfile.open('w') as log:
        proc = subprocess.Popen(command + ['--no-build-fmu',
            '--no-run-processes', '-o', str(artifacts / 'run')],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline and proc.poll() is None:
                output = logfile.read_text(errors='replace')
                if (f'\n{marker}\n' in output and
                        all(f'PX4 FMU bridge: {topic} connected' in output for topic in BRIDGE_TOPICS)):
                    completed = True
                    break
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
    output = logfile.read_text(errors='replace')
    required = ['OS: NuttX', 'HW arch: PX4_FMU_V6C',
                'FMU timing: init_wall=' if args.auto_boot else 'FMU timing: sim=']
    if not args.auto_boot:
        required += ['FMU PWM input: channel=0 pwm=1180', 'latitude_deg: 40.414929',
                     'longitude_deg: -86.932387']
    required += ['PX4 FMU bridge: ' + topic + ' connected' for topic in BRIDGE_TOPICS]
    missing = [value for value in required if value not in output]
    active = output.find('FMU PWM input: channel=0 pwm=1180')
    stopped = output.find('FMU PWM input: channel=0 pwm=1100', active + 1)
    errors = [value for value in ('PANIC', 'Bad icount', 'STALE!', 'TIMEOUT!',
              'physics exchange failed', 'ERROR [') if value in output]
    print(f'Console log: {logfile}')
    if not completed or missing or errors or (not args.auto_boot and stopped < 0):
        raise SystemExit(f'PX4 smoke FAILED: completed={completed}, missing={missing}, '
                         f'errors={errors}, motor_stopped={stopped >= 0}')
    if args.auto_boot:
        print('PASS: direct fastdyn run, TOML auto-initialization, six FMU bridge topics')
    else:
        print('PASS: ARM/NuttX boot, six FMU bridge topics, motor 1100 -> 1180 -> 1100')


if __name__ == '__main__':
    main()
