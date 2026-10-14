"""Compose FIRE source before invoking the configured Modelica compiler."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

FIRE_REL = Path("third_party/courbet_deps/fire_modelica_models")


def input_digest(build) -> str:
    """Include authored sources and generator inputs, never generated build trees."""
    root = build.repo_root / FIRE_REL
    paths = {build.compose_config, build.config_path, build.rumoca_dir / "Cargo.lock"}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in {"build", "target", ".git", "__pycache__"} for part in rel.parts):
            continue
        if path.is_file() and (path.suffix in {".mo", ".rs"} or path.name in {"package.order", "Cargo.toml", "Cargo.lock"}):
            paths.add(path)
    compiler_sources = build.rumoca_dir / "crates"
    if compiler_sources.is_dir():
        paths.update(p for p in compiler_sources.rglob("*")
                     if p.is_file() and p.suffix in {".rs", ".jinja", ".toml"})
    digest = hashlib.sha256()
    digest.update(json.dumps({"model": build.model, "compose_table": build.compose_table, "profile": "vio"},
                             sort_keys=True).encode())
    digest.update(os.environ.get("FASTDYN_MODELICA_LIBRARY", "default").encode())
    digest.update(str(build.model_file).encode())
    digest.update(str(build.rumoca_dir).encode())
    for path in sorted(p for p in paths if p is not None):
        digest.update(str(path).encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"missing")
        digest.update(b"\0")
    return digest.hexdigest()


def compose(build) -> Path:
    from .fmu_build import FmuConfigError

    root = build.repo_root / FIRE_REL
    if build.compose_config is None or not build.compose_config.is_file():
        raise FmuConfigError(f"FIRE config not found: {build.compose_config}")
    command = ["cargo", "run", "--locked", "--manifest-path",
               str(root / "tools/fire-compose/Cargo.toml"), "--", "emit",
               str(build.compose_config), "--profile", "vio", "--output-dir",
               str(build.model_file.parent), "--library-root", str(root)]
    if build.compose_table is not None:
        command.extend(["--config-table", build.compose_table])
    subprocess.run(command, cwd=build.repo_root, check=True)
    manifest = build.model_file.with_suffix(".manifest.json")
    if not manifest.is_file():
        raise FmuConfigError("FIRE model_name must match [FMU.models.<name>].model and model_file")
    data = json.loads(manifest.read_text())
    if data["model"] != build.model or build.model_file.name != build.model + ".mo":
        raise FmuConfigError("FIRE model_name must match the FMU model and model_file")
    return manifest


def stage_msl(build) -> None:
    """Keep portable MSL and FIRE in one root for transitive CLI discovery."""
    from .fmu_build import FmuConfigError

    root = Path(os.environ.get("FASTDYN_MODELICA_LIBRARY", str(build.repo_root / "out/toolchains/ModelicaStandardLibrary"))).expanduser().resolve()
    if not (root / "Modelica/package.mo").is_file():
        raise FmuConfigError(
            f"Portable Modelica Standard Library not found at {root}. "
            "Run utils/setup_fire_toolchain.py, or set FASTDYN_MODELICA_LIBRARY "
            "to a ModelicaStandardLibrary checkout (not OpenModelica's vendor library)."
        )
    stage = build.model_file.parent / "sources"
    for name in ("Modelica", "ModelicaServices", "Complex.mo"):
        target = root / name
        if not target.exists():
            raise FmuConfigError(f"Missing MSL dependency: {target}")
        link = stage / name
        if link.is_symlink():
            if link.resolve() == target:
                continue
            link.unlink()
        elif link.exists():
            raise FmuConfigError(f"Refusing to replace non-symlink dependency: {link}")
        link.symlink_to(target, target_is_directory=target.is_dir())


def record_compilation(build, status: str) -> None:
    path = build.model_file.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data["verification"]["fmi_generation"] = status
    revision = subprocess.run(["git", "-C", str(build.rumoca_dir), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
    data["compiler"] = {"directory": str(build.rumoca_dir),
                        "git_revision": revision.stdout.strip() if revision.returncode == 0 else None}
    path.write_text(json.dumps(data, indent=2) + "\n")
