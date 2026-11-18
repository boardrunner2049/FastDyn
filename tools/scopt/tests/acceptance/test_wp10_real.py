"""WP10 real-data acceptance: T-B1-5 static half, F-B2-1/F-B2-2/F-B2-5 static, V10 smoke.

Inputs (made once, outside the repo; the tests skip when they are absent). All
snapshots come from the integrated WP-QINT binary
(``wt/qemu-int/build/qemu-system-arm``, sha256 8864c726…):

* ``runs/wp10/prep-minimal``: capture-on cold run of ``configs/b0-sc.toml``
  (``tests/qemu/wp7_prep_capture.py``): ``cache/minimal.sc`` + manifest +
  ``capture.scm``;
* ``runs/wp10/r2/minimal-bundle.json``: ``scopt bundle identity -c
  prep-minimal/run.toml --manifest prep-minimal/cache/minimal.sc.fastdyn.json``;
* ``runs/wp10/r2/minimal-runtime-identity.json``: ``scopt bundle
  runtime-identity --run-dir runs/wp10/oracle-rd --sc prep-minimal/cache/minimal.sc
  --scm prep-minimal/capture.scm``;
* ``runs/wp6/real-qint-1/merged.profile.json``: a Profile v1 of that snapshot
  (picks the hottest unit for F-B2-1);
* FreeRTOS MPS2: ``runs/qint/t-b1-1-freertos`` (T-B1-1 on1.sc with on1/on2
  captures and the run's manifest) and ``runs/wp10/r2/freertos/{bundle,
  runtime_identity}.json`` (made the same way from an oracle run dir of
  ``runs/qint/configs/freertos-sc-int.toml``).
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from scopt.formats import artifact as A
from scopt.formats.sc_v5 import sha256_sparse
from scopt.importer.import_cache import import_snapshot, open_snapshot
from scopt.link import NativeLinker, round_up
from scopt.manager import PassManager
from scopt.validate import optimize, validate_artifact

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
W10 = Path(os.environ.get("WP10_REAL_DIR", ROOT / "runs/wp10"))
PREP = W10 / "prep-minimal"
BUNDLE = W10 / "r2/minimal-bundle.json"
RI = W10 / "r2/minimal-runtime-identity.json"
PROFILE = ROOT / "runs/wp6/real-qint-1/merged.profile.json"
FRT = ROOT / "runs/qint/t-b1-1-freertos"
FRT_ID = W10 / "r2/freertos"
B0_SC = ROOT / "src/fastdyn/configs/b0-sc.toml"
PIPES = Path(__file__).resolve().parents[1] / "data/pipelines"

pytestmark = [pytest.mark.real, pytest.mark.slow]


def _need(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            pytest.skip(f"{p} not present")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return tmp_path_factory.mktemp("wp10store")


@pytest.fixture(scope="module")
def minimal(store):
    _need(PREP / "capture.scm", BUNDLE, RI)
    res = import_snapshot(str(PREP), bundle=str(BUNDLE), runtime_identity=str(RI),
                          store=str(store))
    assert res.summary["status"] == {"no_meta": 0, "ok_relocatable": 163, "pinned": 0}
    return open_snapshot(res.store_dir)


def _all_pass(res, upto: int = 8) -> None:
    assert res.published, (res.reason, res.validation and res.validation.fails())
    c = res.validation.codes()
    assert all(c[f"V{i}"] == "pass" for i in range(1, upto + 1)), c


def test_t_b1_5_static_identity(minimal, store):
    res = optimize(minimal, "identity", store)
    _all_pass(res, 9)
    art = Path(res.artifact_dir)
    # V9: whole-file sha256 equality with the input cache
    assert sha256_sparse(art / "cache.sc") == sha256_sparse(Path(minimal.path) / "cache.sc")
    # derived .scm re-imports to the same SnapshotId, every slot bound, same statuses
    again = import_snapshot(sc=str(art / "cache.sc"), scms=[str(art / "cache.scm")])
    assert again.snapshot.snapshot_id == minimal.snapshot_id == res.output_snapshot_id
    assert again.join.histogram() == {"bound": 163}
    assert again.summary["status"] == {"no_meta": 0, "ok_relocatable": 163, "pinned": 0}
    assert A.verify_manifest(art) == []
    rv = validate_artifact(art, minimal)
    assert rv["verdict"] == "accepted" and rv["stored_codes_agree"]
    side = json.loads((art / A.SIDECAR).read_text())
    assert side["bundle_id"] == minimal.bundle["bundle_id"]
    assert side["runtime_identity_id"] == minimal.runtime["runtime_identity_id"]
    assert side["move_map"] == {} and side["loader_requirement"] == "qemu-sc-v5-unmodified"


def _hottest_relocatable(snap) -> str:
    _need(PROFILE)
    prof = json.loads(PROFILE.read_text())
    assert prof["snapshot_id"] == snap.snapshot_id
    tot = {u["unit"]: sum(c["entries"] for c in u["phase"].values())
           for u in prof["units"] if u["status"] == "exact"}
    rel = [u for u in snap.units() if snap.status(u.uid) == "ok_relocatable"]
    return max(rel, key=lambda u: (tot.get(u.uid, 0), u.uid)).uid


@pytest.mark.parametrize("extra", [0, 4 << 20], ids=["max_end", "plus_4MiB"])
def test_f_b2_1_static(minimal, store, extra):
    uid = _hottest_relocatable(minimal)
    u = minimal.unit(uid)
    target = round_up(minimal.max_end, 64) + extra
    res = optimize(minimal, PIPES / "test-move-units.toml", store,
                   params={"move-units": {"moves": {uid: target}}})
    _all_pass(res)
    assert res.validation.codes()["V9"] == "skip"
    lu = res.link.by_uid()[uid]
    assert lu.new_tb_offset == target and sum(x.moved for x in res.link.units) == 1
    # the byte diff is exactly the unit's prologue/ext fixup fields
    old = u.code + u.search
    diff = {i for i, (a, b) in enumerate(zip(old, lu.body)) if a != b}
    fields = {i for f in u.reloc.fixups for i in range(f.field_off, f.field_off + f.width)}
    assert diff and diff <= fields
    assert {f.target_class.split(".")[0] for f in u.reloc.fixups} <= {"prologue", "ext"}


def test_f_b2_2_static_reverse_every_unit(minimal, store):
    # reverse-layout alone leaves the mirror's fixed point(s) in place; exclude
    # them from the order and move them explicitly so every unit moves
    cand = PassManager(minimal, PIPES / "test-reverse-layout.toml").execute()
    fixed = [x.uid for x in NativeLinker(cand, derive_captures=False).link().units
             if not x.moved]
    base = round_up(minimal.max_end, 64) + (8 << 20)
    moves, off = {}, base
    for uid in fixed:
        moves[uid] = off
        u = minimal.unit(uid)
        off = round_up(off + u.end_offset - u.tb_offset, 64)
    res = optimize(minimal, PIPES / "test-reverse-move.toml", store,
                   params={"reverse-layout": {"exclude": fixed},
                           "move-units": {"moves": moves}})
    _all_pass(res)
    moved = {x.uid for x in res.link.units if x.moved}
    assert len(moved) == 163
    cls = Counter(f.target_class for u in minimal.units() if u.uid in moved
                  for f in u.reloc.fixups)
    # every fixup class of the snapshot (G17: 316 tb_ret, 3 epilogue, 3 plugin calls,
    # 3 plugin LEAs) is applied
    assert cls == {"prologue.tb_ret": 316, "prologue.epilogue": 3, "ext.plugin": 3,
                   "ext.ptr_riprel": 3}
    assert [x.uid for x in res.link.units] == [u.uid for u in minimal.units()]  # slot order


def test_f_b2_5_static_rel32_range_rollback(minimal, store):
    u = next(u for u in minimal.units()
             if any(f.target_class == "ext.plugin" for f in u.reloc.fixups))
    res = optimize(minimal, PIPES / "test-move-units.toml", store,
                   params={"move-units": {"moves": {u.uid: 0x60000000}}})
    _all_pass(res, 9)  # nothing moved after the rollback: identity again
    assert [(f.uid, f.reason) for f in res.link.rollbacks] == [(u.uid, "rel32_range")]
    side = json.loads((Path(res.artifact_dir) / A.SIDECAR).read_text())
    assert side["move_map"] == {}
    assert {"unit": u.uid, "reasons": ["failed:rel32_range"]} in side["pinned"]
    assert sha256_sparse(Path(res.artifact_dir) / "cache.sc") == \
        sha256_sparse(Path(minimal.path) / "cache.sc")


@pytest.fixture(scope="module")
def freertos(store):
    _need(FRT / "archive/on1.sc", FRT / "scm/on1.scm", FRT / "scm/on2.scm",
          FRT / "rd/cache/freertos.sc.fastdyn.json", FRT_ID / "bundle.json",
          FRT_ID / "runtime_identity.json")
    res = import_snapshot(sc=str(FRT / "archive/on1.sc"),
                          scms=[str(FRT / "scm/on1.scm"), str(FRT / "scm/on2.scm")],
                          manifest=str(FRT / "rd/cache/freertos.sc.fastdyn.json"),
                          bundle=str(FRT_ID / "bundle.json"),
                          runtime_identity=str(FRT_ID / "runtime_identity.json"),
                          store=str(store))
    assert res.summary["capture"]["binding"] == {"bound": 1013}
    return open_snapshot(res.store_dir)


def test_t_b1_5_static_identity_freertos_two_captures(freertos, store):
    res = optimize(freertos, "identity", store)
    _all_pass(res, 9)
    art = Path(res.artifact_dir)
    assert sha256_sparse(art / "cache.sc") == sha256_sparse(Path(freertos.path) / "cache.sc")
    # both captures bind every slot; the union keeps the first, so one derived file
    assert [n for n, _ in res.link.derived_scms] == ["cache.scm"]
    assert res.output_snapshot_id == freertos.snapshot_id


def test_freertos_reverse_layout_static(freertos, store):
    res = optimize(freertos, PIPES / "test-reverse-layout.toml", store)
    _all_pass(res)
    assert sum(x.moved for x in res.link.units) >= 1000


@pytest.mark.qemu
def test_v10_dynamic_identity_smoke(minimal, store, tmp_path):
    """V10 through the WP8 harness: identity artifact vs the input snapshot, one rep."""
    from scopt.validate import validate_dynamic

    qemu = (minimal.runtime.get("qemu_exe_stat") or {}).get("path")
    _need(B0_SC, Path(qemu or "/nonexistent"))
    res = optimize(minimal, "identity", store)
    doc = validate_dynamic(res.artifact_dir, [B0_SC], tmp_path / "v10",
                           baseline=minimal.path, reps=1)
    assert doc["verdict"] == "pass", doc["failures"]
    r = doc["runs"][0]
    assert r["artifact"]["appended"] == 0 and r["baseline"]["appended"] == 0
    assert r["artifact"]["output_norm_sha256"] == r["baseline"]["output_norm_sha256"]
    rows = [json.loads(x) for x in (tmp_path / "v10/ledger.jsonl").read_text().splitlines()]
    assert rows[0]["stage"] == "validated_dynamic" and rows[0]["verdict"] == "pass"
