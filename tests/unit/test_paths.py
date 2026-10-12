"""fastdyn.paths finds the FastDyn checkout and the default files inside it."""

from __future__ import annotations

from pathlib import Path

import pytest

from fastdyn import paths

CHECKOUT = Path(paths.__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_root_override(monkeypatch):
    monkeypatch.delenv(paths.REPO_ROOT_ENV, raising=False)


def test_checkout_is_detected_from_the_package_location():
    assert paths.repo_root() == CHECKOUT
    assert paths.virtuals_dir() == CHECKOUT / "virtuals"


def test_defaults_point_into_the_checkout():
    assert paths.default_svd_dir() == str(CHECKOUT / "third_party/common/cmsis-svd-data")
    assert paths.default_plugin_library() == str(CHECKOUT / "build/libfastdyn.so")


def test_environment_variable_overrides_the_checkout(monkeypatch, tmp_path):
    (tmp_path / "virtuals").mkdir()
    monkeypatch.setenv(paths.REPO_ROOT_ENV, str(tmp_path))

    assert paths.repo_root() == tmp_path.resolve()
    assert paths.virtuals_dir() == tmp_path.resolve() / "virtuals"
    assert paths.default_plugin_library() == str(tmp_path.resolve() / "build/libfastdyn.so")


def test_without_a_checkout_the_relative_defaults_remain(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "_PACKAGE_DIR", tmp_path / "site-packages" / "fastdyn")

    assert paths.repo_root() is None
    assert paths.virtuals_dir() is None
    assert paths.default_svd_dir() == "third_party/common/cmsis-svd-data"
    assert paths.default_plugin_library() == "build/libfastdyn.so"
