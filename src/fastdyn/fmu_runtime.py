"""Resolve the binary and metadata of the same FMI 3 Co-Simulation artifact."""

from dataclasses import dataclass
from pathlib import Path
import os
import platform
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from .fmu_build import FmuBuild, FmuConfigError, value_references


@dataclass(frozen=True)
class FmuRuntime:
    library_path: Path
    instantiation_token: str
    resource_path: str | None
    value_references: dict[str, int]


def prepare_runtime(build: FmuBuild, work_dir: Path) -> FmuRuntime:
    """Prepare an isolated package for this run, or use an unpacked build tree.

    A packaged FMU is authoritative: never combine its binary with XML from
    an adjacent build tree, which may belong to a different export/UUID.
    """
    if build.package:
        work_dir = work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="fmu-", dir=work_dir))
        try:
            with zipfile.ZipFile(build.fmu_path) as archive:
                for member in archive.infolist():
                    if not (root / member.filename).resolve().is_relative_to(root):
                        raise FmuConfigError(f"FMU contains an invalid path: {member.filename}")
                archive.extractall(root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise FmuConfigError(f"Cannot unpack FMU {build.fmu_path}: {exc}") from exc
    else:
        root = build.output.resolve()
        if not (root / "modelDescription.xml").is_file():
            root /= build.model.replace(".", "_")

    description = root / "modelDescription.xml"
    try:
        xml = description.read_bytes()
        metadata = ET.fromstring(xml)
    except (OSError, ET.ParseError) as exc:
        raise FmuConfigError(f"Cannot read FMU metadata {description}: {exc}") from exc
    if metadata.get("fmiVersion", "").split(".")[0] != "3":
        raise FmuConfigError(f"FMU requires FMI 3: {description}")
    cs = metadata.find("CoSimulation")
    identifier = cs.get("modelIdentifier", "") if cs is not None else ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", identifier):
        raise FmuConfigError(f"FMU has no valid CoSimulation modelIdentifier: {description}")
    token = metadata.get("instantiationToken", "")
    if not token:
        raise FmuConfigError(f"FMU has no instantiationToken: {description}")

    architectures = {"x86_64": "x86_64-linux", "aarch64": "aarch64-linux"}
    arch = platform.machine()
    if platform.system() != "Linux" or arch not in architectures:
        raise FmuConfigError("FMU runtime currently supports x86_64/aarch64 Linux")
    candidates = [root / "binaries" / architectures[arch] / (identifier + ".so")]
    # Older Rumoca exports used the FMI 2 platform directory for FMI 3.
    if arch == "x86_64":
        candidates.append(root / "binaries/linux64" / (identifier + ".so"))
    library = next((path for path in candidates if path.is_file()), None)
    if library is None:
        raise FmuConfigError(
            f"FMU binary for {identifier} is missing; expected "
            + " or ".join(map(str, candidates))
        )
    resources = root / "resources"
    return FmuRuntime(
        library_path=library,
        instantiation_token=token,
        resource_path=str(resources) + os.sep if resources.is_dir() else None,
        value_references=value_references(build, xml=xml),
    )
