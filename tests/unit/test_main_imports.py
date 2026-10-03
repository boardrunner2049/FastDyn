"""fastdyn.main works without BoardRunner/PhantomSmith code or their dependencies.

Each test runs a fresh interpreter in which those packages cannot be
imported, so nothing has to be uninstalled from the development venv.
"""

from __future__ import annotations

import subprocess
import sys

BLOCKER = '''
import importlib.abc
import sys

BLOCKED = (
    "scipy", "openai", "ollama",
    "fastdyn.verifier", "fastdyn.llm", "fastdyn.fuzzer", "fastdyn.swarm",
)


class BlockFeaturePackages(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == blocked or name.startswith(blocked + ".") for blocked in BLOCKED):
            raise ImportError(f"blocked for this test: {name}")
        return None


sys.meta_path.insert(0, BlockFeaturePackages())
'''


def _run_blocked(code):
    return subprocess.run([sys.executable, "-c", BLOCKER + code], capture_output=True, text=True)


def test_main_imports_without_feature_packages():
    result = _run_blocked("import fastdyn.main as main\nassert 'run' in main.cli.commands\n")
    assert result.returncode == 0, result.stderr


def test_core_and_feature_command_help_without_feature_packages():
    result = _run_blocked(
        "from click.testing import CliRunner\n"
        "from fastdyn import main\n"
        "for command in ('run', 'loop', 'generate', 'verifier', 'llm', 'swarm', 'probe-run'):\n"
        "    outcome = CliRunner().invoke(main.cli, [command, '--help'])\n"
        "    assert outcome.exit_code == 0, (command, outcome.output)\n"
    )
    assert result.returncode == 0, result.stderr
