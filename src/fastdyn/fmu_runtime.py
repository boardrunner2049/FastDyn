"""Prepare a native FMI 3 Co-Simulation library from a packaged FMU."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile


@dataclass(frozen=True)
class FmuRuntime:
    library: Path
    instantiation_token: str
    resources: Path


def prepare(path: Path) -> FmuRuntime:
    """Extract once per archive content, building source-only FMUs when needed.

    Keep the portable archive unchanged. The adjacent .runtime directory owns
    the host binary and resources; its hash marker is written only after a
    successful build. Identifiers and tokens come from FMI metadata, not the
    archive filename or a compiler-specific naming convention.
    """
    from fmpy import extract, platform_tuple, sharedLibraryExtension
    from fmpy.build import build_platform_binary

    path = path.resolve()
    with zipfile.ZipFile(path) as archive:
        description = ET.fromstring(archive.read("modelDescription.xml"))
    if not description.get("fmiVersion", "").startswith("3."):
        raise ValueError("FastDyn requires an FMI 3 FMU")
    profile = description.find("CoSimulation")
    if profile is None:
        raise ValueError("FastDyn requires the FMI Co-Simulation interface")
    identifier = profile.get("modelIdentifier", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", identifier):
        raise ValueError(f"Invalid FMI modelIdentifier: {identifier!r}")
    token = description.get("instantiationToken")
    if not token:
        raise ValueError("FMU is missing its instantiationToken")

    with path.open("rb") as handle:
        fingerprint = hashlib.file_digest(handle, "sha256").hexdigest()
    directory = path.with_suffix(".runtime")
    marker = directory / ".archive.sha256"
    binary = Path("binaries") / platform_tuple / (identifier + sharedLibraryExtension)
    runtime = FmuRuntime(directory / binary, token, directory / "resources")
    if (marker.is_file() and marker.read_text() == fingerprint
            and runtime.library.is_file()):
        return runtime

    with tempfile.TemporaryDirectory(prefix=".fastdyn-fmu-", dir=path.parent) as temporary:
        unpacked = Path(temporary) / "fmu"
        extract(str(path), unzipdir=str(unpacked))
        if not (unpacked / binary).is_file():
            if not (unpacked / "sources/buildDescription.xml").is_file():
                raise ValueError(
                    f"FMU has neither a {platform_tuple} binary nor an FMI 3 source build description"
                )
            build_platform_binary(unpacked)
        if not (unpacked / binary).is_file():
            raise ValueError(f"FMU build did not produce {binary}")
        (unpacked / "resources").mkdir(exist_ok=True)
        (unpacked / ".archive.sha256").write_text(fingerprint)
        if directory.exists():
            shutil.rmtree(directory)
        unpacked.rename(directory)
    return runtime
