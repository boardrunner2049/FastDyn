"""WP8: diag exec log parsing and argv rewriting."""

from __future__ import annotations

from pathlib import Path

from scopt.run.diag import parse_exec_log, rewrite_argv
from scopt.run.launcher import RunDir

LOG = """Loaded reset SP 0x20030000 PC 0x8018741 from vector table
Trace 0: 0x7fff6c000100 [00800400/0000000008018740/00000110/ff020000] Reset_Handler
Linking TBs 0x7fff6c000100 index 0 -> 0x7fff6c000300
Trace 0: 0x7fff6c000300 [00800400/0000000008018766/00000110/ff020000]
"""


def test_parse_exec_log():
    p = parse_exec_log(LOG)
    assert [t["tc"] for t in p["traces"]] == [0x7FFF6C000100, 0x7FFF6C000300]
    assert p["traces"][0]["pc"] == 0x08018740 and p["traces"][0]["cflags"] == 0xFF020000
    assert p["traces"][0]["sym"] == "Reset_Handler" and p["traces"][1]["sym"] is None
    assert p["links"] == [{"src_tc": 0x7FFF6C000100, "index": 0, "dst_tc": 0x7FFF6C000300}]


def test_rewrite_argv_prefix_sibling():
    src = RunDir(Path("/r/warm"))
    argv = ["/q/qemu", "-qmp", "unix:/r/warm/qmp.sock,server,nowait",
            "-object", "memory-backend-file,id=ram0,mem-path=/r/warm/ram/ram0.ram",
            "-d", "in_asm", "-D", "/r/warm/work/qemu.log",
            "-accel", "tcg,supercache=/r/warm/cache/m.sc", "-kernel", "/r/warmer/fw.elf"]
    out = rewrite_argv(argv, src, Path("/r/warm-diag"), d_items="exec,int",
                       log_path=Path("/r/warm-diag/qemu.log"))
    assert out == ["/q/qemu", "-qmp", "unix:/r/warm-diag/qmp.sock,server,nowait",
                   "-object", "memory-backend-file,id=ram0,mem-path=/r/warm-diag/ram/ram0.ram",
                   "-accel", "tcg,supercache=/r/warm-diag/cache/m.sc",
                   "-kernel", "/r/warmer/fw.elf", "-d", "exec,int", "-D", "/r/warm-diag/qemu.log"]


def test_scan_exec_log_streams_the_same_counts(tmp_path):
    """WP14: the streaming scanner (used by diag_exec) agrees with parse_exec_log."""
    import collections

    from scopt.run.diag import scan_exec_log

    log = tmp_path / "qemu.log"
    log.write_text(LOG + LOG.split("\n", 1)[1])  # every Trace/Linking line twice
    p = parse_exec_log(log.read_text())
    s = scan_exec_log(log)
    assert s["trace_lines"] == len(p["traces"]) == 4
    assert s["tc_counts"] == collections.Counter(t["tc"] for t in p["traces"])
    assert s["entries"] == collections.Counter((t["pc"], t["flags"], t["cs_base"])
                                               for t in p["traces"])
    assert s["links"] == p["links"][:1]  # distinct links, first-seen order


def test_prepare_direct_diag_rewrites_the_cache_path(tmp_path):
    """WP28: gdb_hits/prepare_direct_diag on a direct (no FastDyn) run directory."""
    import json
    import os

    import pytest

    from scopt.run.diag import DiagError, gdb_hits, is_direct_run, prepare_direct_diag

    src = tmp_path / "run"
    (src / "cache").mkdir(parents=True)
    sc = src / "cache" / "alias.sc"
    sc.write_bytes(b"SC" + b"\0" * 62)
    argv = ["/q/qemu-system-arm", "-M", "mps2-an385", "-accel", f"tcg,supercache={sc}"]
    (src / "direct.json").write_text(json.dumps({"argv": argv, "cache": str(sc)}))
    assert is_direct_run(src) and not is_direct_run(tmp_path)
    os.environ["QEMU_SC_V6_STATS"] = "/elsewhere.json"
    try:
        p = prepare_direct_diag(src, tmp_path / "d", v6_stats=True)
    finally:
        del os.environ["QEMU_SC_V6_STATS"]
    new = tmp_path / "d" / "cache" / "alias.sc"
    assert p["argv"][-1] == f"tcg,supercache={new}" and new.read_bytes() == sc.read_bytes()
    assert p["cwd"] == tmp_path / "d"
    assert p["env"]["QEMU_SC_V6_STATS"] == str(tmp_path / "d" / "v6-stats.json")
    with pytest.raises(DiagError) as e:
        prepare_direct_diag(src, tmp_path / "d")
    assert e.value.code == "diag_dir_not_fresh"
    with pytest.raises(DiagError) as e:
        gdb_hits(src, tmp_path / "h", [("x", 1)], inputs={"input": "/x.bin"})
    assert e.value.code == "input_bank_missing"
    # an argv that does not name the recorded cache is refused
    (src / "direct.json").write_text(json.dumps({"argv": argv[:-1] + ["tcg"], "cache": str(sc)}))
    with pytest.raises(DiagError) as e:
        prepare_direct_diag(src, tmp_path / "d2")
    assert e.value.code == "argv_rewrite"
