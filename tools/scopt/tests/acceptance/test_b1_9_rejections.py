"""T-B1-9 rejection suite (design §6, WP14), plus the T-B1-8 synthetic recovery case.

Each case asserts its reason code. Real files come from a B1 gate run on the
minimal workload (``tests/qemu/b1_gate.py``; ``$B1_GATE_DIR``, default
``runs/wp14/gate/minimal``): the fresh prep ``.sc`` and its capture, the store
snapshot, the identity artifact and a profile ``.qprof``. Each case mutates a
private copy; nothing under the gate directory is written. Cases that cannot be
made from a real file without also breaking an earlier check use the WP4/WP7
synthetic builders (``tests/synth``), as design §6 allows ("synthetic builders
plus real files"): ``missing_reference`` has both, ``record_mismatch`` both,
``unsupported_arena_below_4g`` synthetic only (the real arena is above 4 GiB).

The cases that start QEMU (``record_checksum`` dynamic half, the v6-stamped
proof, the modifiers.txt bundle check) are marked ``qemu``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.canon import fnv1a64
from scopt.formats import scm_v1 as S
from scopt.formats.enums import QprofHeaderFlag, RunInfoFlag, SiteKind
from scopt.formats.sc_v5 import HEADER_SIZE, RECORD_SIZE, ScHeader, read_sc, sparse_copy
from scopt.importer.import_cache import ImportRejected, import_snapshot
from tests.synth import builders as B
from tests.synth.rich_tb import rich_tb, rich_units, with_meta, write_rich_pair

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
GATE = Path(os.environ.get("B1_GATE_DIR", ROOT / "runs/wp14/gate/minimal"))
WT = Path(__file__).resolve().parents[2]
SC_CODE_OFFSET = 0x10000

pytestmark = [pytest.mark.real]


def _need(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            pytest.skip(f"{p} not present (run tests/qemu/b1_gate.py --workload minimal)")


def _one(pattern: str) -> Path:
    xs = sorted(GATE.glob(pattern))
    if len(xs) != 1:
        pytest.skip(f"expected one {GATE}/{pattern}, found {len(xs)}")
    return xs[0]


@pytest.fixture(scope="module")
def real():
    prep = GATE / "prep"
    _need(prep / "prep.json", prep / "capture.scm", GATE / "bundle.json",
          GATE / "runtime_identity.json")
    pj = json.loads((prep / "prep.json").read_text())
    assert pj["verdict"] == "PASS"
    return {"sc": Path(pj["sc"]), "scm": prep / "capture.scm", "manifest": Path(pj["manifest"]),
            "bundle": GATE / "bundle.json", "ri": GATE / "runtime_identity.json",
            "config": Path(pj["base_toml"])}


def _copy(real, tmp: Path, *, scm: bytes | None = None, manifest: dict | None = None) -> Path:
    """A private prep dir: the real .sc (sparse), capture and manifest."""
    d = tmp / "p"
    d.mkdir(parents=True, exist_ok=True)
    sc = d / real["sc"].name
    sparse_copy(real["sc"], sc, fsync=False)
    (d / "capture.scm").write_bytes(scm if scm is not None else real["scm"].read_bytes())
    man = Path(str(sc) + ".fastdyn.json")
    if manifest is None:
        shutil.copyfile(real["manifest"], man)
    else:
        man.write_text(json.dumps(manifest))
    return d


def _patch(path: Path, off: int, data: bytes) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        os.pwrite(fd, data, off)
    finally:
        os.close(fd)


def _set_header(path: Path, **fields) -> None:
    fd = os.open(path, os.O_RDWR)
    try:
        h = ScHeader.from_bytes(os.pread(fd, HEADER_SIZE, 0)).replace(**fields).with_checksum()
        os.pwrite(fd, h.to_bytes(), 0)
    finally:
        os.close(fd)


def _imp(d: Path, **kw):
    return import_snapshot(str(d), **kw)


def _synth(units, tmp: Path, **kw) -> Path:
    d = tmp / "p"
    d.mkdir(parents=True, exist_ok=True)
    write_rich_pair(list(units), str(d), **kw)
    return d


def _tb_chunks(scm: bytes) -> dict[int, tuple[int, S.TbChunk]]:
    f = S.parse_scm(scm)
    return {ch.body.slot: (ch.seq, ch.body) for ch in f.chunks if isinstance(ch.body, S.TbChunk)}


# ---------------------------------------------------------------------------
# .sc structure
# ---------------------------------------------------------------------------

def test_record_checksum_skipped_like_qemu(real, tmp_path):
    d = _copy(real, tmp_path)
    sc = d / real["sc"].name
    h = read_sc(sc).header
    slot = 5
    off = SC_CODE_OFFSET + h.code_size + slot * RECORD_SIZE + 64  # guest_hash, under the checksum
    with open(sc, "rb") as f:
        f.seek(off)
        b = f.read(1)
    _patch(sc, off, bytes([b[0] ^ 0x01]))
    loaded = read_sc(sc)  # the replicated load_records
    assert [s.slot for s in loaded.skipped] == [slot]
    assert loaded.skipped[0].reason == "record_checksum"
    res = _imp(d)
    assert res.summary["records"]["skipped"] == [{"slot": slot, "reason": "record_checksum"}]
    assert slot not in [u.slot for u in res.snapshot.units()]
    assert res.summary["records"]["valid"] == h.record_count - 1


@pytest.mark.qemu
def test_record_checksum_qemu_skips_the_same_record(real, tmp_path):
    """Dynamic half: QEMU skips the same record (it re-translates that TB, appended 1),
    keeps every other record, and the guest output is unchanged."""
    from scopt.run.deploy import deploy_new
    from scopt.run.launcher import launch

    d = _copy(real, tmp_path)
    sc = d / real["sc"].name
    h = read_sc(sc).header
    off = SC_CODE_OFFSET + h.code_size + 5 * RECORD_SIZE + 64
    with open(sc, "rb") as f:
        f.seek(off)
        b = f.read(1)
    _patch(sc, off, bytes([b[0] ^ 0x01]))
    ri = json.loads(real["ri"].read_text())
    rd, _ = deploy_new(sc, real["config"], tmp_path / "rd",
                       qemu=ri["qemu_exe_stat"]["path"])
    r = launch(rd)
    assert r["exit_code"] == 0
    assert r["proof"]["verdict"] == "PASS"  # records [0, n) unchanged: nothing was rebuilt
    assert r["appended"] == 1
    assert "Reg 0:0x00000003" in r["guest_norm"]


def test_header_checksum_rejects(real, tmp_path):
    d = _copy(real, tmp_path)
    sc = d / real["sc"].name
    with open(sc, "rb") as f:
        raw = f.read(HEADER_SIZE)
    h = ScHeader.from_bytes(raw)
    _patch(sc, 0, h.replace(checksum=h.checksum ^ 1).to_bytes())
    with pytest.raises(ImportRejected) as e:
        _imp(d)
    assert e.value.reason == "header_checksum"


def test_version_6_rejects(real, tmp_path):
    d = _copy(real, tmp_path)
    _set_header(d / real["sc"].name, version=6)
    with pytest.raises(ImportRejected) as e:
        _imp(d)
    assert e.value.reason == "version"


def test_truncated_record_array_count_is_min(real, tmp_path):
    d = _copy(real, tmp_path)
    sc = d / real["sc"].name
    n = read_sc(sc).header.record_count
    _set_header(sc, record_count=n + 40)  # the file holds only n records
    res = _imp(d)
    r = res.summary["records"]
    assert r["record_count"] == n + 40 and r["count"] == n  # count = min(record_count, available)
    assert [p["code"] for p in r["problems"]] == ["truncated_records"]
    assert res.summary["status"]["ok_relocatable"] == n


# ---------------------------------------------------------------------------
# capture binding
# ---------------------------------------------------------------------------

def test_record_mismatch_capture_from_another_run_real(real, tmp_path):
    """Slot 3's chunk carries another record (as a capture of another run would)."""
    data = real["scm"].read_bytes()
    tbs = _tb_chunks(data)
    seq, tb = tbs[3]
    f = S.parse_scm(data)
    bad = S.encode_scm(S.replace_body(f, seq, replace(tb, record=tbs[4][1].record)))
    res = _imp(_copy(real, tmp_path, scm=bad))
    u = res.snapshot.by_slot(3)
    assert u.meta_status == "no_meta" and "record_mismatch" in u.reloc.reasons
    assert res.snapshot.status(u.uid) == "no_meta"
    assert res.snapshot.status(res.snapshot.by_slot(4).uid) == "ok_relocatable"


def test_record_mismatch_synthetic(tmp_path):
    us = rich_units(2)
    other = B.resolve_unit(rich_tb(us[0].tb_offset, guest_bytes=b"\x01\xbf" * 4)).record_bytes
    us[0] = with_meta(us[0], scm_record=other)
    res = import_snapshot(str(_synth(us, tmp_path)))
    assert res.snapshot.by_slot(0).reloc.reasons == ("no_meta", "record_mismatch")


def test_chunk_checksum_drops_chunk_and_suffix(real, tmp_path):
    data = real["scm"].read_bytes()
    tab = B.chunk_table(data)
    tb_seqs = [c["seq"] for c in tab if c["type"] == 4]
    k = 10
    bad = B.corrupt_trailer(data, tb_seqs[k])
    res = _imp(_copy(real, tmp_path, scm=bad))
    codes = [p["code"] for p in res.summary["capture"]["problems"]]
    assert "chunk_checksum" in codes
    ok_slots = {c.body.slot for c in S.parse_scm(data).chunks[:]
                if isinstance(c.body, S.TbChunk) and c.seq < tb_seqs[k]}
    meta = {u.slot: u.meta_status for u in res.snapshot.units()}
    assert {s for s, m in meta.items() if m == "ok"} == ok_slots  # the chunk and its suffix
    assert len(ok_slots) == k and res.summary["capture"]["complete"] is False


def _site_unit(data: bytes, kind: SiteKind) -> tuple[int, int, S.TbChunk, int]:
    for slot, (seq, tb) in sorted(_tb_chunks(data).items()):
        for i, s in enumerate(tb.sites):
            if s.kind == kind:
                return slot, seq, tb, i
    pytest.skip(f"no {kind.name} site in the capture")


def test_unexplained_reference_site_removed_from_capture(real, tmp_path):
    data = real["scm"].read_bytes()
    slot, _seq, _tb, i = _site_unit(data, SiteKind.HELPER_CALL)
    res = _imp(_copy(real, tmp_path, scm=B.drop_site(data, slot, i)))
    u = res.snapshot.by_slot(slot)
    assert u.meta_status == "ok" and not u.reloc.relocatable
    assert any(r.startswith("unexplained_reference@") for r in u.reloc.reasons), u.reloc.reasons
    assert res.snapshot.status(u.uid) == "pinned"
    assert res.summary["status"]["ok_relocatable"] == res.summary["records"]["valid"] - 1


def test_missing_reference_site_removed_from_bytes_real(real, tmp_path):
    """The RIP-relative LEA of a MOVI_CONST site becomes a 7-byte NOP in the .sc and
    the capture's code FNV is updated to those bytes, so the chunk still binds but
    the captured site has no reference in the code."""
    data = real["scm"].read_bytes()
    slot, seq, tb, i = _site_unit(data, SiteKind.MOVI_CONST)
    site = tb.sites[i]
    d = _copy(real, tmp_path)
    sc = d / real["sc"].name
    u = read_sc(sc).units()[slot]
    assert site.field_off + site.width - site.insn_off == 7  # lea r64, [rip + disp32]
    body = bytearray(u.body)
    body[site.insn_off:site.insn_off + 7] = bytes.fromhex("0f1f8000000000")
    _patch(sc, SC_CODE_OFFSET + u.record.tc_offset, bytes(body))
    f = S.parse_scm(data)
    (d / "capture.scm").write_bytes(S.encode_scm(S.replace_body(
        f, seq, replace(tb, code_fnv64=fnv1a64(bytes(body))))))
    res = _imp(d)
    v = res.snapshot.by_slot(slot)
    assert v.meta_status == "ok" and not v.reloc.relocatable
    assert any(r.startswith("missing_reference@") for r in v.reloc.reasons), v.reloc.reasons
    with pytest.raises(ImportRejected) as e:
        _imp(d, strict=True)
    assert e.value.reason == "missing_reference"


def test_missing_reference_synthetic(tmp_path):
    us = rich_units(2)
    code = bytearray(us[1].code)
    code[22:29] = bytes.fromhex("0f1f8000000000")
    us[1] = replace(us[1], code=bytes(code))
    res = import_snapshot(str(_synth(us, tmp_path)))
    assert "missing_reference@0x19" in res.snapshot.by_slot(1).reloc.reasons


def test_code_fnv_mismatch_code_byte_flip(real, tmp_path):
    d = _copy(real, tmp_path)
    sc = d / real["sc"].name
    u = read_sc(sc).units()[7]
    off = SC_CODE_OFFSET + u.record.tc_offset + 20
    _patch(sc, off, bytes([u.body[20] ^ 0x01]))  # records do not cover code bytes
    res = _imp(d)
    v = res.snapshot.by_slot(7)
    assert v.meta_status == "no_meta" and "code_fnv_mismatch" in v.reloc.reasons
    assert res.snapshot.status(v.uid) == "no_meta"


def test_run_info_runtime_signature_differs_from_header(real, tmp_path):
    bad = B.swap_run(real["scm"].read_bytes(), {"runtime_signature": 1})
    with pytest.raises(ImportRejected) as e:
        _imp(_copy(real, tmp_path, scm=bad))
    assert e.value.reason == "run_info_mismatch:runtime_signature"


def test_unsupported_flat_run_info(real, tmp_path):
    bad = B.swap_run(real["scm"].read_bytes(), {"flags": int(RunInfoFlag.FLAT)})
    with pytest.raises(ImportRejected) as e:
        _imp(_copy(real, tmp_path, scm=bad))
    assert e.value.reason == "unsupported_flat"


def test_unsupported_flat_manifest(real, tmp_path):
    man = json.loads(real["manifest"].read_text())
    man["inputs"]["settings"]["supercache_flat"] = True
    with pytest.raises(ImportRejected) as e:
        _imp(_copy(real, tmp_path, manifest=man))
    assert e.value.reason == "unsupported_flat"


def test_unsupported_arena_below_4g_synthetic(tmp_path):
    us = B.simple_units(1, code_address=0x7F000000)
    d = _synth(us, tmp_path, sc_header={"code_address": 0x7F000000})
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(d))
    assert e.value.reason == "unsupported_arena_below_4g"


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------

def test_bundle_mismatch_manifest_modifiers(real, tmp_path):
    """The manifest records modifiers.txt's sha256: an edited file -> bundle_mismatch."""
    man = json.loads(real["manifest"].read_text())
    cf = man["inputs"]["content_files"]
    cf["modifiers"] = {"sha256": "ab" * 32, "size": 17}
    with pytest.raises(ImportRejected) as e:
        _imp(_copy(real, tmp_path, manifest=man), bundle=str(real["bundle"]))
    assert e.value.reason == "bundle_mismatch"


@pytest.mark.qemu
def test_bundle_mismatch_modifiers_txt_edited(real, tmp_path):
    """Real FastDyn: a config reading modifiers.txt from a directory; editing only that
    file changes the identity bundle (bundle_mismatch), and deploy refuses an
    artifact carrying the first bundle."""
    from scopt.formats.bundle_v1 import BundleMismatch, check_bundle, identity_bundle
    from scopt.run.deploy import DeployError, deploy_new, oracle
    from scopt.run.launcher import create_run_dir

    if real["config"].name != "b0-sc.toml":
        pytest.skip("the modifiers.txt case is written for the minimal b0-sc.toml")
    qemu = json.loads(real["ri"].read_text())["qemu_exe_stat"]["path"]
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "modifiers.txt").write_text("0x800397e r0 <- 3\n")
    text = real["config"].read_text()
    assert text.count("existing_config_path = false") == 1
    cfg = tmp_path / "ext.toml"
    cfg.write_text(text.replace("existing_config_path = false", f'existing_config_path = "{ext}"'))
    rd1 = create_run_dir(cfg, tmp_path / "ext1", qemu=qemu)
    oracle(rd1)
    b1 = identity_bundle(rd1.toml, rd1.manifest)
    (ext / "modifiers.txt").write_text("0x800397e r0 <- 4\n")
    rd2 = create_run_dir(cfg, tmp_path / "ext2", qemu=qemu)
    oracle(rd2)
    with pytest.raises(BundleMismatch) as e:
        check_bundle(b1, identity_bundle(rd2.toml, rd2.manifest))
    assert e.value.code == "bundle_mismatch"
    art = tmp_path / "art"
    art.mkdir()
    sparse_copy(real["sc"], art / "cache.sc", fsync=False)
    (art / "bundle.json").write_text(json.dumps(b1))
    shutil.copyfile(real["ri"], art / "runtime_identity.json")
    with pytest.raises(DeployError) as e2:
        deploy_new(art, cfg, tmp_path / "dep", qemu=qemu)
    assert e2.value.code == "bundle_mismatch"
    assert not (tmp_path / "dep/cache" / real["sc"].name).exists()


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def prof():
    r0 = GATE / "prof/rep-0"
    _need(r0 / "run.qprof", r0 / "profile.json", GATE / "prof/merged.profile.json")
    snap = _one("store/snapshots/*")
    return {"qprof": r0 / "run.qprof", "staged": _one("prof/rep-0/cache/*.sc"),
            "merged": GATE / "prof/merged.profile.json", "snap": snap,
            "trunc": next(iter(sorted(GATE.glob("store-trunc/snapshots/*"))), None)}


def test_profile_incompatible_other_bundle(prof):
    from scopt.formats.profile_v1 import ProfileIncompatible, ProfileTarget, ProfileView, \
        read_profile

    p = read_profile(prof["merged"])
    v = ProfileView.open(p, ProfileTarget.from_any(str(prof["snap"])))  # the real pairing works
    assert v.same_snapshot
    with pytest.raises(ProfileIncompatible) as e:
        ProfileView.open(p, ProfileTarget.from_any(str(prof["snap"]),
                                                   bundle_id="sha256:" + "e" * 64))
    assert e.value.reason == "profile_incompatible:bundle"


def test_profile_incompatible_coverage(prof):
    """The truncated snapshot (first 100 of 163 units) carries ~77 % of the steady-phase
    entries (all boot entries land on its units), below the default 0.95."""
    from scopt.formats.profile_v1 import ProfileIncompatible, ProfileTarget, ProfileView, \
        read_profile

    if prof["trunc"] is None:
        pytest.skip("no truncated snapshot in the gate run")
    p = read_profile(prof["merged"])
    t = ProfileTarget.from_any(str(prof["trunc"]))
    assert ProfileView.open(p, t, phase="boot").coverage == 1.0
    with pytest.raises(ProfileIncompatible) as e:
        ProfileView.open(p, t, phase="steady")  # default min_profile_match 0.95
    assert e.value.reason == "profile_incompatible:coverage"
    assert e.value.coverage < 0.95


def test_unclean_exit_gives_lower_bound_never_exact(prof, tmp_path):
    from scopt.formats import qprof_v1 as Q
    from scopt.formats.profile_v1 import ProfileView, ProfileTarget, ingest

    q = Q.read_qprof(prof["qprof"])
    assert q.header.flags & QprofHeaderFlag.CLEAN_EXIT
    hd = {k: getattr(q.header, k) for k in q.header.__dataclass_fields__}
    hd["flags"] = int(q.header.flags) & ~int(QprofHeaderFlag.CLEAN_EXIT)
    bad = tmp_path / "unclean.qprof"
    bad.write_bytes(Q.encode_qprof(hd, q.phases, q.tbs, q.edges))
    bundle = json.loads((prof["snap"] / "bundle.json").read_text())
    ri = json.loads((prof["snap"] / "runtime_identity.json").read_text())
    p = ingest(str(bad), str(prof["staged"]), str(prof["snap"]), bundle_id=bundle["bundle_id"],
               contract_id=bundle["contract_id"], runtime_identity=ri)
    assert not p.complete
    assert {u["status"] for u in p.units} == {"lower_bound"}
    assert {u["reason"] for u in p.units} == {"unclean_exit"}
    v = ProfileView.open(p, ProfileTarget.from_any(str(prof["snap"])), phase="boot")
    assert all(c.status != "exact" for ph in ("boot", "steady") for c in v.phase(ph).values())


# ---------------------------------------------------------------------------
# artifacts and the post-run proof
# ---------------------------------------------------------------------------

def test_manifest_that_fails_to_verify(tmp_path):
    from scopt.formats import artifact as A

    art = _one("store/artifacts/*")
    snap = _one("store/snapshots/*")
    assert A.verify_manifest(art) == []
    bad = tmp_path / "art"
    shutil.copytree(art, bad, copy_function=lambda s, d: (
        sparse_copy(s, d, fsync=False) if s.endswith(".sc") else shutil.copy2(s, d)))
    os.chmod(bad / A.LEDGER, 0o644)
    with open(bad / A.LEDGER, "ab") as f:
        f.write(b"\n")
    assert A.verify_manifest(bad) != []
    p = subprocess.run([sys.executable, "-m", "scopt", "validate", str(bad), "--snapshot",
                        str(snap)], cwd=str(WT), capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(WT)})
    assert p.returncode == 1, p.stdout + p.stderr
    assert "V8" in p.stdout


@pytest.mark.qemu
def test_post_run_proof_on_v6_stamped_copy(real, tmp_path):
    from scopt.run.deploy import deploy_new
    from scopt.run.launcher import launch

    snap = _one("store/snapshots/*")
    v6 = tmp_path / "v6.sc"
    sparse_copy(snap / "cache.sc", v6, fsync=False)
    _set_header(v6, version=6)
    ri = json.loads(real["ri"].read_text())
    qemu = ri["qemu_exe_stat"]["path"]
    rd, dep = deploy_new(v6, real["config"], tmp_path / "rd", qemu=qemu)
    assert dep["artifact_header_problems"] == ["version"]
    r = launch(rd)
    log = Path(r["stdout_log"]).read_text(errors="replace")
    # The expectation depends on the loader, chosen by the exe identity (not by the
    # output under test, so a v6 loader that silently truncated would fail here).
    from scopt.validate import v6_capable_exes
    exe = str(ri.get("qemu_sha256") or "").removeprefix("sha256:")
    if exe and exe in v6_capable_exes():
        # A v6 loader (qint-v6, WP22; design B3-B4 §5.1 row 3, §5.2 [R2]): version 6 with
        # the EXT_TABLE feature bit clear fails loud before guest execution and does not
        # touch the file, instead of the v5 truncate. The B1 intent of this case, "a run
        # that could not use the artifact is never a valid sample", is then carried by the
        # run verdict (QEMU exit != 0 -> INVALID_exit_code, launcher.py), while the proof
        # passes because the file is byte-identical. This branch was added after the first
        # T-B3-27 run on the gate binary failed on the v5 expectation; see the B3-B4
        # design implementation note "B3/B4 audit fix: T-B1-9 v6-stamped copy".
        assert "supercache: invalid v6 extension: feature EXT_TABLE clear" in log
        assert r["exit_code"] != 0
        assert r["verdict"] == "INVALID_exit_code"
        assert r["proof"]["verdict"] == "PASS"     # file byte-identical: nothing was rewritten
        assert not any(l.startswith(("Reg ", "[BENCH]")) for l in r["guest_norm"])
        return
    # A v5 loader (qint): valid_header fails, the copy is truncated and rebuilt (B1 §4.8).
    assert "supercache: invalid v6 extension" not in log
    assert r["proof"]["verdict"] == "INVALID_artifact_not_used"
    assert r["proof"]["failed"] == ["header"]
    assert r["verdict"] == "INVALID_artifact_not_used"
    assert r["appended"] is None


# ---------------------------------------------------------------------------
# T-B1-8, synthetic half
# ---------------------------------------------------------------------------

def test_recovery_mismatch_synthetic(tmp_path):
    """A unit whose insn-0/1 boundary (host_end row) is shifted past a helper-call
    return: that return unwinds to insn 0, not its OpMark insn 1."""
    us = rich_units(2)
    ins = list(us[1].meta["insns"])
    ins[0] = S.ScmInsn(ins[0].words, ins[0].host_start, 35)
    ins[1] = S.ScmInsn(ins[1].words, 35, ins[1].host_end)
    us[1] = with_meta(us[1], insns=ins)
    res = import_snapshot(str(_synth(us, tmp_path)))
    assert res.snapshot.by_slot(1).reloc.reasons == ("recovery_mismatch",)
    assert res.snapshot.by_slot(0).reloc.relocatable
