"""Read a portable image identity exported by the original capture environment."""
import json
from functools import lru_cache
from pathlib import Path

from ..canon import sha256_id
from ..formats.scm_v1 import parse_scm
from .classify import HostContext


@lru_cache(maxsize=4)
def _evidence(directory: str, expected: str):
    root = Path(directory)
    raw = (root / "attested.json").read_bytes()
    if sha256_id(raw) != expected:
        raise ValueError("native-image export digest differs")
    report = json.loads(raw)
    scm = (root / "reference.scm").read_bytes()
    if report["schema"] != 1 or sha256_id(scm) != report["scm_sha256"]:
        raise ValueError("attested source capture differs")
    capture = parse_scm(scm)
    if not capture.clean or capture.rejected_chunks or capture.run_info is None:
        raise ValueError("incomplete attested source capture")
    return report, capture.run_info, HostContext.from_capture(capture).maps


def verify_export(directory, expected, role, runtime_id, run_info, mapping, identity, digest):
    """Bind the exported inode tuple and the exact executable mapping before opening a portable copy."""
    report, recorded, maps = _evidence(directory, expected)
    row = report["images"][role]
    st = row["identity"]
    if (runtime_id != report["runtime"] or recorded != run_info or maps.find(mapping.start) != mapping
            or st["path"] != mapping.path or tuple(st[k] for k in ("dev", "ino", "size", "mtime_ns")) != identity
            or row["sha256"] != digest):
        raise ValueError("export does not attest this capture, mapping and image")
    return row
