#!/usr/bin/env python3
"""Query the actual MPS2 CPU address space and read PSRAM bytes before guest boot."""
import argparse
from hashlib import file_digest
import json
from pathlib import Path
import re
import subprocess
import tempfile


def check(exe):
    with tempfile.TemporaryDirectory(prefix="pye-psram-") as temp:
        ram = Path(temp) / "psram"
        marker = b"PYEPROM1"
        with ram.open("wb") as f:
            f.write(marker)
            f.truncate(16 << 20)
        commands = [{"execute": "qmp_capabilities"}]
        commands.extend({"execute": "human-monitor-command", "arguments": {"command-line": c}}
                        for c in ("info mtree -f", "xp /8bx 0x21000000"))
        commands.append({"execute": "quit"})
        p = subprocess.run([str(exe), "-M", "mps2-an385,memory-backend=psram", "-object",
                            "memory-backend-file,id=psram,size=16M,share=off,mem-path=" + str(ram),
                            "-S", "-qmp", "stdio", "-serial", "none", "-display", "none"],
                           input="\n".join(json.dumps(x) for x in commands) + "\n", capture_output=True,
                           text=True, check=True, timeout=30)
    answers = [obj["return"] for line in p.stdout.splitlines() if "return" in (obj := json.loads(line))]
    tree = answers[1].split("Root memory region: armv7m-container", 1)[1].split("FlatView #", 1)[0]
    addresses = {"ssram": 0x20000000, "reserved_gap": 0x20800000, "psram": 0x21000000, "uart": 0x40004000}
    regions = {}
    for name, addr in addresses.items():
        for line in tree.splitlines():
            match = re.search(r"^\s*([0-9a-f]+)-([0-9a-f]+) \([^)]+\): (.+)$", line)
            if match and int(match[1], 16) <= addr <= int(match[2], 16):
                regions[name] = match[3]
    read = bytes.fromhex(" ".join(re.findall(r"0x([0-9a-f]{2})", answers[2])))
    with exe.open("rb") as f:
        digest = file_digest(f, "sha256").hexdigest()
    return {"qemu": str(exe), "qemu_sha256": digest, "regions": regions,
            "marker_hex": marker.hex(), "observed_hex": read.hex(), "psram_visible": read == marker}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("exe", type=Path)
    p.add_argument("--expect", choices=("stock", "original-fork", "fixed"), required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    doc = check(a.exe)
    r = doc["regions"]
    ok = (r["ssram"] == "mps.ssram23" and r["uart"] == "uart"
          and r["psram"] == ("fakeram" if a.expect == "original-fork" else "psram")
          and doc["psram_visible"] == (a.expect != "original-fork")
          and (a.expect == "stock" or r["reserved_gap"] == "fakeram"))
    doc["verdict"] = "PASS" if ok else "FAIL"
    a.out.write_text(json.dumps(doc, indent=2) + "\n")
    raise SystemExit(0 if ok else 1)
