#!/usr/bin/env python3
"""Fetch the portable MSL/FMI dependencies used by FIRE builds; no source edits."""
import argparse
from pathlib import Path
import subprocess
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / 'out/toolchains'
MSL_REV = '6626538a236a34a390655b55d5e8c906bfe31772'
FMI_REV = 'b8778deaf5b746ba4a2f6c155d53f8a33eebbd33'
RUMOCA_REV = 'f477d0b698954b5a70f86286aaae9a3570ef39d5'


def checkout(url, name, ref, revision):
    path = ROOT / name
    if not path.exists():
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', ref, url, str(path)], check=True)
    actual = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != revision:
        raise SystemExit(f'{path}: expected {revision}, got {actual}; existing checkout was left untouched')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rumoca', action='store_true', help='Also fetch the audited Rumoca 0.10.1 revision')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    checkout('https://github.com/modelica/ModelicaStandardLibrary.git', 'ModelicaStandardLibrary', 'v4.0.0', MSL_REV)
    headers = ROOT / 'fmi3/headers'
    headers.mkdir(parents=True, exist_ok=True)
    for name in ['fmi3Functions.h', 'fmi3FunctionTypes.h', 'fmi3PlatformTypes.h']:
        url = f'https://raw.githubusercontent.com/modelica/fmi-standard/{FMI_REV}/headers/{name}'
        with urlopen(url, timeout=60) as response:
            content = response.read()
        path = headers / name
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
    if args.rumoca:
        # Fetch the audited commit, not a moving main branch.
        path = ROOT / 'rumoca'
        if not path.exists():
            subprocess.run(['git', 'init', str(path)], check=True)
            subprocess.run(['git', '-C', str(path), 'fetch', '--depth', '1',
                            'https://github.com/mhcho1994/rumoca.git', RUMOCA_REV], check=True)
            subprocess.run(['git', '-C', str(path), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
        revision = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != RUMOCA_REV:
            raise SystemExit(f'Existing Rumoca checkout differs from {RUMOCA_REV}: {path}')
        print(f'export FASTDYN_RUMOCA_DIR={path}')
    print(f'FIRE build dependencies ready: {ROOT}')


if __name__ == '__main__':
    main()
