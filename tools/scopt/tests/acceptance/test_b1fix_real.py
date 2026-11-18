"""B1FIX real-data checks (minimal gate files, read-only; skipped when absent).

* (a) the real minimal prep ``.sc`` with record 5's checksum broken (a
  non-canonical input whose dropped record is not the last slot): the identity
  pipeline publishes with V9 ``identity_canonicalized`` and the dropped list,
  the derived ``cache.scm`` binds every remaining unit at its output slot, and
  a unit after the dropped record can be moved (V1-V8 pass).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from scopt.formats import artifact as A
from scopt.formats import scm_v1 as S
from scopt.formats.sc_v5 import RECORD_SIZE, read_sc, sparse_copy
from scopt.importer.import_cache import import_snapshot, open_snapshot
from scopt.link import round_up
from scopt.manager import PassManager, default_pass_registry
from scopt.validate import ValidateAndPackage, optimize, validate_artifact
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
GATE = Path(os.environ.get("B1_GATE_DIR", ROOT / "runs/wp14/gate/minimal"))
SC_CODE_OFFSET = 0x10000
BAD = 5

pytestmark = [pytest.mark.real, pytest.mark.slow]


@pytest.fixture(scope="module")
def noncanonical(tmp_path_factory):
    prep = GATE / "prep"
    need = [prep / "prep.json", prep / "capture.scm", GATE / "bundle.json",
            GATE / "runtime_identity.json"]
    for p in need:
        if not p.exists():
            pytest.skip(f"{p} not present (run tests/qemu/b1_gate.py --workload minimal)")
    pj = json.loads((prep / "prep.json").read_text())
    tmp = tmp_path_factory.mktemp("b1fix-real")
    d = tmp / "p"
    d.mkdir()
    sc = d / Path(pj["sc"]).name
    sparse_copy(pj["sc"], sc, fsync=False)
    shutil.copyfile(prep / "capture.scm", d / "capture.scm")
    shutil.copyfile(pj["manifest"], str(sc) + ".fastdyn.json")
    h = read_sc(sc).header
    off = SC_CODE_OFFSET + h.code_size + BAD * RECORD_SIZE + 64  # guest_hash byte
    fd = os.open(sc, os.O_RDWR)
    try:
        b = os.pread(fd, 1, off)
        os.pwrite(fd, bytes([b[0] ^ 0x01]), off)
    finally:
        os.close(fd)
    store = tmp / "store"
    res = import_snapshot(str(d), bundle=str(GATE / "bundle.json"),
                          runtime_identity=str(GATE / "runtime_identity.json"), store=str(store))
    assert res.summary["records"]["skipped"] == [{"slot": BAD, "reason": "record_checksum"}]
    return open_snapshot(res.store_dir), store, h.record_count


def test_real_identity_on_noncanonical_input(noncanonical):
    snap, store, n = noncanonical
    assert len(snap.units()) == n - 1 and BAD not in [u.slot for u in snap.units()]
    res = optimize(snap, "identity", store)
    assert res.published, (res.reason, res.validation and res.validation.fails())
    assert res.validation.codes() == {f"V{i}": "pass" for i in range(1, 10)}
    v9 = [r for r in res.validation.rows if r["code"] == "V9"][0]
    assert v9["status"] == "identity_canonicalized"
    assert v9["dropped"] == [{"slot": BAD, "reason": "record_checksum"}]
    art = Path(res.artifact_dir)
    tbs = S.read_scm(art / "cache.scm").tbs
    assert sorted(tbs) == list(range(n - 1))  # output slots, packed
    again = import_snapshot(sc=str(art / "cache.sc"), scms=[str(art / "cache.scm")])
    assert again.join.histogram() == {"bound": n - 1}
    assert again.snapshot.snapshot_id == res.output_snapshot_id
    rv = validate_artifact(art, snap)
    assert rv["verdict"] == "accepted" and rv["stored_codes_agree"], rv["codes"]


def test_real_move_after_dropped_record(noncanonical):
    snap, store, _n = noncanonical
    u = next(x for x in snap.units() if x.slot > BAD and x.reloc.relocatable)
    d = round_up(snap.max_end, 64) - u.tb_offset + 64
    pipe = {"schema": "scopt.pipeline", "version": 1, "name": "test",
            "allowed_capabilities": ["PLACE"],
            "pass": [{"name": "move-units", "version": 1, "params": {"deltas": {u.uid: d}}}]}
    cand = PassManager(snap, pipe, passes=default_pass_registry()).execute()
    res = ValidateAndPackage(cand, store).run()
    assert res.published, (res.reason, res.validation and res.validation.fails())
    c = res.validation.codes()
    assert all(c[f"V{i}"] == "pass" for i in range(1, 9)) and c["V9"] == "skip", c
    side = json.loads((Path(res.artifact_dir) / A.SIDECAR).read_text())
    assert side["move_map"][u.uid]["slot"] == u.slot - 1  # output slot
