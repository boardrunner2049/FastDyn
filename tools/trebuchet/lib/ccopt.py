#!/usr/bin/env python3
"""Compiler wrapper: force one optimization level and add --emit-relocs when linking.

usage: ccopt.py LEVEL <gcc arguments>   (LEVEL without the dash, e.g. O0)
"""
import os, re, shutil, sys
gcc = shutil.which(os.environ.get("ARM_PREFIX", "arm-none-eabi-") + "gcc")
level = sys.argv[1]
args = [a for a in sys.argv[2:] if not re.fullmatch(r"-O(?:[0-3]|g|s|z|fast)", a)]
extra = [] if "-c" in args else ["-Wl,--emit-relocs"]
os.execv(gcc, [gcc, f"-{level}", *args, *extra])
