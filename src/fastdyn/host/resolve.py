"""Common numeric and symbol-offset resolution."""
from __future__ import annotations

import re
from collections.abc import Mapping

from .errors import SymbolNotFoundError

_NUMBER = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)")
_SYMBOL_OFFSET = re.compile(r"(.+?)([+-])(0[xX][0-9a-fA-F]+|[0-9]+)")


def resolve_address(token: int | str, symbols: Mapping[str, int] | object) -> int:
    """Resolve an integer, decimal/hex literal, or ``symbol+/-offset``.

    Syntax-specific callers remain responsible for bare-hex, dereference, and
    register conventions.
    """
    if isinstance(token, int) and not isinstance(token, bool):
        return token
    if not isinstance(token, str) or not token.strip():
        raise ValueError(f"invalid address expression: {token!r}")
    text = token.strip()
    if _NUMBER.fullmatch(text):
        return int(text, 0 if "x" in text.lower() else 10)
    match = _SYMBOL_OFFSET.fullmatch(text)
    name = match.group(1).strip() if match else text
    offset = 0
    if match:
        offset = int(match.group(3), 0)
        if match.group(2) == "-":
            offset = -offset
    try:
        if hasattr(symbols, "lookup"):
            value = symbols.lookup(name).code_address
        else:
            value = int(symbols[name])
    except (KeyError, SymbolNotFoundError) as exc:
        raise SymbolNotFoundError(f"undefined or unknown firmware symbol: {name!r}") from exc
    return value + offset
