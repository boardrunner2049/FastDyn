"""FastDyn owns invalidation for QEMU's trusted persistent JIT cache."""

from __future__ import annotations

import json

from fastdyn.targets import qemu_target


def _inputs(tmp_path):
    config = tmp_path / "config.toml"
    generated = tmp_path / "virtuals.txt"
    plugin = tmp_path / "libfastdyn.so"
    config.write_text("[Machine]\ncoverage = false\n", encoding="utf-8")
    generated.write_text("0x1000 benchmark_start\n", encoding="utf-8")
    plugin.write_bytes(b"plugin-v1")
    return config, generated, plugin


def _configure(cache, config, generated, plugin, *, coverage=False, flat=False):
    return qemu_target._configure_supercache(
        cache,
        flat=flat,
        settings={"plugin_options": [f"coverage={int(coverage)}"]},
        content_files={"config": config, "virtuals": generated},
        native_files={"plugin": plugin},
    )


def test_unchanged_inputs_keep_warm_cache(tmp_path):
    cache = tmp_path / "firmware.sc"
    config, generated, plugin = _inputs(tmp_path)
    _configure(cache, config, generated, plugin)
    cache.write_bytes(b"warm-cache")

    manifest_path = tmp_path / "firmware.sc.fastdyn.json"
    manifest_mtime = manifest_path.stat().st_mtime_ns
    accel = _configure(cache, config, generated, plugin)

    assert cache.read_bytes() == b"warm-cache"
    assert accel == f"tcg,supercache={cache}"
    assert manifest_path.stat().st_mtime_ns == manifest_mtime
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == qemu_target.SUPERCACHE_MANIFEST_VERSION
    assert len(manifest["fingerprint"]) == 64


def test_flat_mode_is_added_to_accelerator_option(tmp_path):
    cache = tmp_path / "firmware.sc"
    config, generated, plugin = _inputs(tmp_path)

    accel = _configure(cache, config, generated, plugin, flat=True)

    assert accel == f"tcg,supercache={cache},supercache-flat=on"


def test_generated_instrumentation_change_invalidates_cache(tmp_path):
    cache = tmp_path / "firmware.sc"
    config, generated, plugin = _inputs(tmp_path)
    _configure(cache, config, generated, plugin)
    cache.write_bytes(b"stale")
    generated.write_text("0x2000 benchmark_end\n", encoding="utf-8")

    _configure(cache, config, generated, plugin)

    assert not cache.exists()


def test_effective_plugin_option_change_invalidates_cache(tmp_path):
    cache = tmp_path / "firmware.sc"
    config, generated, plugin = _inputs(tmp_path)
    _configure(cache, config, generated, plugin, coverage=False)
    cache.write_bytes(b"stale")

    _configure(cache, config, generated, plugin, coverage=True)

    assert not cache.exists()


def test_native_plugin_rebuild_invalidates_without_hashing_it(tmp_path):
    cache = tmp_path / "firmware.sc"
    config, generated, plugin = _inputs(tmp_path)
    _configure(cache, config, generated, plugin)
    cache.write_bytes(b"stale")
    plugin.write_bytes(b"plugin-v2-is-larger")

    _configure(cache, config, generated, plugin)

    assert not cache.exists()


def test_existing_cache_without_manifest_is_stale(tmp_path):
    cache = tmp_path / "firmware.sc"
    config, generated, plugin = _inputs(tmp_path)
    cache.write_bytes(b"unknown-owner")

    _configure(cache, config, generated, plugin)

    assert not cache.exists()
