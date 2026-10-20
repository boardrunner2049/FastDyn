#!/usr/bin/env python3
"""Serve a FastDyn FMI 3 plant to PX4 SITL over the Simulator MAVLink API."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastdyn.px4_sitl import MotorMapping, serve_tcp  # noqa: E402


def channels(value: str) -> tuple[int, int, int, int]:
    try:
        parsed = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use four comma-separated channel indexes") from exc
    if len(parsed) != 4:
        raise argparse.ArgumentTypeError("exactly four motor channels are required")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--fmu", required=True, type=Path, help="FastDyn FMI 3 Co-Simulation FMU")
    result.add_argument(
        "--host", default="0.0.0.0", help="TCP bind address (default: all interfaces)"
    )
    result.add_argument("--port", type=int, default=4560, help="PX4 simulator TCP port")
    result.add_argument("--step-us", type=int, default=2000, help="FMI step, 1..2000 microseconds")
    result.add_argument("--geoid-separation-m", required=True, type=float,
                        help="WGS84 minus MSL separation for the selected simulation origin")
    result.add_argument("--motor-channels", type=channels, default=(0, 1, 2, 3),
                        help="PX4 output channel for each FastDyn motor (default: 0,1,2,3)")
    result.add_argument("--pwm-min", type=float, default=1100.0)
    result.add_argument("--pwm-max", type=float, default=1900.0)
    result.add_argument("--disarmed-pwm", type=float, default=1000.0)
    result.add_argument(
        "--control-min", type=float, default=0.0,
        help="lower normalized motor endpoint; default assumes unidirectional [0,1]",
    )
    result.add_argument(
        "--control-max", type=float, default=1.0,
        help="upper normalized motor endpoint; default assumes unidirectional [0,1]",
    )
    result.add_argument("--actuator-timeout-s", type=float, default=0.1,
                        help="return plant motors to disarmed PWM after stale output")
    return result


def main() -> int:
    args = parser().parse_args()
    if not args.fmu.is_file():
        raise SystemExit(f"FMU not found: {args.fmu}")
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be in 1..65535")
    mapping = MotorMapping(
        args.motor_channels,
        pwm_min=args.pwm_min,
        pwm_max=args.pwm_max,
        disarmed_pwm=args.disarmed_pwm,
        control_min=args.control_min,
        control_max=args.control_max,
    )
    try:
        serve_tcp(
            args.fmu,
            host=args.host,
            port=args.port,
            mapping=mapping,
            geoid_separation_m=args.geoid_separation_m,
            step_us=args.step_us,
            actuator_timeout_s=args.actuator_timeout_s,
        )
    except KeyboardInterrupt:
        print("Stopping FastDyn BIL server")
    except ModuleNotFoundError as exc:
        if exc.name in {"fmpy", "pymavlink"}:
            raise SystemExit(
                f"Missing {exc.name}; install the BIL runtime with "
                "`python -m pip install -r requirements-px4-sitl.txt`"
            ) from exc
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
