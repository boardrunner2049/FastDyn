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


def public_layout_geometry(units, ctx):
    """Public extents/starts before a layout pass and ambiguous profile endpoints.

    Captured counts do not distinguish a public TB from a private copy.
    A grown exit can also transfer through a new guard at a different offset.
    Neither can supply exact public transfer-site samples to the page model.
    """
    state = ctx.b3_state
    effective = {u.uid: u for u in units}
    positions = {u.uid: u.tb_offset for u in units}
    private = set()
    grown = {}
    if state is not None:
        private.update(state.variant_sources())
        private.update(state.firmware_member_sources())
        if state.exits:
            from ..link import NativeLinkerV2, unplaced_public_offsets
            from ..manager_b3 import materialize

            plan = materialize(state, ctx.b3).plan
            linker = NativeLinkerV2(ctx.b3.snapshot, plan, offsets=ctx.b3.offsets,
                                    derive_captures=False)
            effective, grown = linker.effective_units(units)
            positions, _ = unplaced_public_offsets(ctx.b3.snapshot.units(), effective, grown)
    return effective, positions, private, set(grown)
