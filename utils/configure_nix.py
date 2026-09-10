#!/usr/bin/env python3
"""Compatibility entry point; the portable generator is now fastdyn-config."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fastdyn.configure import main

if __name__ == '__main__':
    main()
