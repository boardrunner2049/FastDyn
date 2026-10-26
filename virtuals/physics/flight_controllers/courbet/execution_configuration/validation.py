"""Reusable ELF/DWARF checks; expectations belong to firmware or target profiles."""
import shutil
import subprocess
import tempfile


def validate_target(elf, checks):
    unknown = checks.keys() - {'elf_sha256', 'symbol_addresses', 'instructions'}
    if unknown:
        raise ValueError(f'unknown validation settings: {sorted(unknown)}')
    digest = checks.get('elf_sha256')
    if digest is not None and digest != elf.sha256:
        raise ValueError('ELF SHA-256 mismatch; review absolute-address hooks before changing the profile')
    addresses = checks.get('symbol_addresses', {})
    if not isinstance(addresses, dict):
        raise ValueError('validation.symbol_addresses must be a table')
    for symbol, expected in addresses.items():
        if not isinstance(expected, int) or elf.resolve(symbol) != expected:
            raise ValueError(f'{symbol}: unexpected target address (expected {expected!r})')
    instructions = checks.get('instructions', [])
    if not isinstance(instructions, list):
        raise ValueError('validation.instructions must be an array of tables')
    for check in instructions:
        if (not isinstance(check, dict) or set(check) != {'at', 'bytes'}
                or not isinstance(check['at'], str) or not isinstance(check['bytes'], str)):
            raise ValueError('each instruction check must contain at and bytes strings')
        expected = bytes.fromhex(check['bytes'])
        if not expected:
            raise ValueError('instruction check must contain nonempty bytes')
        if elf.read_at(elf.resolve(check['at']), len(expected)) != expected:
            raise ValueError(f'{check["at"]} changed: review the instruction hook before regenerating')


def validate_abi(binary, expected):
    debugger = shutil.which('gdb-multiarch') or shutil.which('arm-none-eabi-gdb')
    if not debugger:
        raise ValueError('gdb-multiarch or arm-none-eabi-gdb with Python is required to check ELF DWARF')
    script = 'python\nimport gdb\n'
    script += f'expected = {expected!r}\n'
    script += """for name, (size, offsets) in expected.items():
    typ = gdb.lookup_type('struct ' + name)
    if size is not None:
        assert typ.sizeof == size, (name, 'size', typ.sizeof, size)
    fields = {f.name: f.bitpos // 8 for f in typ.fields() if hasattr(f, 'bitpos')}
    for field, offset in offsets.items():
        assert fields[field] == offset, (name, field, fields[field], offset)
print('FASTDYN_EXECUTION_ABI_OK')
end
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.gdb') as commands:
        commands.write(script)
        commands.flush()
        result = subprocess.run([debugger, '--nx', '--batch', '-iex', 'set auto-load off',
                                 str(binary), '-x', commands.name],
                                capture_output=True, text=True, timeout=60)
    if result.returncode or 'FASTDYN_EXECUTION_ABI_OK' not in result.stdout:
        raise ValueError('firmware ABI mismatch; review the bridge offsets:\n' + result.stdout + result.stderr)
