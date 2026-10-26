"""ArduPilot callback argument conventions, shared across vehicle targets."""
from execution_configuration.rules import resolve_operand


def validate(elf):
    # Existing ChibiOS hooks have no common DWARF layout contract yet.
    # Targets with absolute-address rules pin the ELF in profile.toml.
    pass


def resolve_argument(callback, argument, elf):
    if callback == 'gps_get_type_mavlink':
        # Legacy configurations carry a literal vehicle tag (e.g. copter).
        # It is opaque callback data, not an ELF symbol; the C hook currently ignores it.
        return argument
    return None if argument is None else resolve_operand(argument, elf)
