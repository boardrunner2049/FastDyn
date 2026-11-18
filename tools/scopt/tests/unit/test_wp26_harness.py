"""WP26: run harness for v6 and the dynamic oracles (B3/B4 design §5.1, §5.7, §9.3-§9.5, §10.1).

Unit part of the plan's acceptance:

* the O2b comparator on recorded logs (equal, shifted PC, shifted CVR), plus
  the other comparators (O1, O2a, O3, O4, O5, O6) on recorded or synthetic data;
* a negative-control artifact is refused by ``scopt publish`` (and by the
  publish primitive, deploy and campaign preflight);
* refusal-code pinning for a changed patch plan (F-B3-16(a)) and a
  coverage-enabled configuration (F-B3-16(e)).

Recorded inputs live in ``tests/data/wp26`` (copies; ``PROVENANCE.md`` there
names each source run). Tests on real snapshots or FastDyn files are marked
``real`` and skip when the files are absent or stale.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.run import oracles as O
from scopt.run.diag import (FLUSH_FUNCS, artifact_probe_addrs, parse_hits, parse_twin,
                            read_v6_stats, stats_env)
from scopt.paths import fastdyn_root

DATA = Path(__file__).resolve().parent.parent / "data" / "wp26"
LOGS = DATA / "logs"
FIXTURE = (fastdyn_root() / "fixtures/b3")


def _log(name: str) -> str:
    return (LOGS / name).read_text()


def _shift(text: str, kind: int, rec: int, field: int, delta: int) -> str:
    """Add ``delta`` to field ``field`` of record ``rec`` of kind ``kind`` (0x53
    SysTick, 0x54 fault, 0x55 IRQ) in a recorded guest log."""
    lines = text.splitlines()
    want = f"Reg 0:0x{(kind << 16) | field:08x}"
    k = -1
    for i, x in enumerate(lines):
        if x == f"Reg 0:0x{kind << 16:08x}":
            k += 1
        if k == rec and x == want:
            v = int(lines[i + 1][6:], 16)
            lines[i + 1] = f"Reg 1:0x{(v + delta) & 0xFFFFFFFF:08x}"
            return "\n".join(lines) + "\n"
    raise AssertionError("record not found")


# ---------------------------------------------------------------------------
# O2b: handler records (acceptance: equal, shifted PC, shifted CVR)
# ---------------------------------------------------------------------------

def test_o2b_parser_on_recorded_logs():
    p = O.parse_handler_records(_log("7-icount-warm-1.norm"))
    assert len(p["systick"]) == 214 and p["end"][3] == 214 and not p["errors"]
    assert all(len(r) == len(O.SREC) for r in p["systick"])
    f = O.parse_handler_records(_log("3b-icount-warm-1.norm"))
    assert len(f["fault"]) == 1 and f["fault"][0]["cfsr"] == 0x8200 and \
        f["fault"][0]["iter"] == 77 and f["fault"][0]["bfar"] == 0xFFFFFFFF
    i = O.parse_handler_records(_log("b4-9-icount-warm-1.norm"))
    assert len(i["irq"]) == 100 and i["end"][4] == 100


def test_o2b_equal_recorded_runs():
    """Two recorded icount runs of F-B3-7 (nocache and warm) are equal."""
    r = O.o2b_records(_log("7-icount-nocache-1.norm"), _log("7-icount-warm-1.norm"), "icount")
    assert r.verdict == "pass", r.detail
    assert r.data["records"]["systick"] == {"reference": 214, "artifact": 214}


@pytest.mark.parametrize("field,name", [(1, "pc"), (0, "cvr")])
@pytest.mark.parametrize("log", ["7-icount-warm-1.norm", "b4-3-icount-warm-1.norm"])
def test_o2b_detects_shifted_pc_and_cvr(log, field, name):
    """An interrupt taken one instruction later (stacked PC + 2) or after a
    different instruction count (CVR - 1) is a difference in icount mode."""
    ref = _log(log)
    art = _shift(ref, 0x53, 5, field, 2 if name == "pc" else -1)
    r = O.o2b_records(ref, art, "icount")
    assert r.verdict == "fail" and f"systick[5].{name}" in r.detail, r.detail
    assert r.data["differences"] == 1


def test_o2b_noicount_mode_is_invariant_based():
    """noicount: SysTick positions depend on host time and are not compared
    (two real noicount runs of F-B3-7 have different record counts, yet are
    consistent), while a shifted CVR is not a difference there but a shifted
    fault position is, in both modes."""
    a, b = _log("7-noicount-nocache-1.norm"), _log("7-noicount-warm-1.norm")
    assert O.o2b_records(a, b, "noicount").verdict == "pass"
    assert O.o2b_records(a, b, "icount").verdict == "fail"
    ref = _log("7-icount-warm-1.norm")
    assert O.o2b_records(ref, _shift(ref, 0x53, 3, 0, 5), "noicount").verdict == "pass"
    f = _log("3b-icount-warm-1.norm")
    for mode in ("icount", "noicount"):
        r = O.o2b_records(f, _shift(f, 0x54, 0, 0, 2), mode)
        assert r.verdict == "fail" and "fault[0].pc" in r.detail
    # an iteration-shifted fault is a different fault position too
    r = O.o2b_records(f, _shift(f, 0x54, 0, 6, 1), "noicount")
    assert r.verdict == "fail" and "fault[0].iter" in r.detail
    # spare-IRQ records (raised by a TB-execution count) keep their position in noicount
    i1, i0 = _log("b4-9-icount-warm-1.norm"), _log("b4-9-noicount-warm-1.norm")
    assert O.o2b_records(i1, i0, "noicount").verdict == "pass"
    r = O.o2b_records(i0, _shift(i0, 0x55, 7, 0, 2), "noicount")
    assert r.verdict == "fail" and "irq[7].pc" in r.detail


def test_o2b_structural_failures():
    ref = _log("7-icount-warm-1.norm")
    lines = ref.splitlines()
    # drop the last SysTick record's iteration field: incomplete and end counter mismatch
    cut = next(i for i in range(len(lines) - 1, 0, -1) if lines[i] == "Reg 0:0x00530007")
    art = "\n".join(lines[:cut] + lines[cut + 2:]) + "\n"
    r = O.o2b_records(ref, art, "noicount")
    assert r.verdict == "fail" and "incomplete" in r.detail
    r = O.o2b_records(ref, "Current CPU type: cortex-m3-arm-cpu\n", "icount")
    assert r.verdict == "fail" and "no guest record pairs" in r.detail
    with pytest.raises(ValueError):
        O.o2b_records(ref, ref, "shift0")


@pytest.mark.real
def test_o2b_with_fixture_invariants():
    """The fixture's named invariants (fixtures/b3/expected.py) run on the artifact
    log; a recorded run passes them, a corrupted result fails them."""
    if not (FIXTURE / "expected.py").is_file():
        pytest.skip("fixtures/b3 not present")
    fx = O.load_fixture_expected(FIXTURE / "expected.py")
    ref = _log("7-noicount-nocache-1.norm")
    art = _log("7-noicount-warm-1.norm")
    ok = O.o2b_records(ref, art, "noicount", invariants=lambda: fx(art, "7", "noicount"))
    assert ok.verdict == "pass", ok.detail
    lines = art.splitlines()
    k = lines.index("Reg 0:0x005f0003")  # end.systicks no longer equals the record count
    lines[k + 1] = f"Reg 1:0x{int(lines[k + 1][6:], 16) + 1:08x}"
    bad = "\n".join(lines) + "\n"
    r = O.o2b_records(ref, bad, "noicount", invariants=lambda: fx(bad, "7", "noicount"))
    assert r.verdict == "fail" and "fixture invariant" in r.detail


def test_fixture_norm_form():
    gn = ["Current CPU type: cortex-m3-arm-cpu", "Reg 0:0x00000003",
          "[BENCH][b3-1] elapsed=<host-time>",
          "fastdyn.targets.qemu_target|INFO|  QEMU exited with code 0"]
    assert O.fixture_norm(gn, 0) == ("Current CPU type: cortex-m3-arm-cpu\nReg 0:0x00000003\n"
                                     "[BENCH][b3-1] elapsed=<host-time>\nexit_code=0\n")
    assert O.icount_mode('[Machine]\nicount = { shift = 0 }\n') == "icount"
    assert O.icount_mode('[Machine]\n# icount = { shift = 0 }\n') == "noicount"


# ---------------------------------------------------------------------------
# O1, O2a, O3, O4, O5, O6
# ---------------------------------------------------------------------------

def _run(**kw):
    d = {"verdict": "PASS", "exit_code": 0, "output_norm_sha256": "sha256:aa",
         "guest_norm": ["Reg 0:0x1"], "proof": {"verdict": "PASS", "appended": 0,
                                                 "failed": [], "file_version": 6}}
    d.update(kw)
    return d


def test_o1_outputs():
    assert O.o1_outputs(_run(), _run()).verdict == "pass"
    r = O.o1_outputs(_run(), _run(guest_norm=["Reg 0:0x2"]))
    assert r.verdict == "fail" and "guest_norm differs" in r.detail
    r = O.o1_outputs(_run(), _run(verdict="INVALID_artifact_not_used"))
    assert r.verdict == "fail" and "artifact run verdict" in r.detail
    assert r.data["differences"] == [] and r.data["unproved"] == ["artifact"]
    r = O.o1_outputs(_run(), _run(exit_code=134, guest_norm=["x"]))
    assert r.data["differences"] == ["exit_code", "guest_norm"] and r.data["unproved"] == []
    r = O.o1_outputs(_run(), _run(guest_norm=["x"]), fixture=lambda: [], exact=False)
    assert r.verdict == "pass"  # noicount timer phase: the fixture oracle decides
    r = O.o1_outputs(_run(), _run(), fixture=lambda: ["result[0]: got 1, want 0"])
    assert r.verdict == "fail" and "fixture oracle" in r.detail
    with pytest.raises(ValueError):
        O.o1_outputs(_run(), _run(), exact=False)


def test_o2a_on_recorded_int_logs():
    a = (DATA / "int" / "7-nc1.int.log").read_text()
    b = (DATA / "int" / "7-warm.int.log").read_text()
    r = O.o2a_int(a, b)
    assert r.verdict == "pass" and r.data["lines"]["reference"] == 1500
    lines = b.splitlines()
    k = next(i for i, x in enumerate(lines) if x.startswith("...taking pending") and i > 200)
    lines[k] = lines[k].replace("exception 15", "exception 21")
    r = O.o2a_int(a, "\n".join(lines) + "\n")
    assert len(O.parse_int_log(b)) == len(b.splitlines())  # every line is an exception line
    assert r.verdict == "fail" and r.data["first_difference"]["line"] == k
    # a loader warning line is not an exception line
    assert O.o2a_int(a, "qemu-system-arm: warning: x\n" + b).verdict == "pass"
    assert O.o2a_int(None, b).verdict == "not_run"


def test_o3_nonvacuity():
    st = read_v6_stats(DATA / "stats" / "guarded.json")
    hits = {"targets": [{"what": "variant 0 C'", "hits": 990}, {"what": "slot 0 F", "hits": 0}]}
    req = {"stats": {"links_armed": ">0", "refused.breakpoints": "==0"},
           "hits": {"variant 0 C'": ">0"}}
    r = O.o3_nonvacuity(st, hits, req)
    assert r.verdict == "pass", r.detail
    r = O.o3_nonvacuity(st, hits, {"stats": {"variant_entry_exits": ">0"}})
    assert r.verdict == "vacuous" and "variant_entry_exits = 0" in r.detail
    r = O.o3_nonvacuity(st, hits, {"hits": {"slot 0 F": ">0"}})
    assert r.verdict == "vacuous"
    # not collected is unknown, never zero
    r = O.o3_nonvacuity(read_v6_stats(DATA / "stats" / "missing.json"), None,
                        {"stats": {"links_armed": ">0"}, "hits": {"variant 0 C'": ">0"}})
    assert r.verdict == "not_run"
    with pytest.raises(ValueError):
        O.o3_nonvacuity(st, hits, {"stats": {"links_armed": "positive"}})


def test_o4_o5_o6():
    p = O.OracleResult("O1", "pass")
    f = O.OracleResult("O2b", "fail", "systick[3].pc",
                       {"differences": 1, "reference_problems": []})
    runs = {"artifact_run": _run(), "reference_run": _run()}
    assert O.o4_detected([p, f], **runs).verdict == "pass"
    assert O.o4_detected([p, O.OracleResult("O2a", "pass")], **runs).verdict == "fail"
    assert O.o4_detected([O.OracleResult("O2a", "not_run")], **runs).verdict == "not_run"
    st = read_v6_stats(DATA / "stats" / "guarded.json")
    r = O.o5_fallback(_run(), st)
    assert r.verdict == "reported" and r.data["links_armed"] == 7 and r.data["appended"] == 0
    r = O.o5_fallback(_run(proof={"verdict": "INVALID_artifact_not_used",
                                  "failed": ["v6_header"]}), None)
    assert r.verdict == "fail" and "v6_header" in r.detail
    bad = dict(st, invariant_violations=1)
    assert O.o5_fallback(_run(), bad).verdict == "fail"
    ring = {"index": 300, "buffer_sha256": "sha256:" + "1" * 64}
    hits = {"targets": [{"what": "variant 0 C'", "hits": 12}]}
    ok = O.o6_attribution({"ring": ring}, {"ring": dict(ring)}, hits, "variant 0 C'")
    assert ok.verdict == "pass"
    r = O.o6_attribution({"ring": ring}, {"ring": dict(ring, index=301)}, hits, "variant 0 C'")
    assert r.verdict == "fail"
    r = O.o6_attribution({"ring": ring}, {"ring": ring},
                         {"targets": [{"what": "variant 0 C'", "hits": 0}]}, "variant 0 C'")
    assert r.verdict == "fail" and "did not run" in r.detail
    assert O.o6_attribution(None, {"ring": ring}).verdict == "not_run"
    # coverage off: no ring buffer on either side -> unknown, not equal and not a failure
    none = {"index": 0, "consumed": 0, "buffer_sha256": None}
    assert O.o6_attribution({"ring": none}, {"ring": dict(none)}).verdict == "not_run"
    assert O.o6_attribution({"ring": ring}, {"ring": none}).verdict == "not_run"
    r = O.o6_attribution({"files": {"bbl.txt": "sha256:1"}}, {"files": {"bbl.txt": "sha256:2"}})
    assert r.verdict == "fail" and "bbl.txt" in r.detail
    rows = O.oracle_rows([f, p], fixture="F-B3-7", arm="spec")
    assert [x["oracle"] for x in rows] == ["O1", "O2b"] and rows[0]["fixture"] == "F-B3-7"
    with pytest.raises(ValueError):
        O.OracleResult("O7", "pass")


def test_coverage_files(tmp_path):
    (tmp_path / "virtuals").mkdir()
    (tmp_path / "bbl.txt").write_text("0x10\n")
    (tmp_path / "io.log").write_text("x")
    assert list(O.coverage_files(tmp_path)) == ["bbl.txt"]
    assert O.coverage_files(tmp_path / "missing") == {}


# ---------------------------------------------------------------------------
# diag: stats, hit counters, twin parser
# ---------------------------------------------------------------------------

def test_read_v6_stats_recorded_and_missing(tmp_path):
    g = read_v6_stats(DATA / "stats" / "guarded.json")
    assert g["present"] and not g["problems"] and g["links_armed"] == 7 and g["file_version"] == 6
    v5 = read_v6_stats(DATA / "stats" / "v5-control.json")
    assert v5["present"] and v5["loaded"] is False and v5["file_version"] == 5
    m = read_v6_stats(tmp_path / "none.json")
    assert m["present"] is False and m["problems"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(dict(json.loads((DATA / "stats" / "guarded.json").read_text()),
                                   links_armed=-1, schema="x")))
    b = read_v6_stats(bad)
    assert any("schema" in x for x in b["problems"]) and any("links_armed" in x
                                                            for x in b["problems"])
    assert stats_env(tmp_path / "s.json") == {"QEMU_SC_V6_STATS": str(tmp_path / "s.json")}
    from scopt.run.diag import DiagError
    with pytest.raises(DiagError):
        stats_env("rel/s.json")


INFO_BP = """Num     Type           Disp Enb Address            What
2       hw breakpoint  keep y   0x00007fff6c0012c0
\tbreakpoint already hit 990 times
\tignore next 999999010 hits
3       hw breakpoint  keep y   0x00007fff6c001400
\tignore next 1000000000 hits
4       hw breakpoint  keep y   0x00007fff6c001500
\tbreakpoint already hit 1 time
[Inferior 1 (process 7) exited normally]
"""


def test_parse_hits():
    h = parse_hits(INFO_BP)
    assert h == {0x7fff6c0012c0: 990, 0x7fff6c001400: 0, 0x7fff6c001500: 1}


def test_parse_twin():
    n = len(FLUSH_FUNCS)
    rows = ["1       breakpoint     keep y   0x0000555555 in benchmark_start",
            "2       breakpoint     keep y   0x0000555556 in benchmark_end"]
    for k in range(3, 3 + n):
        rows.append(f"{k}       breakpoint     keep y   0x00005555{k:02x} in f{k}")
        if k == 5:
            rows.append("\tbreakpoint already hit 4 times")
    text = "\n".join(["SCOPT benchmark_start bp_head=0x0 wp_head=0x0 singlestep=0",
                      "SCOPT benchmark_end bp_head=0x0 wp_head=(nil) singlestep=0",
                      "SCOPT info-breakpoints-begin", *rows, "SCOPT info-breakpoints-end",
                      "SCOPT cov index=300 consumed=290 buffer=0x7f00"])
    t = parse_twin(text)
    assert t["counters_total"] == 4 and t["counters"][FLUSH_FUNCS[2]] == 4
    assert t["counters_resolved"] == n and t["unresolved"] == []
    # a pending breakpoint (or a missing row) is unresolved: its calls are unknown
    pend = text.replace("keep y   0x0000555507 in f7", "keep y   <PENDING>  f7")
    pend = "\n".join(x for x in pend.splitlines() if not x.startswith("9 "))
    t = parse_twin(pend)
    assert t["unresolved"] == [FLUSH_FUNCS[4], FLUSH_FUNCS[6]]
    assert t["counters_resolved"] == n - 2
    assert t["cov"] == {"index": 300, "consumed": 290, "buffer": "0x7f00"}
    assert t["start"]["bp_head"] == "0x0" and t["end"]["wp_head"] == "(nil)"


# ---------------------------------------------------------------------------
# synthetic v6 artifact (minimal snapshot; WP18 helpers)
# ---------------------------------------------------------------------------

def _v6_link(monkeypatch):
    from scopt.link import B3Plan
    from tests.synth import b3 as H

    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:1]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs[:2])
    plan = B3Plan(grow=grow, variants=tuple(vs))
    ri6 = H.v6_identity(monkeypatch)
    return s, plan, ri6, H


@pytest.fixture
def v6_store(tmp_path, monkeypatch):
    """A published structural v6 artifact (SPEC slots + variants) of minimal."""
    from scopt.validate import package_b3

    s, plan, ri6, H = _v6_link(monkeypatch)
    r = package_b3(s, plan, tmp_path / "store", candidate_id="sha256:" + "61" * 32,
                   runtime=ri6, v6_exes=[H.QINT_V6_SHA], allow_uncertified=True)
    assert r.status == "published", r.reason
    return s, plan, ri6, H, Path(r.artifact_dir)


@pytest.mark.real
def test_artifact_probe_addrs(v6_store):
    from scopt.formats.sc_v6 import read_sc6

    *_, art = v6_store
    rows = artifact_probe_addrs(art / "cache.sc")
    f = read_sc6(art / "cache.sc")
    base = f.sc.header.code_address
    kinds = [r["kind"] for r in rows]
    assert kinds.count("variant") == len(f.ext.variants) == 2
    assert kinds.count("stub") == kinds.count("fallback") == len(f.ext.slots)
    v0 = next(r for r in rows if r["kind"] == "variant")
    assert v0["addr"] == base + f.ext.variants[0].tc_offset
    for r in rows:
        if r["kind"] == "fallback":
            assert r["how"] == "decoded"
            j = r["slot"]
            s = f.ext.slots[j]
            stub = next(x for x in rows if x["kind"] == "stub" and x["slot"] == j)
            assert stub["addr"] < r["addr"] < stub["addr"] + s.guard_len
            # F is the original lookup tail (mov rdi, rbp; call [rip+..]; jmp rax)
            i = next(i for i in f.sc.valid_slots
                     if f.sc.records[i].tb_offset == s.src_tb_offset)
            off = r["addr"] - (base + f.sc.records[i].tc_offset)
            assert f.sc.bodies[i][off:off + 3] == bytes.fromhex("488bfd")


# ---------------------------------------------------------------------------
# post-run proof for v6
# ---------------------------------------------------------------------------

def _copy_sc(src: Path, dst: Path) -> Path:
    from scopt.formats.sc_v5 import sparse_copy

    dst.parent.mkdir(parents=True, exist_ok=True)
    sparse_copy(src, dst)
    os.chmod(dst, 0o600)
    return dst


@pytest.mark.real
def test_proof_v6_pass_old_loader_and_ext(v6_store, tmp_path):
    from scopt.formats.sc_v5 import ScHeader
    from scopt.run.proof import VERDICT_INVALID, VERDICT_PASS, deployed_state, prove

    *_, art = v6_store
    sc = _copy_sc(art / "cache.sc", tmp_path / "rd" / "cache" / "c.sc")
    man = sc.parent / "c.sc.fastdyn.json"
    man.write_text(json.dumps({"version": 1, "fingerprint": "ab" * 32}))
    st = deployed_state(sc, man)
    assert st["file_version"] == 6 and st["ext_sha256"].startswith("sha256:")
    side = json.loads((art / "cache.sc.scopt.json").read_text())
    assert st["ext_artifact_id8"] == side["x_ext_artifact_id8"]
    r = prove(st)
    assert r["verdict"] == VERDICT_PASS and r["file_version"] == 6
    assert {c["check"] for c in r["checks"]} >= {"v6_header", "ext"}
    # an ext byte changed during the run
    fd = os.open(sc, os.O_RDWR)
    b = os.pread(fd, 1, 0x100 + 70)
    os.pwrite(fd, bytes([b[0] ^ 1]), 0x100 + 70)
    os.close(fd)
    r = prove(st)
    assert r["verdict"] == VERDICT_INVALID and r["failed"] == ["ext"]
    os.pwrite(fd := os.open(sc, os.O_RDWR), b, 0x100 + 70)
    os.close(fd)
    assert prove(st)["verdict"] == VERDICT_PASS
    # the old (v5-only) loader: valid_header fails on version 6 -> fresh v5 header (F-B3-18)
    with open(sc, "rb") as f:
        h = ScHeader.from_bytes(f.read(80))
    fd = os.open(sc, os.O_RDWR)
    os.pwrite(fd, h.replace(version=5, reserved=0, record_count=0).with_checksum().to_bytes(), 0)
    os.close(fd)
    r = prove(st)
    # (the record bytes are still there; header and v6_header name the rewrite)
    assert r["verdict"] == VERDICT_INVALID and r["failed"] == ["header", "v6_header"]
    assert "fresh v5 header" in next(c["detail"] for c in r["checks"]
                                     if c["check"] == "v6_header")
    assert r["appended"] is None


@pytest.mark.real
def test_proof_direct_frontend_has_no_manifest(v6_store, tmp_path):
    from scopt.run.proof import VERDICT_PASS, deployed_state, prove

    *_, art = v6_store
    sc = _copy_sc(art / "cache.sc", tmp_path / "d" / "c.sc")
    st = deployed_state(sc, None, frontend="none")
    r = prove(st)
    assert r["verdict"] == VERDICT_PASS
    row = next(c for c in r["checks"] if c["check"] == "manifest")
    assert row["ok"] and "direct QEMU run" in row["detail"]
    # a FastDyn deploy without a manifest still fails
    assert prove(deployed_state(sc, None))["failed"] == ["manifest"]
    with pytest.raises(ValueError):
        deployed_state(sc, None, frontend="qemu")


# ---------------------------------------------------------------------------
# negative controls (acceptance: refused by scopt publish)
# ---------------------------------------------------------------------------

def _flip_variant_byte(link):
    v = link.variants[0]
    body = bytearray(v.body)
    body[1] ^= 0x10
    return replace(link, variants=(replace(v, body=bytes(body)),) + link.variants[1:])


@pytest.fixture
def negative(tmp_path, monkeypatch):
    from scopt.run.negative import package_negative

    s, plan, ri6, H = _v6_link(monkeypatch)
    r = package_negative(s, plan, tmp_path / "neg", label="v16-byte", mutant="undeclared "
                         "variant byte", detector="F-B3-1 O1", mutate=_flip_variant_byte,
                         runtime=ri6, v6_exes=[H.QINT_V6_SHA])
    return s, plan, ri6, H, r


@pytest.mark.real
def test_negative_control_packaged_and_tagged(negative):
    from scopt.formats.sc_v6 import read_sc6
    from scopt.run.policy import NEGATIVE_MARKER, artifact_tags

    *_, r = negative
    assert r.packaged, (r.reason, r.blocking[:2])
    d = Path(r.path)
    assert {x["code"] for x in r.bypassed} == {"V16"}
    side = json.loads((d / "cache.sc.scopt.json").read_text())
    tag = side["x_negative_control"]
    assert tag["bypassed_failures"] == ["V16"] and tag["statically_rejected"]
    assert tag["byte_mutation"] and tag["reimport"] == "skipped"
    assert (d / NEGATIVE_MARKER).is_file() and artifact_tags(d)["negative_control"]
    # resealed: the loader would load it (no ext problem), and the sidecar id matches
    f = read_sc6(d / "cache.sc")
    assert f.loadable and f.problem is None
    assert side["x_ext_artifact_id8"] == bytes(f.ext.artifact_id8).hex()
    val = json.loads((d / "validation.json").read_text())
    assert val["verdict"] == "rejected"  # the static rejection is recorded, not hidden
    led = (d / "ledger.jsonl").read_text()
    assert '"negative_control"' in led and '"pass"' not in led
    assert (d / "MANIFEST.sha256").is_file() and r.artifact_id.startswith("sha256:")


@pytest.mark.real
def test_negative_control_refused_by_scopt_publish(negative, v6_store, tmp_path):
    """Acceptance: a negative-control artifact is refused by `scopt publish`; a
    normal artifact is published by the same command (control)."""
    from scopt.cli import cli, load_commands
    from scopt.formats.artifact import ArtifactError, publish

    *_, r = negative
    load_commands()
    runner = CliRunner()
    store = tmp_path / "pubstore"
    res = runner.invoke(cli, ["publish", r.path, "--store", str(store)])
    assert res.exit_code == 2, res.output
    assert "publish refused: negative_control" in res.output
    assert not (store / "artifacts").exists() or \
        not [p for p in (store / "artifacts").iterdir() if not p.name.startswith(".tmp")]
    # the marker alone suffices (a sidecar that lost its tag)
    d2 = tmp_path / "copy"
    shutil.copytree(r.path, d2)
    side = json.loads((d2 / "cache.sc.scopt.json").read_text())
    side.pop("x_negative_control")
    (d2 / "cache.sc.scopt.json").write_text(json.dumps(side))
    res = runner.invoke(cli, ["publish", str(d2), "--store", str(store)])
    assert res.exit_code == 2 and "negative_control" in res.output
    # the publish primitive itself refuses a tagged staging directory
    with pytest.raises(ArtifactError) as e:
        publish(d2, store)
    assert e.value.code == "negative_control"
    # control: an identity v5 artifact (no edits, nothing uncertified) is publishable
    from scopt.link import B3Plan
    from scopt.validate import package_b3

    s, _plan, ri6, H, _art = v6_store
    ok = package_b3(s, B3Plan(), tmp_path / "s-id", candidate_id="sha256:" + "62" * 32)
    assert ok.status == "published", ok.reason
    res = runner.invoke(cli, ["publish", ok.artifact_dir, "--store", str(store), "--json"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["artifact_id"] == ok.artifact_id and doc["status"] == "created"
    # the structural v6 artifact carries x_uncertified: refused (fail closed)
    res = runner.invoke(cli, ["publish", str(_art), "--store", str(store)])
    assert res.exit_code == 2 and "certification_not_run" in res.output


@pytest.mark.real
def test_negative_control_refusals_elsewhere(negative, tmp_path):
    from scopt.formats.sc_v5 import ScHeader
    from scopt.run.deploy import artifact_refusals, resolve_artifact
    from scopt.run.negative import package_negative

    s, plan, ri6, H, r = negative
    art = resolve_artifact(r.path)
    with open(art["sc"], "rb") as f:
        h = ScHeader.from_bytes(f.read(80))
    reasons, _ = artifact_refusals(art, h)
    assert reasons == ["negative_control_artifact"]
    assert artifact_refusals(art, h, allow_negative_control=True)[0] == []
    # without the bypass the same mutant is refused by the packager (V16 is static)
    r2 = package_negative(s, plan, tmp_path / "neg2", label="nobypass",
                          mutate=_flip_variant_byte, runtime=ri6, v6_exes=[H.QINT_V6_SHA],
                          bypass=())
    assert r2.status == "refused" and r2.reason == "unbypassed:V16"
    assert not (tmp_path / "neg2" / "nobypass").exists()
    # never into a store's artifacts/ directory, never over an existing directory
    with pytest.raises(ValueError):
        package_negative(s, plan, tmp_path / "store" / "artifacts", label="x",
                         runtime=ri6, v6_exes=[H.QINT_V6_SHA])
    with pytest.raises(FileExistsError):
        package_negative(s, plan, Path(r.path).parent, label="v16-byte",
                         runtime=ri6, v6_exes=[H.QINT_V6_SHA])


# ---------------------------------------------------------------------------
# deploy refusal codes (F-B3-16 (a), (e)), test policies, v6 consistency
# ---------------------------------------------------------------------------

STALE = DATA / "stale"


# The recorded manifests name fixtures/b3/b3.elf by path, size and mtime; WP24
# rebuilds that file. The firmware they were recorded with is kept here
# (stale/b3.elf, from runs/wp24-fix/fixture-before-fix/, size and mtime equal to
# the manifests' entry) and materialized with the recorded mtime, so these
# tests do not depend on the current state of fixtures/b3/.
_STALE_FW: dict[str, str] = {}


def _stale_firmware() -> str:
    import tempfile

    if "path" not in _STALE_FW:
        ent = json.loads((STALE / "b3-1.sc.fastdyn.json").read_text())[
            "inputs"]["native_files"]["firmware"]
        import atexit

        d = Path(tempfile.mkdtemp(prefix="wp26-stale-fw-"))
        atexit.register(shutil.rmtree, d, True)
        dst = d / "b3.elf"
        shutil.copyfile(STALE / "b3.elf", dst)
        os.utime(dst, ns=(ent["mtime_ns"], ent["mtime_ns"]))
        assert dst.stat().st_size == ent["size"]
        _STALE_FW["path"] = str(dst)
    return _STALE_FW["path"]


def _stale_manifest(name):
    man = json.loads((STALE / f"b3-{name}.sc.fastdyn.json").read_text())
    man["inputs"]["native_files"]["firmware"]["path"] = _stale_firmware()
    return man


def _bundle(name):
    from scopt.formats.bundle_v1 import BundleStale, identity_bundle

    try:
        return identity_bundle(STALE / f"b3-{name}.toml", _stale_manifest(name))
    except BundleStale as e:  # the FastDyn plugin changed (src/fastdyn is never modified)
        pytest.skip(f"stale fixture manifest: {e}")
    except OSError as e:
        pytest.skip(f"fixture files not present: {e}")


def _ri(name):
    from scopt.run.toml_rewrite import normalized_config_sha256

    return {"normalized_config_sha256": normalized_config_sha256(
        (STALE / f"b3-{name}.toml").read_text())}


@pytest.mark.real
@pytest.mark.parametrize("case,fields", [
    ("16a", ["instrumentation_hash", "patch_bindings_hash", "patch_plan_hash"]),
    ("16e", ["contract_id", "instrumentation_hash", "patch_bindings_hash", "patch_plan_hash"]),
])
def test_f_b3_16_refusal_codes_pinned(case, fields):
    """Deploying the F-B3-1 artifact under the F-B3-16(a) (patch `r0 <- 2`) or
    F-B3-16(e) (coverage = true) configuration is refused with
    ``bundle_mismatch`` first, then ``config_mismatch`` (deploy raises the
    first). (a)'s fields are the patch plan's; (e)'s include ``contract_id``
    (coverage). The plan hash also differs in (e) because every fixture config
    prints its own BENCH name through a virtual."""
    from scopt.run.deploy import identity_reasons

    want_b, want_ri = _bundle("1"), _ri("1")
    reasons, detail = identity_reasons(want_b, _bundle(case), want_ri, _ri(case))
    assert reasons == ["bundle_mismatch", "config_mismatch"]
    assert detail["bundle_fields"] == fields
    assert identity_reasons(want_b, _bundle("1"), want_ri, _ri("1"))[0] == []
    # a bare .sc has no identity documents: nothing to compare here
    assert identity_reasons(None, _bundle(case), None, _ri(case))[0] == []


@pytest.mark.real
def test_f_b3_16_single_field_changes():
    """Isolated: only the patch plan, or only coverage, differs from F-B3-1."""
    import copy

    from scopt.formats.bundle_v1 import identity_bundle
    from scopt.run.deploy import identity_reasons

    base = _bundle("1")
    man = _stale_manifest("1")
    m_a = copy.deepcopy(man)
    m_a["inputs"]["content_files"]["modifiers"]["sha256"] = "0" * 64
    rs, det = identity_reasons(base, identity_bundle(STALE / "b3-1.toml", m_a), None, None)
    assert rs == ["bundle_mismatch"] and det["bundle_fields"] == [
        "instrumentation_hash", "patch_bindings_hash", "patch_plan_hash"]
    m_e = copy.deepcopy(man)
    po = m_e["inputs"]["settings"]["plugin_options"]
    m_e["inputs"]["settings"]["plugin_options"] = [
        "coverage=1" if x == "coverage=0" else "edge_coverage=1" if x == "edge_coverage=0"
        else x for x in po]
    assert m_e["inputs"]["settings"]["plugin_options"] != po
    rs, det = identity_reasons(base, identity_bundle(STALE / "b3-1.toml", m_e), None, None)
    assert rs == ["bundle_mismatch"] and det["bundle_fields"] == ["contract_id"]


def _sc6_artifact_dir(art: Path, dst: Path) -> Path:
    shutil.copytree(art, dst)
    for p in [dst, *dst.rglob("*")]:
        os.chmod(p, (stat.S_IRWXU if p.is_dir() else 0o600))
    return dst


@pytest.mark.real
def test_artifact_refusals_v6_consistency_and_policies(v6_store, tmp_path):
    from scopt.formats.sc_v5 import ScHeader
    from scopt.run.deploy import artifact_refusals, resolve_artifact
    from scopt.run.policy import tag_sidecar

    *_, art0 = v6_store
    d = _sc6_artifact_dir(art0, tmp_path / "a")

    def refs(**kw):
        a = resolve_artifact(d)
        with open(a["sc"], "rb") as f:
            h = ScHeader.from_bytes(f.read(80))
        return artifact_refusals(a, h, **kw)

    rs, info = refs()
    assert rs == [] and info["file_version"] == 6 and info["ext_artifact_id8"] == \
        info["sidecar_ext_artifact_id8"]
    sp = d / "cache.sc.scopt.json"
    side = json.loads(sp.read_text())
    sp.write_text(json.dumps(dict(side, x_ext_artifact_id8="00" * 8)))
    assert refs()[0] == ["ext_artifact_id_mismatch"]
    sp.write_text(json.dumps({k: v for k, v in side.items() if k != "x_ext_artifact_id8"}))
    assert refs()[0] == ["ext_artifact_id_missing"]
    sp.write_text(json.dumps(dict(side, loader_requirement="qemu-sc-v5-unmodified")))
    assert refs()[0] == ["loader_requirement_mismatch"]
    # test policy allow_ram_resident (F-B3-15/17)
    sp.write_text(json.dumps(tag_sidecar(dict(side), ["allow_ram_resident"], ["ram_resident"])))
    assert refs()[0] == ["test_only_artifact:allow_ram_resident"]
    assert refs(test_policies=["allow_ram_resident"])[0] == []
    sp.write_text(json.dumps(dict(side, x_test_only={"policies": ["allow_everything"]})))
    assert refs(test_policies=["allow_everything"])[0] == ["unknown_test_policy:allow_everything"]
    sp.write_text(json.dumps(dict(side, x_test_only=True)))
    assert refs()[0] == ["unknown_test_policy:<malformed>"]


def test_policy_waive_and_tag():
    from scopt.run.policy import publish_refusal, tag_sidecar, waive

    kept, w = waive([("ram_resident", ""), ("no_meta", "x"), "ram_resident"],
                    ["allow_ram_resident"])
    assert kept == [("no_meta", "x")] and w == ["ram_resident"]
    assert waive(["ram_resident"], [])[0] == ["ram_resident"]
    with pytest.raises(ValueError):
        waive([], ["allow_x"])
    assert tag_sidecar({}, []) == {}
    with pytest.raises(ValueError):
        tag_sidecar({}, ["allow_x"])
    assert publish_refusal("/nonexistent") is None


def test_deploy_refuses_test_only_before_the_oracle(tmp_path, monkeypatch):
    """deploy() refuses a test-policy artifact before running the FastDyn shim and
    records the refusal in deploy.json (so launch refuses the run dir)."""
    from types import SimpleNamespace

    from scopt.formats.sc_v5 import ScHeader
    import scopt.run.deploy as D
    from scopt.run.proof import deploy_refusal

    art = tmp_path / "art"
    art.mkdir()
    h = ScHeader(b"QSCACHE1", 5, 80, 1 << 20, 0x7fff6c000000, 0, 0, 0, 4096, 10, 152, 0, 0)
    (art / "cache.sc").write_bytes(h.with_checksum().to_bytes())
    (art / "cache.sc.scopt.json").write_text(json.dumps(
        {"loader_requirement": "qemu-sc-v5-unmodified",
         "x_test_only": {"policies": ["allow_ram_resident"]}}))
    root = tmp_path / "rd"
    root.mkdir()
    rd = SimpleNamespace(root=root, cache=root / "cache" / "x.sc")
    monkeypatch.setattr(D, "oracle", lambda *a, **k: pytest.fail("oracle must not run"))
    with pytest.raises(D.DeployError) as e:
        D.deploy(art, rd)
    assert e.value.code == "test_only_artifact:allow_ram_resident"
    assert "test_only_artifact" in deploy_refusal(rd)


# ---------------------------------------------------------------------------
# direct (non-FastDyn) deploy and launch
# ---------------------------------------------------------------------------

FAKE_QEMU = """#!/bin/sh
# stand-in for qemu-system-arm in the direct-path unit test: prints like the
# alias image and leaves the cache untouched
for a in "$@"; do case "$a" in tcg,supercache=*) test -f "${a#tcg,supercache=}" || exit 3;; esac; done
echo "alias z=000007d0"
echo "alias nz=00000000"
"""


def _small_sc(path: Path) -> Path:
    from scopt.formats.sc_v5 import ScHeader

    h = ScHeader(b"QSCACHE1", 5, 80, 1 << 16, 0x7fff6c000000, 0, 0, 0, 4096, 10, 152, 0, 0)
    path.write_bytes(h.with_checksum().to_bytes())
    return path


def test_direct_deploy_and_launch(tmp_path):
    from scopt.run.deploy import DeployError, deploy_direct, launch_direct

    q = tmp_path / "qemu-system-arm"
    q.write_text(FAKE_QEMU)
    q.chmod(0o755)
    sc = _small_sc(tmp_path / "alias.sc")
    argv = ["@QEMU@", "-M", "mps2-an385", "-accel", "tcg,supercache=@SUPERCACHE@"]
    doc = deploy_direct(sc, tmp_path / "rd", argv=argv, qemu=q, cache_name="alias.sc")
    assert doc["method"] == "direct" and doc["identity"]["checked"] is False
    assert doc["state"]["frontend"] == "none"
    d = json.loads((tmp_path / "rd" / "direct.json").read_text())
    assert d["argv"][-1] == f"tcg,supercache={tmp_path / 'rd' / 'cache' / 'alias.sc'}"
    run = launch_direct(tmp_path / "rd", timeout_s=20)
    assert run["verdict"] == "PASS", run["proof"]
    assert run["guest_norm"] == ["alias z=000007d0", "alias nz=00000000", "exit_code=0"]
    from scopt.run.launcher import RunDirError
    with pytest.raises(RunDirError):
        launch_direct(tmp_path / "rd")  # used
    with pytest.raises(DeployError) as e:
        deploy_direct(sc, tmp_path / "rd", argv=argv, qemu=q)
    assert e.value.code == "run_dir_not_fresh"
    with pytest.raises(DeployError) as e:
        deploy_direct(sc, tmp_path / "rd2", argv=["@QEMU@", "-M", "x"], qemu=q)
    assert e.value.code == "direct_argv_missing"
    with pytest.raises(DeployError) as e:
        deploy_direct(sc, tmp_path / "rd3", argv=argv, qemu=None)
    assert e.value.code == "direct_qemu_missing"
    # an artifact dir whose RuntimeIdentity names another QEMU
    art = tmp_path / "art"
    art.mkdir()
    shutil.copyfile(sc, art / "cache.sc")
    (art / "runtime_identity.json").write_text(json.dumps({"qemu_sha256": "sha256:" + "0" * 64}))
    with pytest.raises(DeployError) as e:
        deploy_direct(art, tmp_path / "rd4", argv=argv, qemu=q)
    assert e.value.code in ("runtime_mismatch:qemu_sha256", "bundle_schema",
                            "unknown_schema_version", "runtime_identity_id_mismatch")


# ---------------------------------------------------------------------------
# arms (§10.1, §9.4)
# ---------------------------------------------------------------------------

def test_arm_tables():
    from scopt.run.arms import B3B4_ARMS, FIXTURE_ARMS, ablation_arms, b3b4_arm_specs

    assert list(B3B4_ARMS) == ["nocache", "empty", "warm", "identity", "identity-v6", "layout",
                               "b3-chain", "b3-static", "b3-spec", "b4", "semantic",
                               "combined"]
    assert list(FIXTURE_ARMS) == ["warm", "identity-v6", "chain", "spec", "b4", "combined"]
    assert {a.loader for a in B3B4_ARMS.values() if a.name in ("b4", "warm", "layout")} == {"v5"}
    assert B3B4_ARMS["semantic"].loader == "v6" and "eliminate-state-sync" in \
        B3B4_ARMS["semantic"].passes
    assert set(B3B4_ARMS["combined"].passes) == set(B3B4_ARMS["semantic"].passes) | {"b2-layout"}
    names = [a.name for a in ablation_arms()]
    assert "semantic-no-branch-fold" in names and "b4-D2" in names
    specs, not_run = b3b4_arm_specs({"b3-spec": "/x", "semantic-no-branch-fold": "/y"})
    by = {s.name: s for s in specs}
    assert by["warm"].source == "auto" and by["warm"].role == "reference"
    assert by["nocache"].kind == "nocache" and by["b3-spec"].source == "/x"
    assert by["semantic-no-branch-fold"].parent == "semantic" and \
        by["semantic-no-branch-fold"].ablated_pass == "branch-fold"
    assert "semantic" in not_run and "b3-chain" in not_run
    from scopt.run.arms import CampaignError
    with pytest.raises(CampaignError):
        b3b4_arm_specs({"bogus": "/z"})


def test_campaign_semantic_arm_supported_with_artifact(tmp_path):
    from scopt.run.arms import campaign_from_args

    c = campaign_from_args(base_config="/b.toml", arms=["warm", "semantic"], out_name="t",
                           artifacts={"semantic": "/s"})
    assert "semantic" not in c.unsupported and "combined" in c.unsupported
    assert c.arm("semantic").source == "/s"


@pytest.mark.real
def test_preflight_refuses_negative_and_requires_v6_loader(negative, v6_store, tmp_path,
                                                          monkeypatch):
    from scopt.run.arms import CampaignError, campaign_from_args, preflight

    import re

    src = STALE / "b3-1.toml"
    base = tmp_path / "base.toml"
    # the fixture template pins [Machine].qemu_path (qemu_path_conflict otherwise)
    base.write_text(re.sub(r"(?m)^qemu_path\s*=.*$", "", src.read_text()))
    *_, r = negative
    *_, art = v6_store
    monkeypatch.delenv("SCOPT_V6_EXES", raising=False)
    import scopt.validate as V
    monkeypatch.setattr(V, "V6_GATE_BINARY_JSON", tmp_path / "none.json")
    c = campaign_from_args(base_config=str(base), arms=["warm", "neg"], out_name="t",
                           warm=str(art), artifacts={"neg": r.path})
    with pytest.raises(CampaignError) as e:
        preflight(c, tmp_path / "o1")
    assert e.value.code == "negative_control_arm"
    c = campaign_from_args(base_config=str(base), arms=["warm"], out_name="t", warm=str(art))
    with pytest.raises(CampaignError) as e:
        preflight(c, tmp_path / "o2")
    assert e.value.code == "v6_arm_needs_qemu"
    q = tmp_path / "qemu"
    q.write_bytes(b"not a v6 binary")
    c.qemu = str(q)
    with pytest.raises(CampaignError) as e:
        preflight(c, tmp_path / "o3")
    assert e.value.code == "v6_loader_required"
    import hashlib
    monkeypatch.setenv("SCOPT_V6_EXES", hashlib.sha256(q.read_bytes()).hexdigest())
    ids = preflight(c, tmp_path / "o4")
    assert ids["v6_arms"] == ["warm"] and ids["arms"]["warm"]["file_version"] == 6


FAKE_CAPTURE_QEMU = """#!/bin/sh
# writes the cache it is given and the capture file named by QEMU_SC_CAPTURE
for a in "$@"; do case "$a" in tcg,supercache=*) printf 'QSCACHE1' > "${a#tcg,supercache=}";; esac; done
test -n "$QEMU_SC_CAPTURE" && printf 'SCM' > "$QEMU_SC_CAPTURE"
env | grep -c '^QEMU_SC_' > "$(dirname "$QEMU_SC_CAPTURE")/n_sc_vars"
echo "alias z=000007d0"
"""


def test_prepare_direct_plumbing(tmp_path, monkeypatch):
    from scopt.run.deploy import DeployError, prepare_direct

    q = tmp_path / "qemu-system-arm"
    q.write_text(FAKE_CAPTURE_QEMU)
    q.chmod(0o755)
    monkeypatch.setenv("QEMU_SC_PROFILE", "/tmp/should-be-scrubbed")
    argv = ["@QEMU@", "-M", "mps2-an385", "-accel", "tcg,supercache=@SUPERCACHE@"]
    doc = prepare_direct(tmp_path / "prep", argv=argv, qemu=q, cache_name="alias.sc")
    assert doc["verdict"] == "PASS" and doc["exit_code"] == 0
    assert Path(doc["capture"]).read_text() == "SCM"
    assert (tmp_path / "prep" / "n_sc_vars").read_text().strip() == "1"  # only the capture var
    with pytest.raises(DeployError) as e:
        prepare_direct(tmp_path / "prep", argv=argv, qemu=q)
    assert e.value.code == "run_dir_not_fresh"


def test_proof_v6_checks_need_the_ext_table_bit(tmp_path):
    """A version-6 header with EXT_TABLE clear is a v5 file with its version
    field changed (B1-9): the proof keeps the v5 check list for it."""
    from scopt.formats.sc_v5 import ScHeader
    from scopt.run.proof import deployed_state

    h = ScHeader(b"QSCACHE1", 6, 80, 1 << 16, 0x7fff6c000000, 0, 0, 0, 4096, 10, 152, 0, 0)
    p = tmp_path / "a.sc"
    p.write_bytes(h.with_checksum().to_bytes())
    st = deployed_state(p, None)
    assert "ext_sha256" not in st and st["file_version"] == 6
    p.write_bytes(h.replace(reserved=1).with_checksum().to_bytes())
    st = deployed_state(p, None)
    assert st["ext_sha256"].startswith("sha256:") and st["ext_artifact_id8"] == "00" * 8


# ---------------------------------------------------------------------------
# WP26 review fixes: `scopt publish` verifies its source; dynamic_oracles never
# passes with requested oracles not run; bare .sc tags; O3 crashed gdb runs;
# O2b end counters; icount_mode
# ---------------------------------------------------------------------------

def _writable_copy(src, dst):
    shutil.copytree(src, dst)
    for p in [Path(dst), *Path(dst).rglob("*")]:
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    return Path(dst)


def _flip(path, off):
    with open(path, "r+b") as f:
        f.seek(off)
        b = f.read(1)
        f.seek(off)
        f.write(bytes([b[0] ^ 0xFF]))


def _reseal_manifest(d):
    from scopt.formats import artifact as A

    (d / A.MANIFEST).write_bytes(A.manifest_bytes(d))


def _rewrite_validation(d, edit):
    """Edit validation.json, re-point the sidecar's validation_sha256 at it and
    reseal MANIFEST.sha256 (a consistent but lying source)."""
    from scopt.canon import sha256_id
    from scopt.formats import artifact as A

    val = json.loads((d / A.VALIDATION).read_text())
    edit(val)
    vb = A.write_json_file(d / A.VALIDATION, val)
    side = json.loads((d / A.SIDECAR).read_text())
    side["validation_sha256"] = sha256_id(vb)
    A.write_json_file(d / A.SIDECAR, side)
    _reseal_manifest(d)


@pytest.mark.real
def test_scopt_publish_verifies_the_source(v6_store, tmp_path):
    """Blocking review issue 1: `scopt publish` refuses a source whose cache.sc
    changed after validation (manifest, then SnapshotId), whose validation.json
    the sidecar does not name, or whose validation rejected it; nothing reaches
    the store. Control: an untouched copy publishes under the source's id."""
    from scopt.cli import cli, load_commands
    from scopt.commands.run_b3 import publish_directory
    from scopt.formats import artifact as A
    from scopt.formats.sc_v5 import read_sc
    from scopt.link import B3Plan
    from scopt.validate import package_b3

    s, *_ = v6_store
    ok = package_b3(s, B3Plan(), tmp_path / "s-id", candidate_id="sha256:" + "63" * 32)
    assert ok.status == "published", ok.reason
    sc = read_sc(Path(ok.artifact_dir) / "cache.sc")
    body_off = 0x10000 + sc.records[sc.valid_slots[0]].tc_offset + 2
    store = tmp_path / "pub"
    load_commands()
    runner = CliRunner()

    def published():
        d = store / "artifacts"
        return sorted(p.name for p in d.iterdir() if not p.name.startswith(".tmp")) \
            if d.exists() else []

    def refused(d, code):
        with pytest.raises(A.ArtifactError) as e:
            publish_directory(str(d), str(store))
        assert e.value.code == code, (e.value.code, e.value.detail)
        res = runner.invoke(cli, ["publish", str(d), "--store", str(store)])
        assert res.exit_code == 2 and f"publish refused: {code}" in res.output, res.output
        assert published() == []

    # (1) the reviewer's case: one cache.sc byte flipped after validation
    d1 = _writable_copy(ok.artifact_dir, tmp_path / "c1")
    _flip(d1 / "cache.sc", body_off)
    assert A.verify_manifest(d1) == ["hash_mismatch: cache.sc"]
    refused(d1, "manifest_verify")
    # (2) ... and the manifest resealed: the cache.sc no longer is the validated output
    _reseal_manifest(d1)
    assert read_sc(d1 / "cache.sc").snapshot_id() != sc.snapshot_id()
    refused(d1, "output_snapshot_mismatch")
    # (3) validation.json edited, sidecar not updated (manifest resealed)
    d3 = _writable_copy(ok.artifact_dir, tmp_path / "c3")
    (d3 / A.VALIDATION).write_text((d3 / A.VALIDATION).read_text() + " ")
    _reseal_manifest(d3)
    refused(d3, "validation_mismatch")
    # (4) a consistent source whose validation rejected the candidate
    d4 = _writable_copy(ok.artifact_dir, tmp_path / "c4")
    _rewrite_validation(d4, lambda v: v.update(verdict="rejected"))
    refused(d4, "validation_failed")
    # (5) validation names another output snapshot than the sidecar
    d5 = _writable_copy(ok.artifact_dir, tmp_path / "c5")
    _rewrite_validation(d5, lambda v: v.update(output_snapshot_id="sha256:" + "0" * 64))
    refused(d5, "validation_mismatch")
    # (6) an extra file the manifest does not list
    d6 = _writable_copy(ok.artifact_dir, tmp_path / "c6")
    (d6 / "extra.txt").write_text("x")
    refused(d6, "manifest_verify")
    # (7) validation rows that are not objects: a reason code, not AttributeError
    d7 = _writable_copy(ok.artifact_dir, tmp_path / "c7")
    _rewrite_validation(d7, lambda v: v.update(rows=["V1 pass", 3]))
    refused(d7, "validation_failed")
    # the publish primitive refuses (4) and (3)-style staging directories too
    for d, code in ((d4, "validation_failed"), (d3, "validation_mismatch")):
        st = _writable_copy(d, tmp_path / f"stage-{d.name}")
        with pytest.raises(A.ArtifactError) as e:
            A.publish(st, store)
        assert e.value.code == code
    assert published() == []
    # control: an untouched copy publishes, under the source's own ArtifactId
    d0 = _writable_copy(ok.artifact_dir, tmp_path / "c0")
    doc = publish_directory(str(d0), str(store))
    assert doc["artifact_id"] == ok.artifact_id and doc["status"] == "created"
    assert A.verify_manifest(doc["artifact_dir"]) == []


@pytest.mark.real
def test_bare_sc_of_a_negative_control_is_refused(negative, tmp_path):
    """Review minor: deploying `<neg_dir>/cache.sc` does not bypass the tags."""
    from scopt.formats.sc_v5 import ScHeader
    from scopt.run.deploy import artifact_refusals, resolve_artifact
    from scopt.run.policy import artifact_tags, deploy_refusals

    *_, r = negative
    bare = Path(r.path) / "cache.sc"
    assert artifact_tags(bare)["negative_control"]
    assert deploy_refusals(bare) == ["negative_control_artifact"]
    art = resolve_artifact(str(bare))
    with open(art["sc"], "rb") as f:
        h = ScHeader.from_bytes(f.read(80))
    assert artifact_refusals(art, h)[0] == ["negative_control_artifact"]
    # a bare .sc elsewhere has no tags
    other = tmp_path / "plain" / "x.sc"
    other.parent.mkdir()
    shutil.copyfile(bare, other)
    assert deploy_refusals(other) == []
    # its own sidecar X.scopt.json with a test policy is honoured
    (other.parent / "x.sc.scopt.json").write_text(json.dumps(
        {"x_test_only": {"policies": ["allow_ram_resident"], "waived": []}}))
    assert deploy_refusals(other) == ["test_only_artifact:allow_ram_resident"]


def test_o3_hits_from_a_crashed_gdb_run_are_unknown():
    req = {"hits": {"variant 0 C'": ">0"}}
    good = {"targets": [{"what": "variant 0 C'", "hits": 5, "run": 0}],
            "runs": [{"qemu_exit": 0}], "ok": True}
    assert O.o3_nonvacuity(None, good, req).verdict == "pass"
    for bad in ({**good, "runs": [{"qemu_exit": None}], "ok": False},
                {**good, "runs": [{"qemu_exit": 1}], "ok": False},
                {"targets": good["targets"], "ok": False}):
        r = O.o3_nonvacuity(None, bad, req)
        assert r.verdict == "not_run" and "hits[variant 0 C']" in r.detail
    # zero hits from a crashed run are unknown too, not "vacuous"
    z = {"targets": [{"what": "variant 0 C'", "hits": 0, "run": 0}],
         "runs": [{"qemu_exit": 137}], "ok": False}
    assert O.o3_nonvacuity(None, z, req).verdict == "not_run"


def test_o2b_needs_end_counters_in_both_modes():
    ref = _log("7-icount-warm-1.norm")
    lines = ref.splitlines()
    first_end = next(i for i, x in enumerate(lines) if x.startswith("Reg 0:0x005f"))
    cut = "\n".join(lines[:first_end]) + "\n"
    for mode in ("icount", "noicount"):
        r = O.o2b_records(ref, cut, mode)
        assert r.verdict == "fail" and "lacks end counters" in r.detail, (mode, r.detail)
    assert O.o2b_records(ref, ref, "noicount").verdict == "pass"


def test_icount_mode_parses_the_toml():
    assert O.icount_mode((STALE / "b3-1.toml").read_text()) == "icount"
    assert O.icount_mode("[fastdyn]\nicount = false\n") == "noicount"
    assert O.icount_mode("icount = 0\n") == "noicount"
    assert O.icount_mode('icount = "off"\n') == "noicount"
    assert O.icount_mode("icount = { shift = 0, enabled = false }\n") == "noicount"
    assert O.icount_mode("# icount = { shift = 0 }\nx = 1\n") == "noicount"
    assert O.icount_mode("[q]\nicount = { shift = 0 }\n") == "icount"
    assert O.icount_mode("icount = false\nbroken = [\n") == "noicount"  # text fallback
    assert O.icount_mode("icount = { shift = 0 }\nbroken = [\n") == "icount"


def _fake_runs(monkeypatch, *, stats=None, launch_verdict="PASS"):
    """dynamic_oracles with deploy/launch/stats stubbed (no QEMU)."""
    import types

    from scopt.run import deploy as D
    from scopt.run import diag as G
    from scopt.run import launcher as L

    def deploy_new(src, cfg, rd_path, **kw):
        Path(rd_path).mkdir(parents=True)
        return types.SimpleNamespace(root=Path(rd_path)), {}

    def launch(rd, **kw):
        return {"run_dir": str(rd.root), "verdict": launch_verdict, "exit_code": 0,
                "guest_norm": ["Reg 0:0x00500000", "Reg 1:0x00000001"],
                "output_norm_sha256": "sha256:" + "1" * 64, "norm_b0": "x", "appended": 0,
                "proof": {"verdict": launch_verdict, "failed": [], "appended": 0}}

    monkeypatch.setattr(D, "deploy_new", deploy_new)
    monkeypatch.setattr(L, "launch", launch)
    monkeypatch.setattr(G, "read_v6_stats", lambda p: stats)
    monkeypatch.setattr(G, "artifact_probe_addrs", lambda p: [])


def test_dynamic_oracles_incomplete_when_a_requested_oracle_is_not_run(tmp_path, monkeypatch):
    """Blocking review issue 2: a requested oracle that ends up not_run (no v6
    stats file: a v6 artifact on a v5-only binary) gives `incomplete`, never
    `pass`, a non-pass ledger row and a non-zero exit from the CLI."""
    from scopt.cli import cli, load_commands

    _fake_runs(monkeypatch, stats=None)
    art = tmp_path / "art"
    art.mkdir()
    (art / "cache.sc").write_bytes(b"")
    base = tmp_path / "base"
    base.mkdir()
    cfg = tmp_path / "c.toml"
    cfg.write_text("x = 1\n")
    req = {"stats": {"links_armed": ">0"}}
    doc = O.dynamic_oracles(art, [cfg], tmp_path / "o1", oracles=["O1", "O3"], baseline=base,
                            o3_requirements=req, quiet=True)
    rows = {r["oracle"]: r["verdict"] for r in doc["cases"][0]["rows"]}
    assert rows == {"O1": "pass", "O3": "not_run"}
    assert doc["verdict"] == "incomplete" and doc["failures"] == []
    assert doc["incomplete"] and "O3: not_run" in doc["incomplete"][0]
    led = (tmp_path / "o1" / "ledger.jsonl").read_text()
    assert '"incomplete"' in led and '"pass"' not in led
    # O3 asked for without requirements is not_run too
    doc = O.dynamic_oracles(art, [cfg], tmp_path / "o2", oracles=["O3"], baseline=base,
                            quiet=True)
    assert doc["verdict"] == "incomplete"
    # O6 on an unproved run: not_run -> incomplete (O1 fails -> fail wins)
    _fake_runs(monkeypatch, stats=None, launch_verdict="INVALID_artifact_not_used")
    doc = O.dynamic_oracles(art, [cfg], tmp_path / "o3", oracles=["O6"], baseline=base,
                            quiet=True)
    assert doc["verdict"] == "incomplete"
    doc = O.dynamic_oracles(art, [cfg], tmp_path / "o4", oracles=["O1", "O6"], baseline=base,
                            quiet=True)
    assert doc["verdict"] == "fail"
    # control: O1 alone ran and passed
    _fake_runs(monkeypatch, stats=None)
    doc = O.dynamic_oracles(art, [cfg], tmp_path / "o5", oracles=["O1"], baseline=base,
                            quiet=True)
    assert doc["verdict"] == "pass" and doc["incomplete"] == []
    # the CLI exits 1 for incomplete, 0 for pass
    load_commands()
    runner = CliRunner()
    res = runner.invoke(cli, ["validate", str(art), "--dynamic", "--oracles", "O1,O3",
                              "-c", str(cfg), "--out", str(tmp_path / "o6"),
                              "--baseline", str(base),
                              "--o3", json.dumps(req)])
    assert res.exit_code == 1 and "oracles incomplete" in res.output, res.output
    res = runner.invoke(cli, ["validate", str(art), "--dynamic", "--oracles", "O1",
                              "-c", str(cfg), "--out", str(tmp_path / "o7"),
                              "--baseline", str(base)])
    assert res.exit_code == 0 and "oracles pass" in res.output, res.output


# ---------------------------------------------------------------------------
# WP26 review 2: O4 fails closed (a control that never ran is never detected);
# O4 mode of dynamic_oracles; v6 artifact on a non-v6 QEMU; repeated record
# fields
# ---------------------------------------------------------------------------

def test_o4_fails_closed_on_a_control_that_did_not_run():
    """Blocking review issue: O1 'fail' because the artifact run was not
    proved (INVALID_artifact_not_used: the loader refused/rebuilt the file, or
    a v6 control on the v5-only qint binary) is not a detection."""
    ref = _run(proof={"verdict": "PASS", "appended": 0, "failed": []})
    # the reviewer's repro: identical outputs, artifact not used
    art = _run(verdict="INVALID_artifact_not_used",
               proof={"verdict": "INVALID_artifact_not_used", "failed": ["v6_header"]})
    o1 = O.o1_outputs(ref, art)
    assert o1.verdict == "fail"
    r = O.o4_detected([o1], artifact_run=art, reference_run=ref)
    assert r.verdict == "not_run" and "did not provably run" in r.detail, r
    # ... even when its outputs differ (a run without the artifact proves nothing)
    art2 = dict(art, guest_norm=["Reg 0:0x2"], exit_code=1)
    o1 = O.o1_outputs(ref, art2)
    assert O.o4_detected([o1], artifact_run=art2, reference_run=ref).verdict == "not_run"
    # stray capture files: the launch verdict is not an executed verdict
    art3 = _run(verdict="INVALID_stray_capture_files", guest_norm=["x"])
    o1 = O.o1_outputs(ref, art3)
    assert O.o4_detected([o1], artifact_run=art3, reference_run=ref).verdict == "not_run"
    # an unproved reference: unknown
    ref4 = _run(verdict="INVALID_exit_code")
    o1 = O.o1_outputs(ref4, _run(guest_norm=["x"]))
    assert O.o4_detected([o1], artifact_run=_run(guest_norm=["x"]),
                         reference_run=ref4).verdict == "not_run"
    # no launch documents: unknown
    assert O.o4_detected([o1], artifact_run=None, reference_run=ref).verdict == "not_run"
    # a proved control whose outputs equal the reference: NOT detected
    o1 = O.o1_outputs(ref, _run())
    assert O.o4_detected([o1], artifact_run=_run(), reference_run=ref).verdict == "fail"
    # a proved control with different outputs: detected
    art5 = _run(guest_norm=["Reg 0:0x2"])
    o1 = O.o1_outputs(ref, art5)
    r = O.o4_detected([o1], artifact_run=art5, reference_run=ref)
    assert r.verdict == "pass" and r.data["detected_by"] == ["O1"]
    # F-B4-9 shape: QEMU aborts while running the proved mutant -> detected
    art6 = _run(verdict="INVALID_exit_code", exit_code=134)
    o1 = O.o1_outputs(ref, art6)
    assert o1.data["differences"] == ["exit_code"]
    r = O.o4_detected([o1], artifact_run=art6, reference_run=ref)
    assert r.verdict == "pass", r
    # an O1 'fail' that records no output difference never counts
    bare = O.OracleResult("O1", "fail", "artifact run verdict X")
    r = O.o4_detected([bare], artifact_run=_run(), reference_run=ref)
    assert r.verdict == "not_run" and r.data["ignored"]


def test_o4_on_recorded_o2b_logs():
    """O2b detections count only with a structurally sound reference log."""
    ref = (LOGS / "7-icount-warm-1.norm").read_text()
    lines = ref.splitlines()
    cut = "\n".join(x for x in lines if not x.startswith("Reg 0:0x005f")) + "\n"
    runs = {"artifact_run": _run(), "reference_run": _run()}
    # the control's log lost its end counters (cut short): a record difference
    o2b = O.o2b_records(ref, cut, "icount")
    assert o2b.verdict == "fail" and o2b.data["reference_problems"] == []
    assert O.o4_detected([o2b], **runs).verdict == "pass"
    # a broken reference log: the 'fail' is not evidence about the control
    o2b = O.o2b_records(cut, ref, "icount")
    assert o2b.verdict == "fail" and o2b.data["reference_problems"]
    r = O.o4_detected([o2b], **runs)
    assert r.verdict == "not_run" and r.data["ignored"]
    # equal logs: not detected
    assert O.o4_detected([O.o2b_records(ref, ref, "icount")], **runs).verdict == "fail"


def test_parse_handler_records_rejects_a_repeated_field():
    """Review minor: a record whose index-0 pair is missing must not silently
    overwrite the previous record of that kind."""
    log = (LOGS / "7-icount-warm-1.norm").read_text()
    p = O.parse_handler_records(log)
    assert p["errors"] == [] and p["systick"]
    lines = log.splitlines()
    # drop the index-0 pair (0x0053_0000) of the second SysTick record
    idx = [i for i, x in enumerate(lines) if x == "Reg 0:0x00530000"]
    assert len(idx) >= 2
    k = idx[1]
    bad = lines[:k] + lines[k + 2:]
    q = O.parse_handler_records(bad)
    assert any("repeated field systick.pc" in e for e in q["errors"]), q["errors"][:3]
    assert q["systick"][0] == p["systick"][0]  # the first record is not overwritten
    r = O.o2b_records(log, bad, "icount")
    assert r.verdict == "fail" and "repeated field" in r.detail


def _fake_runs_per_arm(monkeypatch, artifact_doc, reference_doc):
    import types

    from scopt.run import deploy as D
    from scopt.run import diag as G
    from scopt.run import launcher as L

    def deploy_new(src, cfg, rd_path, **kw):
        Path(rd_path).mkdir(parents=True)
        return types.SimpleNamespace(root=Path(rd_path)), {}

    def launch(rd, **kw):
        d = artifact_doc if rd.root.name.endswith("-artifact") else reference_doc
        return {"run_dir": str(rd.root), **d}

    monkeypatch.setattr(D, "deploy_new", deploy_new)
    monkeypatch.setattr(L, "launch", launch)
    monkeypatch.setattr(G, "read_v6_stats", lambda p: None)
    monkeypatch.setattr(G, "artifact_probe_addrs", lambda p: [])


def test_dynamic_oracles_negative_control_o4_mode(tmp_path, monkeypatch):
    """Review minor: with --negative-control the verdict is O4's, so an exit 0
    means the mutant was detected; a control that did not run is incomplete."""
    from scopt.cli import cli, load_commands

    art, base = tmp_path / "art", tmp_path / "base"
    art.mkdir()
    base.mkdir()
    (art / "cache.sc").write_bytes(b"")
    cfg = tmp_path / "c.toml"
    cfg.write_text("x = 1\n")
    good = {"verdict": "PASS", "exit_code": 0, "guest_norm": ["Reg 0:0x00500000",
                                                              "Reg 1:0x00000001"],
            "output_norm_sha256": "sha256:" + "1" * 64, "appended": 0,
            "proof": {"verdict": "PASS", "failed": [], "appended": 0}}
    load_commands()
    runner = CliRunner()

    def run(name, art_doc, ref_doc=good):
        _fake_runs_per_arm(monkeypatch, art_doc, ref_doc)
        doc = O.dynamic_oracles(art, [cfg], tmp_path / name, oracles=["O1"], baseline=base,
                                allow_negative_control=True, quiet=True)
        res = runner.invoke(cli, ["validate", str(art), "--dynamic", "--oracles", "O1",
                                  "-c", str(cfg), "--out", str(tmp_path / (name + "-cli")),
                                  "--baseline", str(base), "--negative-control"])
        return doc, res

    # outputs equal to warm: the mutant was NOT detected -> fail, exit 1
    doc, res = run("eq", good)
    rows = {r["oracle"]: r["verdict"] for r in doc["cases"][0]["rows"]}
    assert rows == {"O1": "pass", "O4": "fail"} and doc["verdict"] == "fail"
    assert doc["negative_control"] is True
    assert res.exit_code == 1 and "mutant NOT detected" in res.output, res.output
    # outputs differ: detected -> pass, exit 0 (the O1 'fail' is the detection)
    doc, res = run("diff", dict(good, guest_norm=["Reg 0:0x00500000", "Reg 1:0x00000002"],
                                output_norm_sha256="sha256:" + "2" * 64))
    rows = {r["oracle"]: r["verdict"] for r in doc["cases"][0]["rows"]}
    assert rows == {"O1": "fail", "O4": "pass"} and doc["verdict"] == "pass"
    assert doc["failures"] == []
    assert res.exit_code == 0 and "mutant detected" in res.output, res.output
    led = (tmp_path / "diff" / "ledger.jsonl").read_text()
    assert "negative control O4" in led
    # the blocking case: the control never ran (artifact not used) -> incomplete, exit 1
    nu = dict(good, verdict="INVALID_artifact_not_used",
              proof={"verdict": "INVALID_artifact_not_used", "failed": ["v6_header"]})
    doc, res = run("nu", nu)
    rows = {r["oracle"]: r["verdict"] for r in doc["cases"][0]["rows"]}
    assert rows == {"O1": "fail", "O4": "not_run"} and doc["verdict"] == "incomplete"
    assert res.exit_code == 1 and "control not proved to run" in res.output, res.output
    # O4 mode needs an O1/O2a/O2b oracle; O4 outside a negative control is refused
    with pytest.raises(ValueError):
        O.dynamic_oracles(art, [cfg], tmp_path / "x", oracles=["O5"], baseline=base,
                          allow_negative_control=True, quiet=True)
    with pytest.raises(ValueError):
        O.dynamic_oracles(art, [cfg], tmp_path / "y", oracles=["O1", "O4"], baseline=base,
                          quiet=True)


def test_dynamic_oracles_refuses_v6_on_a_qemu_that_cannot_load_it(tmp_path, monkeypatch):
    """Blocking review issue (part 2): a v6 artifact run on a v5-only binary
    is refused before any launch (every v6 run there would be
    INVALID_artifact_not_used)."""
    import hashlib

    from scopt.cli import cli, load_commands
    from scopt.validate import v6_capable_exes

    art, base = tmp_path / "art", tmp_path / "base"
    art.mkdir()
    base.mkdir()
    (art / "cache.sc").write_bytes(b"SCACHE\0\0" + (6).to_bytes(4, "little") + b"\0" * 64)
    cfg = tmp_path / "c.toml"
    cfg.write_text("x = 1\n")
    exe = tmp_path / "qemu-v5only"
    exe.write_bytes(b"not a v6 loader")
    sha = hashlib.sha256(exe.read_bytes()).hexdigest()
    monkeypatch.delenv("SCOPT_V6_EXES", raising=False)
    assert sha not in v6_capable_exes()
    launched = []
    _fake_runs(monkeypatch)
    from scopt.run import launcher as L

    real = L.launch
    monkeypatch.setattr(L, "launch", lambda rd, **kw: launched.append(rd) or real(rd, **kw))
    with pytest.raises(O.OracleRefused) as e:
        O.dynamic_oracles(art, [cfg], tmp_path / "o", oracles=["O1"], baseline=base,
                          qemu=exe, quiet=True)
    assert e.value.code == "v6_loader_required" and launched == []
    load_commands()
    res = CliRunner().invoke(cli, ["validate", str(art), "--dynamic", "--oracles", "O1",
                                   "-c", str(cfg), "--out", str(tmp_path / "o2"),
                                   "--baseline", str(base), "--qemu", str(exe),
                                   "--negative-control"])
    assert res.exit_code == 2 and "v6_loader_required" in res.output, res.output
    # control: the same binary declared v6-capable runs
    monkeypatch.setenv("SCOPT_V6_EXES", sha)
    _fake_runs(monkeypatch)
    doc = O.dynamic_oracles(art, [cfg], tmp_path / "o3", oracles=["O1"], baseline=base,
                            qemu=exe, quiet=True)
    assert doc["verdict"] == "pass"
