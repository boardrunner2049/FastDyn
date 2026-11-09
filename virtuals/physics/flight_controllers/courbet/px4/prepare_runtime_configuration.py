#!/usr/bin/env python3
"""Resolve one PX4 version into the maintained FMUv6C machine/plant TOML."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULTS = {
    'binary': 'virtuals/physics/flight_controllers/courbet/bin/px4_fmu-v6c_default.elf',
    'existing_config_path':
        'virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/unlabeled_conf',
    'bridge_boot': 'virtuals/physics/flight_controllers/courbet/px4/fmu_v6c/boot.nsh',
}


def _replace_setting(text: str, key: str, old_value: str, new_value: Path) -> str:
    old = f'{key} = {json.dumps(old_value)}'
    new = f'{key} = {json.dumps(str(Path(new_value).resolve()))}'
    if text.count(old) != 1:
        raise ValueError(f'expected exactly one maintained {key} setting in the base config')
    return text.replace(old, new)


def main() -> None:
    """Write a runtime TOML with only the selected PX4 profile paths replaced."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--execution-config', type=Path, required=True)
    parser.add_argument('--boot', type=Path, required=True)
    args = parser.parse_args()
    text = args.base.read_text(encoding='utf-8')
    text = _replace_setting(text, 'binary', DEFAULTS['binary'], args.elf)
    text = _replace_setting(text, 'existing_config_path',
                            DEFAULTS['existing_config_path'], args.execution_config)
    text = _replace_setting(text, 'bridge_boot', DEFAULTS['bridge_boot'], args.boot)
    args.output.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
