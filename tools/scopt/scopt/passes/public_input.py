"""Input requirements for passes that edit only captured public records."""
from ..formats.layouts import SC_VERSION
from .base import Outcome, PassResult


def public_input_refusal(header):
    if header is None:
        return PassResult(Outcome.SKIPPED_UNKNOWN, "missing_cache_header")
    if header.version != SC_VERSION:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "unsupported_cache_version",
                          f"expected {SC_VERSION}, found {header.version}")
    if header.reserved:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "existing_cache_extensions_unsupported",
                          f"input has extension flags {header.reserved:#x}")
    return None


def page_layout_refusal(header, state):
    refusal = public_input_refusal(header)
    if refusal is not None or state is None:
        return refusal
    if state.firmware_regions:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "firmware_region_terminal_stage",
                          "placement cannot move an emitted firmware region")
    if state.exits:
        return PassResult(Outcome.SKIPPED_UNSUPPORTED, "pending_exit_growth_changes_layout",
                          "page scoring uses captured block sizes and transfer offsets")
    return None
