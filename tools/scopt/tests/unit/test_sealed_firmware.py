import pytest

from scopt.formats.sealed_firmware import parse_trace, range_argument


def test_parse_complete_sealed_trace() -> None:
    trace = "\n".join((
        "scopt-sealed-coverage-v1 110 800400 ff000000",
        "range 100 104",
        "tb 100 a00 ffffffffffffffff 110 800400 ff000000 2 1",
        "tb 102 a02 ffffffffffffffff 110 800400 ff000000 2 1",
        "end",
    ))
    parsed = parse_trace(trace)
    assert parsed["ranges"] == ((0x100, 0x104),)
    assert len(parsed["rows"]) == 2
    assert range_argument(parsed["ranges"]) == "100-104"


@pytest.mark.parametrize("mutation", ("missing", "duplicate", "context", "multi"))
def test_parse_rejects_incomplete_or_inconsistent_trace(mutation: str) -> None:
    rows = [
        "tb 100 a00 ffffffffffffffff 110 800400 ff000000 2 1",
        "tb 102 a02 ffffffffffffffff 110 800400 ff000000 2 1",
    ]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[1] = rows[0]
    elif mutation == "context":
        rows[1] = rows[1].replace("800400", "800401")
    else:
        rows[1] = rows[1][:-1] + "2"
    trace = "\n".join(("scopt-sealed-coverage-v1 110 800400 ff000000",
                        "range 100 104", *rows, "end"))
    with pytest.raises(ValueError):
        parse_trace(trace)
