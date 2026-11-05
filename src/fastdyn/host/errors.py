"""Errors raised by FastDyn's host-side firmware analysis API."""


class HostAnalysisError(ValueError):
    """Base class for an invalid or unsupported host analysis request."""


class UnsupportedTargetError(HostAnalysisError):
    """The requested target or instruction mode is not supported."""


class ImageAddressError(HostAnalysisError):
    """An image read is invalid or touches unmapped memory."""


class SymbolNotFoundError(HostAnalysisError):
    """No defined symbol matches a lookup."""


class AmbiguousSymbolError(HostAnalysisError):
    """A single-symbol lookup matched more than one address."""


class DwarfQueryError(HostAnalysisError):
    """DWARF metadata cannot answer a requested query."""
