from fastdyn.binary.symmap import SymbolResolver
from fastdyn.binary.symmap.core import SymbolInfo
from fastdyn.binary.symmap.providers.dwarf import DwarfProvider
from fastdyn import fastdyn_log as fastdyn_log_conf
try:  # Direct imports in host-side tests do not go through plugin discovery.
    from _fastdyn_virtual_utils.rtos_models import RTOS_SIGNATURES, identify_rtos as _identify_rtos
except ModuleNotFoundError:
    from virtuals.utils.rtos_models import RTOS_SIGNATURES, identify_rtos as _identify_rtos
from .introspector_base import RTOSIntrospector
from fastdyn.machine import VirtualInstruction
from dataclasses import dataclass, field
from elftools.elf.elffile import ELFFile
import importlib
from pathlib import Path

fastdyn_log = fastdyn_log_conf.getFastdynLogger()


@dataclass(frozen=True)
class IntrospectionPlan:
    """The declarative artifacts generated for one firmware-wide RTOS pass."""

    schema: str
    virtuals: list[VirtualInstruction] = field(default_factory=list)

def identify_rtos(symbols):
    """
    Pass in a list or set of symbol names from your SymbolInfo dictionary.
    """
    rtos = _identify_rtos(symbols)
    fastdyn_log.info("Detected RTOS:" + rtos if rtos != "Unknown/Custom Baremetal"
                     else "Likely Unknown/Custom Baremetal")
    return rtos


def _add_elf_symbol_table_symbols(binary, symbols, analysis=None):
    """Fill DWARF gaps from the ELF symbol table without replacing DWARF data.

    Production RTOS builds frequently keep scheduler globals in a different
    compilation unit whose DWARF location uses a location list. The existing
    DWARF provider intentionally ignores those ambiguous expressions, while
    the linked ELF symbol table still has an unambiguous address.
    """
    if analysis is not None:
        entries = analysis.symbols.all()
        for symbol in entries:
            if symbol.name in symbols:
                continue
            symbols[symbol.name] = SymbolInfo(
                name=symbol.name, address=symbol.code_address, size=symbol.size,
                kind="function" if symbol.kind == "function" else "variable",
                provider=symbol.provider, confidence=0.9,
            )
        return symbols
    with open(binary, "rb") as elf_file:
        elf = ELFFile(elf_file)
        for section in elf.iter_sections():
            if section.header.sh_type not in {"SHT_SYMTAB", "SHT_DYNSYM"}:
                continue
            for symbol in section.iter_symbols():
                name = symbol.name
                address = int(symbol.entry.st_value)
                if not name or symbol.entry.st_shndx == "SHN_UNDEF" or name in symbols:
                    continue
                symbol_type = symbol.entry.st_info.type
                kind = "function" if symbol_type == "STT_FUNC" else "variable"
                symbols[name] = SymbolInfo(name, address, int(symbol.entry.st_size) or None,
                                           kind, "elf-symbol-table", 0.9)
    return symbols

def _load_local_introspectors():
    """Scans the current directory for anything ending in _introspector.py"""
    current_dir = Path(__file__).parent / "rtos"
    for source in current_dir.glob("*_introspector.py"):
        filename = source.name
        # Only load files that match our strict naming convention
        if filename.endswith('_introspector.py') and filename != 'base_introspector.py':
            # Strip the '.py' extension to get the module name
            module_name = filename[:-3]
            # Dynamically import it, triggering the auto-registration
            importlib.import_module(f"{__package__}.rtos.{module_name}")


def supported_rtos() -> frozenset[str]:
    """Return RTOS names with a loaded Python introspection implementation.

    Detection signatures are broader than the set of implementations. Keeping
    those concepts separate ensures that seeing a known RTOS never causes an
    unhandled factory failure.
    """
    _load_local_introspectors()
    return frozenset(RTOSIntrospector._registry)


def introspect_rtos(binary, *, architecture="", machine="", cpu="", analysis=None):
    """Inspect an ELF and return schema plus internal hook rules.

    This function deliberately does not mutate a FastDyn CPU object. The
    run-preprocessing layer owns integration of the returned plan.
    """
    # TODO: Add some sort of persistent storage for symbols between runs
    # Use a hash of the binary and the symbols to determine if we need to re-resolve the symbols
    resolver = SymbolResolver([DwarfProvider(session=analysis)])
    syms = _add_elf_symbol_table_symbols(binary, resolver.resolve(binary), analysis)
    for key, value in syms.items():
        if key in ["ch_system", "ch_debug"]:
            fastdyn_log.info(f"Found {key}: {value}")
    rtos_name = identify_rtos(syms)
    implemented = supported_rtos()

    if rtos_name == "Unknown/Custom Baremetal":
        fastdyn_log.info("Cannot introspect custom baremetal firmware.")
        return IntrospectionPlan(schema="")
    if rtos_name not in implemented:
        fastdyn_log.warning(
            "Detected %s, but this FastDyn build has no introspection "
            "implementation for it. Skipping RTOS introspection.",
            rtos_name,
        )
        return IntrospectionPlan(schema="")
    # Dynamically instantiate the correct introspector!
    introspector = RTOSIntrospector.create(
        rtos_name, None, syms, binary,
        architecture=architecture, machine=machine, cpu=cpu, analysis=analysis,
    )
    # Fire up the OS-specific hooks
    schema_contents = introspector.setup_hooks()

    return IntrospectionPlan(schema=schema_contents, virtuals=introspector.virtuals)
