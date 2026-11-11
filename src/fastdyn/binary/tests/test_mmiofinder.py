from pathlib import Path

import pytest

from fastdyn.binary.mmiofinder import find_mmio_accesses
from fastdyn.binary.nextpc import next_pcs


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

FIRMWARE = Path(
    "./tests/RTOSDemo.axf"
)


def test_thumb_constants_literal_pool_cfg_and_transfer_widths():
    # Vector table, then Thumb code assembled from:
    #   ldr r4, [pc, #12] ; ldr/ldrb/ldrh [r4]; movw/movt r5; ldr [r5,#4]
    # The literal pool contains 0x40010000. Instruction sizes intentionally
    # differ from byte/halfword transfer widths.
    blob = bytes.fromhex(
        "00100020" "09000000"
        "044c" "2568" "2178" "2188"
        "40f20005" "c4f20105" "6868" "fee7"
        "00000140"
    )
    hits = find_mmio_accesses(
        blob=blob,
        base_va=0,
        arch="arm32",
        mode="thumb",
        mmio_regions=[(0x40010000, 0x400100ff)],
    )

    assert [(hit.address, hit.transfer_size) for hit in hits] == [
        (0x40010000, 4),
        (0x40010000, 1),
        (0x40010000, 2),
        (0x40010004, 4),
    ]
    assert [hit.instruction_size for hit in hits] == [2, 2, 2, 2]


@pytest.mark.skipif(
    not FIRMWARE.exists(),
    reason="STM32 firmware image not available on this system",
)
def test_find_mmio_accesses_and_nextpc():
    blob = FIRMWARE.read_bytes()

    hits = find_mmio_accesses(
        blob=blob,
        base_va=0x08000000,
        arch="arm32",
        mode="thumb",
        mmio_regions=[(0x40000000, 0x5FFFFFFF)],
    )

    assert hits, "Expected at least one MMIO access"

    for h in hits:
        assert h.pc is not None
        assert h.address is not None

        pcs = next_pcs(
            blob=blob,
            pc=h.pc,
            base_va=0x08000000,
            arch="arm32",
            mode="thumb",
        )

        # next_pcs returns a set
        assert isinstance(pcs, set)
        assert len(pcs) >= 1

