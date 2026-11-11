"""Small public API for reusable host-side firmware analysis."""
from .constants import (ConstantPropagation, KnownMemoryAccess,
                        find_known_memory_accesses)
from .decode import DecodedInstruction, Decoder
from .dwarf import (DwarfQueries, GlobalResult, MemberResult, SourceLocation, TypeResult,
                    constant_member_offset)
from .errors import (AmbiguousSymbolError, DwarfQueryError, HostAnalysisError,
                     ImageAddressError, SymbolNotFoundError, UnsupportedTargetError)
from .image import ImageAccess, ImageChunk, ImageRead
from .resolve import resolve_address
from .rtos import RTOS_HINT_MARKERS, RTOS_SIGNATURES, identify_rtos
from .session import AnalysisSession, AnalysisSessionPool
from .symbols import FunctionRange, Symbol, SymbolIndex

__all__ = [
    "AnalysisSession", "AnalysisSessionPool", "ImageAccess", "ImageChunk", "ImageRead",
    "Symbol", "SymbolIndex", "FunctionRange", "DwarfQueries", "TypeResult",
    "MemberResult", "GlobalResult", "SourceLocation", "constant_member_offset",
    "Decoder", "DecodedInstruction", "ConstantPropagation", "KnownMemoryAccess",
    "find_known_memory_accesses",
    "resolve_address", "RTOS_SIGNATURES", "RTOS_HINT_MARKERS", "identify_rtos",
    "HostAnalysisError", "UnsupportedTargetError", "ImageAddressError",
    "SymbolNotFoundError", "AmbiguousSymbolError", "DwarfQueryError",
]
