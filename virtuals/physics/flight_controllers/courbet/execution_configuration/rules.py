"""Compile the legacy virtuals.txt / modifiers.txt syntax used by the plugin."""
import re

from .elf import is_number


def resolve_operand(operand, elf):
    """Keep register/memory expressions and literals; resolve symbolic operands."""
    register = re.fullmatch(r'r(?:[0-9]|1[0-5])|s(?:[0-9]|[12][0-9]|3[01])|d(?:[0-9]|1[0-5])', operand)
    if is_number(operand) or register or any(char in operand for char in '*[]'):
        return operand
    return hex(elf.resolve(operand))


def compile_rules(source, kind, elf, firmware):
    lines = []
    for number, raw in enumerate(source.read_text().splitlines(), 1):
        raw = raw.split('#', 1)[0].strip()
        if not raw:
            continue
        parts = raw.split()
        try:
            valid_lengths = (2, 3) if kind == 'virtuals' else (3,)
            if len(parts) not in valid_lengths:
                raise ValueError('expected location, action, and ' +
                                 ('optional argument' if kind == 'virtuals' else 'value'))
            parts[0] = hex(elf.resolve(parts[0]))
            if kind == 'modifiers':
                parts[2] = resolve_operand(parts[2], elf)
            else:
                argument = parts[2] if len(parts) == 3 else None
                argument = firmware.resolve_argument(parts[1], argument, elf)
                if argument is not None:
                    parts = parts[:2] + [argument]
            lines.append(' '.join(parts))
        except ValueError as error:
            raise ValueError(f'{source}:{number}: {error}') from error
    return '\n'.join(lines) + '\n'
