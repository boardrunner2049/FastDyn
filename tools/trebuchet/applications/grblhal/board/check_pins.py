"""Negative controls consume the measured GPIO trace from a completed run."""
import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_board import pins


def verify_measured(run):
    observed = (run / "pins.txt").read_text().splitlines()
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        path = tmp / "pins.txt"
        path.write_text("\n".join(observed) + "\n")
        valid = pins(tmp)
        assert valid["axis_steps"] == [72414, 55226, 3359]
        active = next(index for index, line in enumerate(observed) if int(line.split()[1], 16) & 1)
        for label in ("missing_edge", "wrong_direction", "disabled_motor", "zero_width"):
            changed = observed.copy()
            stamp, portb, portc = changed[active].split()
            if label == "missing_edge":
                del changed[active]
            elif label == "wrong_direction":
                changed[active] = f"{stamp} {portb} {int(portc, 16) ^ 32:04x}"
            elif label == "disabled_motor":
                changed[active] = f"{stamp} {int(portb, 16) | 2:04x} {portc}"
            else:
                _, portb, portc = changed[active + 1].split()
                changed[active + 1] = f"{stamp} {portb} {portc}"
            path.write_text("\n".join(changed) + "\n")
            try:
                pins(tmp)
            except ValueError as exc:
                print(f"{label} rejected: {exc}")
            else:
                raise AssertionError(f"admitted {label}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("observed_run", type=Path)
    verify_measured(p.parse_args().observed_run)
