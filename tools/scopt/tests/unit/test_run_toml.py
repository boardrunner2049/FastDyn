"""WP8: per-run TOML rewrite and normalized config hash."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scopt.run.toml_rewrite import (TomlRewriteError, normalized_config_sha256, parse_memory_size,
                                    rewrite_toml, toml_basic_string, verify_rewrite)
from scopt.paths import fastdyn_root

B0_SC = (fastdyn_root() / "src/fastdyn/configs/b0-sc.toml")

SAMPLE = '''# comment with supercache = "nope"
[Memory]

[Memory.main]
id = "ram0"
memory_size  = "512M"
backend      = "file"
memory_file  = "/old/ram0.ram"   # keep this comment

[[Memory.ram1]]
id = "ram1"
memory_size  = "512K"
memory_file  = '/old/ram1.ram'

[[Memory.ram1]]
id = "ram2"
memory_size  = "4K"
backend = "ram"

[Machine]
platform = "generic-cortexm"
supercache = "/old/cache/x.sc"
supercache_flat = false
qmp_socket = "/old/qmp.sock"

[CPU]
[[CPU.cpu0]]
logger_content = """
supercache = "inside a multi-line string"
qmp_socket = "also inside"
"""
cpu = "cortex-m3"
'''


def _rw(text, root="/run/x"):
    return rewrite_toml(text, supercache=f"{root}/cache/x.sc",
                        memory_file=lambda b: f"{root}/ram/{b.id}.ram",
                        qmp_socket=f"{root}/qmp.sock")


def test_rewrite_changes_only_path_lines():
    r = _rw(SAMPLE)
    old, new = SAMPLE.splitlines(), r.text.splitlines()
    assert len(old) == len(new)
    changed = [(a, b) for a, b in zip(old, new) if a != b]
    assert [b for _, b in changed] == [
        'memory_file  = "/run/x/ram/ram0.ram"   # keep this comment',
        "memory_file  = \"/run/x/ram/ram1.ram\"",
        'supercache = "/run/x/cache/x.sc"',
        'qmp_socket = "/run/x/qmp.sock"',
    ]
    cfg = tomllib.loads(r.text)
    assert cfg["CPU"]["cpu0"][0]["logger_content"] == tomllib.loads(SAMPLE)["CPU"]["cpu0"][0][
        "logger_content"]
    assert r.memory_files == {"ram0": "/run/x/ram/ram0.ram", "ram1": "/run/x/ram/ram1.ram"}
    assert "ram2" not in r.memory_files  # ram backend: no file


def test_rewrite_is_deterministic_and_normalized_hash_is_path_free():
    a, b = _rw(SAMPLE, "/run/a"), _rw(SAMPLE, "/run/b")
    assert _rw(SAMPLE, "/run/a").text == a.text
    assert a.text != b.text
    assert normalized_config_sha256(a.text) == normalized_config_sha256(b.text) \
        == normalized_config_sha256(SAMPLE)
    edited = SAMPLE.replace('cpu = "cortex-m3"', 'cpu = "cortex-m4"')
    assert normalized_config_sha256(edited) != normalized_config_sha256(SAMPLE)


def test_missing_qmp_socket_is_inserted():
    text = SAMPLE.replace('qmp_socket = "/old/qmp.sock"\n', "")
    r = _rw(text)
    assert tomllib.loads(r.text)["Machine"]["qmp_socket"] == "/run/x/qmp.sock"
    assert "Machine.qmp_socket" in r.changed


def test_no_cache_arm_stays_without_cache():
    text = SAMPLE.replace('supercache = "/old/cache/x.sc"\n', "")
    r = rewrite_toml(text, supercache=None, memory_file=lambda b: "/r/" + b.id,
                     qmp_socket="/r/q")
    assert "supercache" not in tomllib.loads(r.text)["Machine"]
    assert r.supercache is None


def test_flat_is_refused():
    with pytest.raises(TomlRewriteError) as e:
        _rw(SAMPLE.replace("supercache_flat = false", "supercache_flat = true"))
    assert e.value.code == "unsupported_flat"


def test_verify_rewrite_rejects_other_changes():
    r = _rw(SAMPLE)
    verify_rewrite(SAMPLE, r.text)
    with pytest.raises(TomlRewriteError) as e:
        verify_rewrite(SAMPLE, r.text.replace('platform = "generic-cortexm"', 'platform = "x"'))
    assert e.value.code == "rewrite_changed_other_keys"


def test_basic_string_and_sizes():
    assert toml_basic_string('a"b\\c') == '"a\\"b\\\\c"'
    assert tomllib.loads("k = " + toml_basic_string('we"ird\\path\t\x01'))["k"] == 'we"ird\\path\t\x01'
    assert parse_memory_size("512M") == 512 << 20
    assert parse_memory_size("512K") == 512 << 10
    assert parse_memory_size("0x1000") == 4096


@pytest.mark.real
def test_b0_config_rewrite():
    if not B0_SC.exists():
        pytest.skip(f"{B0_SC} not present")
    text = B0_SC.read_text()
    r = _rw(text, "/data/runs/a")
    diff = [i for i, (x, y) in enumerate(zip(text.splitlines(), r.text.splitlines())) if x != y]
    assert len(diff) == 4 and len(text.splitlines()) == len(r.text.splitlines())
    assert set(r.changed) == {"Machine.supercache", "Machine.qmp_socket",
                              "Memory.main.memory_file", "Memory.ram1[0].memory_file"}
