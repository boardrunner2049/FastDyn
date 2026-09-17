"""
Compiles device models using the boardrunner SDK CMake build system.

Reads build configuration from boardrunner_sdk/build_config.env and
invokes cmake to build all model .so files in the model/ directory.
"""

import os
import logging
import subprocess
from pathlib import Path

from .. import fastdyn_log as fastdyn_log_conf

log = logging.getLogger(__name__)
fastdyn_log = fastdyn_log_conf.getFastdynLogger()


class CompilationError(Exception):
    """Raised when model compilation fails."""

    def __init__(self, message: str, compiler_output: str = ""):
        self.compiler_output = compiler_output
        super().__init__(message)


# Keys expected in build_config.env
_CONFIG_KEYS = {
    "FASTDYN_INCLUDE_DIR": "Path to FastDyn include directory (containing device.h)",
    "QEMU_INCLUDE_DIR": "Path to QEMU include directory",
}

BUILD_CONFIG_FILENAME = "build_config.env"


def load_build_config(sdk_dir: str) -> dict:
    """Load build configuration from the boardrunner SDK build_config.env file.

    Args:
        sdk_dir: Path to the boardrunner_sdk directory.

    Returns:
        Dict with configuration keys and their values.

    Raises:
        CompilationError: If the config file is missing or has missing keys.
    """
    config_path = Path(sdk_dir) / BUILD_CONFIG_FILENAME

    if not config_path.is_file():
        raise CompilationError(
            "Build configuration file not found: %s\n"
            "Create this file with the following keys:\n%s\n"
            "Example:\n"
            "  FASTDYN_INCLUDE_DIR=/path/to/FastDyn/include\n"
            "  QEMU_INCLUDE_DIR=/path/to/qemu/include"
            % (
                config_path,
                "\n".join("  %s: %s" % (k, v) for k, v in _CONFIG_KEYS.items()),
            )
        )

    config = {}
    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Remove surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                config[key] = value

    missing = [k for k in _CONFIG_KEYS if k not in config or not config[k]]
    if missing:
        raise CompilationError(
            "Missing required key(s) in %s: %s\n"
            "Each key must have a non-empty value."
            % (config_path, ", ".join(missing))
        )

    return config


def _cmake_cache_value(cache_path: Path, key: str) -> str | None:
    if not cache_path.is_file():
        return None
    prefix = key + ":"
    with open(cache_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(prefix) and "=" in line:
                return line.split("=", 1)[1].strip()
    return None


def compile_model(
    sdk_dir: str,
    *,
    fastdyn_include_dir: str | None = None,
    qemu_include_dir: str | None = None,
) -> tuple:
    """Compile all device models in the boardrunner SDK.

    If the build/ directory does not exist under sdk_dir, runs the
    CMake configure step first. Then runs cmake --build.

    Args:
        sdk_dir: Path to the boardrunner_sdk directory.

    Returns:
        Tuple of (success: bool, output: str).
        success is True if compilation succeeded, False otherwise.
        output contains the combined stdout/stderr from the build.

    Raises:
        CompilationError: If the build configuration is missing or invalid.
    """
    sdk_path = Path(sdk_dir).resolve()

    if not sdk_path.is_dir():
        raise CompilationError(
            "Boardrunner SDK directory not found: %s" % sdk_dir
        )

    # Derive include dirs from the checkout layout first
    # (<root>/boardrunner/boardrunner_sdk -> <root>/include, <root>/../qemu/include),
    # matching what `fastdyn run` does; fall back to build_config.env (Docker).
    fastdyn_root = sdk_path.parents[1]
    auto_fastdyn_include = fastdyn_root / "include"
    auto_qemu_include = fastdyn_root.parent / "qemu" / "include"

    fastdyn_include = fastdyn_include_dir
    qemu_include = qemu_include_dir
    if fastdyn_include is None and (auto_fastdyn_include / "device.h").is_file():
        fastdyn_include = str(auto_fastdyn_include)
    if qemu_include is None and auto_qemu_include.is_dir():
        qemu_include = str(auto_qemu_include)

    if fastdyn_include is None or qemu_include is None:
        config = load_build_config(str(sdk_path))
        fastdyn_include = fastdyn_include or config["FASTDYN_INCLUDE_DIR"]
        qemu_include = qemu_include or config["QEMU_INCLUDE_DIR"]

    # Validate paths exist
    if not Path(fastdyn_include).is_dir():
        raise CompilationError(
            "FASTDYN_INCLUDE_DIR does not exist: %s" % fastdyn_include
        )
    if not Path(qemu_include).is_dir():
        raise CompilationError(
            "QEMU_INCLUDE_DIR does not exist: %s" % qemu_include
        )

    build_dir = sdk_path / "build"

    cache_path = build_dir / "CMakeCache.txt"
    needs_configure = (
        not build_dir.is_dir()
        or _cmake_cache_value(cache_path, "FASTDYN_INCLUDE_DIR") != str(Path(fastdyn_include).resolve())
        or _cmake_cache_value(cache_path, "QEMU_INCLUDE_DIR") != str(Path(qemu_include).resolve())
    )

    if needs_configure:
        fastdyn_log.info("Running BoardRunner CMake configure...")
        configure_cmd = [
            "cmake",
            "-S", str(sdk_path),
            "-B", str(build_dir),
            "-DFASTDYN_INCLUDE_DIR=%s" % fastdyn_include,
            "-DQEMU_INCLUDE_DIR=%s" % qemu_include,
        ]

        result = subprocess.run(
            configure_cmd,
            capture_output=True,
            text=True,
            cwd=str(sdk_path),
        )

        if result.returncode != 0:
            output = _combine_output(result)
            fastdyn_log.error("CMake configure failed")
            return False, output

        fastdyn_log.info("CMake configure succeeded")

    # Build step
    fastdyn_log.info("Building models...")
    build_cmd = [
        "cmake",
        "--build", str(build_dir),
        "-j",
    ]

    result = subprocess.run(
        build_cmd,
        capture_output=True,
        text=True,
        cwd=str(sdk_path),
    )

    output = _combine_output(result)

    if result.returncode != 0:
        fastdyn_log.error("Model compilation failed")
        return False, output

    fastdyn_log.info("Model compilation succeeded")
    return True, output


def format_compilation_error(compiler_output: str) -> str:
    """Format compiler output into a string suitable for an LLM follow-up prompt.

    Extracts error lines and provides context for the LLM to fix.

    Args:
        compiler_output: Raw stdout/stderr from the build process.

    Returns:
        Formatted error description string.
    """
    # Extract lines containing "error:" for a focused summary
    lines = compiler_output.split("\n")
    error_lines = [
        line for line in lines
        if "error:" in line.lower() or "undefined reference" in line.lower()
    ]

    summary = "\n".join(error_lines) if error_lines else compiler_output

    return (
        "Compilation Error:\n"
        "The model failed to compile with the following error(s):\n\n"
        "%s\n\n"
        "Full compiler output:\n%s"
        % (summary, compiler_output[-3000:])  # Limit to last 3000 chars
    )


def _combine_output(result: subprocess.CompletedProcess) -> str:
    """Combine stdout and stderr from a subprocess result."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr)
    return "\n".join(parts)
