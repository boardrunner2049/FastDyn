"""Strict ARM ELF symbol resolution; only function symbols lose the Thumb bit."""
import hashlib
from io import BytesIO
from pathlib import Path
import re

from elftools.elf.elffile import ELFFile


def is_number(token):
    return re.fullmatch(r'[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)', token) is not None


def integer(token):
    return int(token, 16 if 'x' in token.lower() else 10)


class FirmwareELF:
    def __init__(self, path):
        self.path = Path(path).resolve()
        data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.elf = ELFFile(BytesIO(data))
        if (self.elf['e_machine'] != 'EM_ARM' or self.elf.elfclass != 32
                or not self.elf.little_endian):
            raise ValueError('expected a little-endian ARM32 firmware ELF')
        table = self.elf.get_section_by_name('.symtab')
        if table is None:
            raise ValueError('ELF has no symbol table; use the unstripped build output')
        self.symbols = {}
        for symbol in table.iter_symbols():
            if not symbol.name or symbol['st_shndx'] == 'SHN_UNDEF':
                continue
            address = symbol['st_value']
            if symbol['st_info']['type'] == 'STT_FUNC':
                address &= ~1
            self.symbols.setdefault(symbol.name, set()).add(address)

    def resolve(self, token):
        if is_number(token):
            return integer(token)
        match = re.fullmatch(r'(.+?)(?:\+(0[xX][0-9a-fA-F]+|[0-9]+))?', token)
        if match is None:
            raise ValueError(f'invalid symbol expression: {token!r}')
        name, offset = match.groups()
        choices = self.symbols.get(name, set())
        if len(choices) != 1:
            raise ValueError(f'{name}: expected one address, found {sorted(choices)}')
        return next(iter(choices)) + (integer(offset) if offset else 0)

    def read_at(self, address, size):
        for section in self.elf.iter_sections():
            if not section['sh_flags'] & 2 or section['sh_type'] == 'SHT_NOBITS':
                continue
            offset = address - section['sh_addr']
            if 0 <= offset and offset + size <= section['sh_size']:
                return section.data()[offset:offset + size]
        raise ValueError(f'no ELF data at {address:#x} for {size} bytes')

    def symbol_map(self):
        return ''.join(f'{name}:0x{next(iter(values)):08x}\n'
                       for name, values in sorted(self.symbols.items()) if len(values) == 1)
