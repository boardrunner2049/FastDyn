#!/usr/bin/env python3
"""Compare two FastDyn observation traces under contract v1."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastdyn.preflight.validation import compare_observation_traces


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    comparison = compare_observation_traces(args.reference, args.candidate)
    print(json.dumps(comparison.to_dict(), sort_keys=True))
    return 0 if comparison.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
