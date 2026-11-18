"""WP7 fix to the WP2 decoder: rel8 fields of loop/jrcxz are extracted (fail closed)."""

from scopt.decode.x86 import RefKind, extract_refs, sweep


def test_loop_and_jrcxz_rel8_are_references():
    for raw in ("e2fe", "e3fe", "67e3fe", "e1fe", "e0fe"):  # loop, jrcxz, jecxz, loope, loopne
        ins = sweep(bytes.fromhex(raw), 0x1000)
        refs = extract_refs(ins, (0x1000, 0x100))
        assert len(refs) == 1 and refs[0].width == 1, raw
        assert refs[0].kind == RefKind.JCC_REL8 and refs[0].value == ins[0].end - 2, raw
