"""Strict ARM ELF symbol resolution; only function symbols lose the Thumb bit."""
import hashlib
from pathlib import Path
import re

from fastdyn.host import AnalysisSession, ImageAddressError, resolve_address


def is_number(token):
    return re.fullmatch(r'[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)', token) is not None


class FirmwareELF:
    def __init__(self, path):
        self.path = Path(path).resolve()
        data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.analysis = AnalysisSession.open(
            self.path, architecture='arm', instruction_mode='thumb-mclass'
        )
        self.elf = self.analysis.elf
        if (self.elf['e_machine'] != 'EM_ARM' or self.elf.elfclass != 32
                or not self.elf.little_endian):
            raise ValueError('expected a little-endian ARM32 firmware ELF')
        if self.elf.get_section_by_name('.symtab') is None:
            raise ValueError('ELF has no symbol table; use the unstripped build output')
        self.symbols = {}
        for symbol in self.analysis.symbols.all():
            self.symbols.setdefault(symbol.name, set()).add(symbol.code_address)

    def resolve(self, token):
        if is_number(token):
            return resolve_address(token, {})
        match = re.fullmatch(r'(.+?)(?:\+(0[xX][0-9a-fA-F]+|[0-9]+))?', token)
        if match is None:
            raise ValueError(f'invalid symbol expression: {token!r}')
        name, offset = match.groups()
        choices = self.symbols.get(name, set())
        if len(choices) != 1:
            raise ValueError(f'{name}: expected one address, found {sorted(choices)}')
        expression = name + (f'+{offset}' if offset else '')
        return resolve_address(expression, {name: next(iter(choices))})

    def read_at(self, address, size):
        try:
            result = self.analysis.image.read(address, size)
        except ImageAddressError as error:
            raise ValueError(str(error)) from error
        if result.source != 'file':
            raise ValueError(f'no file-backed ELF data at {address:#x} for {size} bytes')
        return result.data

    def symbol_map(self):
        return ''.join(f'{name}:0x{next(iter(values)):08x}\n'
                       for name, values in sorted(self.symbols.items()) if len(values) == 1)
