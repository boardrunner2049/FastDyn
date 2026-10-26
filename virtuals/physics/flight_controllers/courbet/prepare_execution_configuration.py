#!/usr/bin/env python3
"""Validate an ARM firmware ELF and resolve a target's execution configuration."""
import argparse
from pathlib import Path
import subprocess

from elftools.common.exceptions import ELFError

from execution_configuration import prepare_execution_configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True,
                        help='target profile.toml, or the directory containing it')
    parser.add_argument('--elf', type=Path, help='override the ELF specified in profile.toml')
    parser.add_argument('--output-dir', type=Path,
                        help='override the output directory (relative to the current directory)')
    args = parser.parse_args()
    try:
        destination = prepare_execution_configuration(
            args.profile, elf_path=args.elf, output_dir=args.output_dir)
    except (ValueError, OSError, ELFError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Cannot prepare execution configuration: {error}\n')
    print(f'Prepared execution configuration: {destination}')
    print((destination / 'firmware.sha256').read_text().strip())


if __name__ == '__main__':
    main()
