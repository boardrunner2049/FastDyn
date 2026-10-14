"""Exercise setup's QEMU lifecycle with local Git sources and a tiny build.

Git, patch application, and make are real; no network or QEMU compiler is used.
"""

import os
from pathlib import Path
import subprocess

import pytest


SETUP = Path(__file__).resolve().parents[2] / "setup.sh"
HEADER = Path("include/qemu/qemu-plugin.h")


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def run_function(name, *args):
    return subprocess.run(
        [
            "bash", "-ec",
            'source "$1" --help >/dev/null; shift; "$@"',
            "test-setup", str(SETUP), name, *map(str, args),
        ],
        capture_output=True, text=True, timeout=20,
    )


def assert_success(result):
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def source_repo(tmp_path):
    root = tmp_path / "origin"
    root.mkdir()
    git(root, "init", "-b", "fastdyn")
    (root / HEADER).parent.mkdir(parents=True)
    (root / HEADER).write_text("base\n")
    (root / "local-change").write_text("original\n")
    configure = root / "configure"
    configure.write_text(
        "#!/usr/bin/env bash\nset -e\n"
        'printf "%s\\n" "$@" > configure.args\n'
        "cat > Makefile <<'MAKE'\n"
        ".PHONY: qemu-system-arm\n"
        "qemu-system-arm:\n"
        "\tcat ../include/qemu/qemu-plugin.h ../local-change > qemu-system-arm\n"
        "\tchmod +x qemu-system-arm\n"
        "\techo build >> build-runs\n"
        "MAKE\n"
        "touch build.ninja\n"
    )
    configure.chmod(0o755)
    git(root, "add", ".")
    git(
        root, "-c", "user.name=Setup Test", "-c", "user.email=setup@example.test",
        "-c", "commit.gpgsign=false", "commit", "-m", "QEMU fixture",
    )
    git(root, "tag", "test-ref")
    return root


@pytest.fixture
def patch_repo(tmp_path):
    root = tmp_path / "fastdyn"
    (root / "patches").mkdir(parents=True)
    (root / "patches/qemu-fastdyn-plugin-icount.patch").write_text(
        "diff --git a/include/qemu/qemu-plugin.h b/include/qemu/qemu-plugin.h\n"
        "--- a/include/qemu/qemu-plugin.h\n"
        "+++ b/include/qemu/qemu-plugin.h\n"
        "@@ -1 +1 @@\n"
        "-base\n"
        "+patched\n"
    )
    return root


@pytest.mark.parametrize("existing_ws", [False, True])
@pytest.mark.parametrize("ref", ["fastdyn", "test-ref"])
def test_clone_downloads_only_and_preserves_workspace(tmp_path, source_repo, existing_ws, ref):
    target = tmp_path / "qemu with spaces"
    ram = target / "ws/my_m4_ram"
    if existing_ws:
        ram.parent.mkdir(parents=True)
        ram.write_bytes(b"existing RAM")

    assert_success(run_function("fastdyn_setup_qemu_clone", target, source_repo, ref))

    assert git(target, "rev-parse", "HEAD") == git(source_repo, "rev-parse", ref)
    assert (target / HEADER).read_text() == "base\n"
    assert not (target / "build").exists()
    if existing_ws:
        assert ram.read_bytes() == b"existing RAM"


@pytest.mark.parametrize("worktree", [False, True])
def test_clone_preserves_existing_branch_remote_and_changes(tmp_path, source_repo, worktree):
    target = tmp_path / "qemu"
    if worktree:
        git(source_repo, "worktree", "add", "-b", "local-work", str(target))
    else:
        assert_success(run_function("fastdyn_setup_qemu_clone", target, source_repo, "fastdyn"))
        git(target, "checkout", "-b", "local-work")
    git(target, "remote", "add", "personal", "/missing/personal-origin")
    (target / HEADER).write_text("local changes\n")
    git(target, "add", str(HEADER))
    (target / "local-change").write_text("unstaged changes\n")
    before = (
        git(target, "rev-parse", "HEAD"), git(target, "branch", "--show-current"),
        git(target, "remote", "-v"), git(target, "diff", "--cached"), git(target, "diff"),
    )

    assert_success(run_function("fastdyn_setup_qemu_clone", target, "/missing/repo", "missing-ref"))

    assert before == (
        git(target, "rev-parse", "HEAD"), git(target, "branch", "--show-current"),
        git(target, "remote", "-v"), git(target, "diff", "--cached"), git(target, "diff"),
    )


def test_clone_can_retry_failed_fetch(tmp_path, source_repo):
    target = tmp_path / "qemu"
    failed = run_function("fastdyn_setup_qemu_clone", target, source_repo, "missing-ref")
    assert failed.returncode != 0
    assert_success(run_function("fastdyn_setup_qemu_clone", target, source_repo, "fastdyn"))
    assert (target / HEADER).read_text() == "base\n"


def test_clone_does_not_overwrite_untracked_files(tmp_path, source_repo):
    target = tmp_path / "qemu"
    target.mkdir()
    (target / "configure").write_text("keep this file\n")
    result = run_function("fastdyn_setup_qemu_clone", target, source_repo, "fastdyn")
    assert result.returncode != 0
    assert (target / "configure").read_text() == "keep this file\n"


def test_build_requires_sources_before_environment_setup(tmp_path):
    target = tmp_path / "qemu"
    venv = tmp_path / "venv"
    result = subprocess.run(
        ["bash", str(SETUP), "--build-qemu", "--qemu-root", str(target), "--venv", str(venv)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "--clone-qemu --build-qemu" in result.stderr
    assert not target.exists()
    assert not venv.exists()


def test_build_patches_and_rebuilds_local_sources_without_fetching(source_repo, patch_repo):
    git(source_repo, "remote", "add", "origin", "/missing/offline-origin")
    git(source_repo, "checkout", "-b", "local-work")
    head = git(source_repo, "rev-parse", "HEAD")
    assert_success(run_function("fastdyn_setup_qemu_build", patch_repo, source_repo))
    binary = source_repo / "build/qemu-system-arm"
    assert binary.read_text() == "patched\noriginal\n"

    (source_repo / "local-change").write_text("updated locally\n")
    assert_success(run_function("fastdyn_setup_qemu_build", patch_repo, source_repo))

    assert binary.read_text() == "patched\nupdated locally\n"
    assert (source_repo / "build/build-runs").read_text().splitlines() == ["build", "build"]
    assert git(source_repo, "rev-parse", "HEAD") == head
    assert git(source_repo, "branch", "--show-current") == "local-work"
    assert git(source_repo, "remote", "get-url", "origin") == "/missing/offline-origin"


def test_existing_binary_is_rebuilt_after_patch_application(source_repo, patch_repo):
    build = source_repo / "build"
    build.mkdir()
    binary = build / "qemu-system-arm"
    binary.write_text("old unpatched binary\n")
    binary.chmod(0o755)

    assert_success(run_function("fastdyn_setup_qemu_build", patch_repo, source_repo))
    assert binary.read_text() == "patched\noriginal\n"


def test_patch_failure_stops_build_even_with_existing_binary(source_repo, patch_repo):
    (source_repo / HEADER).write_text("incompatible source\n")
    build = source_repo / "build"
    build.mkdir()
    binary = build / "qemu-system-arm"
    binary.write_text("existing binary\n")
    binary.chmod(0o755)

    result = run_function("fastdyn_setup_qemu_build", patch_repo, source_repo)

    assert result.returncode != 0
    assert "does not apply cleanly" in result.stderr
    assert not (build / "configure.args").exists()
    assert binary.read_text() == "existing binary\n"


def test_build_initializes_pinned_submodule_without_advancing_ref(source_repo, patch_repo):
    relative = "third_party/common/qemu"
    target = patch_repo / relative
    git(patch_repo, "init")
    git(patch_repo, "-c", "protocol.file.allow=always", "submodule", "add", str(source_repo), relative)
    pinned = git(target, "rev-parse", "HEAD")
    git(patch_repo, "submodule", "deinit", "-f", relative)
    (source_repo / "local-change").write_text("new upstream commit\n")
    git(source_repo, "add", ".")
    git(source_repo, "-c", "user.name=Setup Test", "-c", "user.email=setup@example.test",
        "-c", "commit.gpgsign=false", "commit", "-m", "Advance upstream")

    assert_success(run_function("fastdyn_setup_qemu_build", patch_repo, target))

    assert git(target, "rev-parse", "HEAD") == pinned
    assert (target / "build/qemu-system-arm").read_text() == "patched\noriginal\n"
    (target / "local-change").write_text("local submodule edit\n")
    assert_success(run_function("fastdyn_setup_qemu_build", patch_repo, target))
    assert git(target, "rev-parse", "HEAD") == pinned
    assert (target / "build/qemu-system-arm").read_text() == "patched\nlocal submodule edit\n"


@pytest.mark.parametrize(
    "options,expected",
    [
        ([], []),
        (["--clone-qemu"], ["clone"]),
        (["--build-qemu"], ["build", "plugin"]),
        (["--clone-qemu", "--build-qemu"], ["clone", "build", "plugin"]),
        (["--build-qemu", "--clone-qemu"], ["clone", "build", "plugin"]),
        (["--clone-qemu", "--build-qemu", "--build-gazebo"], ["clone", "build", "gazebo"]),
    ],
)
def test_main_dispatches_requested_steps_in_dependency_order(tmp_path, source_repo, options, expected):
    # Stub unrelated setup work and lifecycle functions to isolate CLI dispatch.
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin/python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    calls = tmp_path / "calls"
    script = '''
source "$1" --help >/dev/null
shift
ctags() { :; }
fastdyn_setup_qemu_clone() { echo clone >> "$SETUP_CALLS"; }
fastdyn_setup_qemu_build() { echo build >> "$SETUP_CALLS"; }
fastdyn_setup_plugin_build() { echo plugin >> "$SETUP_CALLS"; }
fastdyn_setup_gazebo_plugin_build() { echo gazebo >> "$SETUP_CALLS"; }
fastdyn_setup_main "$fastdyn_setup_repo_root" "$@"
'''
    result = subprocess.run(
        [
            "bash", "-ec", script, "test-setup", str(SETUP),
            "--python", str(python), "--venv", str(venv),
            "--qemu-root", str(source_repo), "--skip-submodules", "--skip-optifuzz",
            "--skip-qemu-workspace", "--no-upgrade-pip", *options,
        ],
        env={**os.environ, "SETUP_CALLS": str(calls)},
        capture_output=True, text=True, timeout=10,
    )
    assert_success(result)
    assert (calls.read_text().splitlines() if calls.exists() else []) == expected
