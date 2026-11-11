#!/usr/bin/env python3
"""Build a pinned hardware-target release in an isolated source worktree.

Requires the upstream compiler version. Outputs a new review artifact; it never
overwrites the validated ELF/profile. Use prepare.py explicitly to install a
reviewed rebuild, then rerun its mission before accepting the new hash.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[5]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    plan = json.loads((HERE/'plan.json').read_text())
    parser.add_argument('version', choices=[v['tag'] for v in plan['versions']])
    parser.add_argument('--source', type=Path, default=ROOT.parent/'betaflight')
    parser.add_argument('--toolchain', type=Path, required=True, help='compiler installation containing bin/arm-none-eabi-gcc')
    parser.add_argument('--jobs', type=int, default=16)
    args = parser.parse_args()
    version = next(v for v in plan['versions'] if v['tag'] == args.version)
    compiler = args.toolchain.resolve()/'bin/arm-none-eabi-gcc'
    actual = subprocess.check_output([str(compiler),'-dumpversion'],text=True).strip()
    if actual != version['gcc']:
        parser.error(f'upstream requires GCC {version["gcc"]}, found {actual}')
    source = args.source.resolve()
    subprocess.run(['git','-C',str(source),'cat-file','-e',version['commit']],check=True)
    checkout = ROOT/'out/betaflight/version-sweep/sources'/args.version
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = ROOT/'out/betaflight/version-sweep/builds'/args.version/stamp
    (output/'obj').mkdir(parents=True)
    (output/'bin').mkdir()
    def step(phase,command):
        subprocess.run([sys.executable,str(HERE/'measure.py'),'--version',args.version,
                        '--phase',phase,'--output',str(output/'evidence'),'--',*command],check=True,cwd=ROOT)
    if not checkout.exists():
        step('checkout',['git','-C',str(source),'worktree','add','--detach',str(checkout),version['commit']])
    head = subprocess.check_output(['git','-C',str(checkout),'rev-parse','HEAD'],text=True).strip()
    if head != version['commit']:
        parser.error(f'{checkout} is at {head}; refusing to change it')
    if 'config_commit' in version and not (checkout/'src/config/.git').exists():
        local = source/'src/config'
        if not (local/'.git').exists():
            parser.error('initialize the source checkout config submodule first: git submodule update --init src/config')
        step('config-checkout',['git','-C',str(local),'worktree','add','--detach',
                                str(checkout/'src/config'),version['config_commit']])
    if subprocess.check_output(['git','-C',str(checkout),'status','--porcelain','--untracked-files=no'],text=True).strip():
        parser.error(f'{checkout} has tracked changes; refusing to build it')
    command = ['make','-C',str(checkout),'DEBUG=GDB',f'ARM_SDK_DIR={args.toolchain.resolve()}',
               f'OBJECT_DIR={output}/obj',f'BIN_DIR={output}/bin','MAKE=/usr/bin/make',
               'PYTHON=/usr/bin/python3',f'-j{args.jobs}']
    if 'config_commit' in version:
        command += ['CONFIG=MATEKF405CTR',f'CONFIG_DIR={checkout}/src/config']
    else:
        command += ['TARGET=STM32F405' if args.version == '4.4.3' else 'TARGET=MATEKF405']
    if args.version == '4.4.3':
        command += ['DEBUG_FLAGS=-ggdb2 -gdwarf-5 -DDEBUG']
        step('flash-O0',command+['CC_DEBUG_OPTIMISATION=-O0',str(output/'obj/STM32F405/drivers/flash_m25p16.o')])
    goal = 'fwo' if args.version.startswith(('2025.','2026.')) else 'hex'
    step('clean-build',command+[goal])
    elf = next((output/'obj').glob('*.elf'))
    symbols = subprocess.check_output([str(args.toolchain.resolve()/'bin/arm-none-eabi-nm'),str(elf)],text=True)
    required = ['mpu6000SpiDetect','bmp280Calculate']
    assert all(name in symbols for name in required)
    assert all(name not in symbols for name in ['virtualGyroDetect','virtualAccDetect','virtualBarometerDetect'])
    banner = subprocess.check_output([str(compiler),'--version'],text=True).splitlines()[0]
    report = dict(version=version,compiler=banner,elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),
                  elf_bytes=elf.stat().st_size,source_changes=False,build_command=command+[goal])
    (output/'build.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'Built review artifact: {elf}\nSHA-256: {report["elf_sha256"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
