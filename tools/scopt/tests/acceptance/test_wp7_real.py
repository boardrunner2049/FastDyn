"""WP7 real-data acceptance: T-B1-3, T-B1-4, T-B1-8 and re-import determinism.

Inputs (made once, outside the repo; the tests skip when they are absent).
``W7`` is ``$WP7_REAL_DIR`` or ``runs/wp7-r2`` (made with the integrated
WP-QINT binary ``wt/qemu-int/build/qemu-system-arm``, sha256 8864c726…;
``runs/wp7`` holds the first round made with the WP3 capture binary):

* ``W7/prep-minimal``: a capture-on cold run of ``configs/b0-sc.toml``
  (``tests/qemu/wp7_prep_capture.py``): ``cache/minimal.sc``, its FastDyn
  manifest and ``capture.scm``;
* ``W7/minimal-bundle.json``: ``scopt bundle identity -c prep-minimal/run.toml
  --manifest prep-minimal/cache/minimal.sc.fastdyn.json``;
* ``W7/minimal-runtime-identity.json``: ``scopt bundle runtime-identity`` of
  the deployed warm run ``W7/t-b1-4/warm`` with ``--sc`` the prep ``.sc`` and
  ``--scm`` its capture;
* ``W7/t-b1-4/diag/diag.json``: ``scopt diag exec`` (WP8) of that warm run on
  the imported snapshot's ``cache.sc``; its ``Linking TBs`` lines are the
  goto_tb oracle;
* FreeRTOS MPS2 captures: ``runs/qint/t-b1-1-freertos`` (integrated binary,
  T-B1-1 on1/on2) and ``runs/wp3/freertos-cap1`` (WP3 binary), 1013 TBs each.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scopt.formats.region_v1 import validate_region
from scopt.graph import check_graph, export_graph, graph_bytes, load_graph, roundtrip_ok
from scopt.importer.import_cache import G17, import_snapshot, open_snapshot
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
W7 = Path(os.environ.get("WP7_REAL_DIR", ROOT / "runs/wp7-r2"))
PREP = W7 / "prep-minimal"
BUNDLE = W7 / "minimal-bundle.json"
RI = W7 / "minimal-runtime-identity.json"
DIAG = W7 / "t-b1-4/diag/diag.json"
QINT_FRT = ROOT / "runs/qint/t-b1-1-freertos"
FRT_FW = ROOT / ("fixtures/freertos-mps2/FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/"
                 "build/gcc/output/RTOSDemo.out")
PLUGIN = ROOT / "src/fastdyn/build/libfastdyn.so"
# name -> (.sc, captures, explicit firmware/plugin when the .sc has no manifest)
FREERTOS = {
    "qint-on1+on2": (QINT_FRT / "archive/on1.sc", [QINT_FRT / "scm/on1.scm",
                                                  QINT_FRT / "scm/on2.scm"],
                     {"firmware": str(FRT_FW), "plugin": str(PLUGIN)}),
    "wp3-cap1": (ROOT / "runs/wp3/freertos-cap1/cache/freertos.sc",
                 [ROOT / "runs/wp3/freertos-cap1.scm"], {}),
}
WP3_MINIMAL_SCM = ROOT / "runs/wp7/prep-minimal/capture.scm"  # WP3 binary: other signature

pytestmark = [pytest.mark.real, pytest.mark.slow]


def _need(*paths: Path) -> None:
    for p in paths:
        if not p.exists():
            pytest.skip(f"{p} not present")


@pytest.fixture(scope="module")
def minimal(tmp_path_factory):
    _need(PREP / "capture.scm", BUNDLE, RI)
    store = tmp_path_factory.mktemp("wp7store")
    return import_snapshot(str(PREP), bundle=str(BUNDLE), runtime_identity=str(RI),
                           store=str(store))


def test_t_b1_3_import_crosscheck(minimal):
    s = minimal.summary
    assert s["records"]["valid"] == 163 and s["records"]["skipped"] == []
    assert s["capture"]["binding"] == {"bound": 163}  # 100% of slots bind
    assert s["capture"]["complete"] is True
    assert s["status"] == {"no_meta": 0, "ok_relocatable": 163, "pinned": 0}
    reasons = s["pin_reasons"]
    for code in ("unexplained_reference", "missing_reference", "recovery_mismatch"):
        assert reasons.get(code, 0) == 0
    assert s["tallies"] == G17  # 394 / 3 / 3 / 316 / 3 / 316 (+0:105,+1:50,+3:161) / 336 / 52 / 0
    # the CF_NOIRQ (io-recompile) TBs are ok_relocatable
    snap = minimal.snapshot
    noirq = [u for u in snap.units() if u.key.cflags & 0x10000]
    assert noirq and all(snap.status(u.uid) == "ok_relocatable" for u in noirq)
    # helper symbols equal the HELPER names where non-NULL
    hs = s["helper_symbols"]
    assert hs["checked"] > 0 and hs["ok"] == hs["checked"] and hs["mismatch"] == []
    assert s["bundle_id"] == json.loads(BUNDLE.read_text())["bundle_id"]
    # every manifest-derivable bundle field and the RuntimeIdentity were checked
    ic = s["identity_checks"]
    assert ic["bundle"]["unverified"] == ["memory_map_hash"]
    assert {"image_hash", "patch_plan_hash", "contract_id", "instrumentation_hash"} <= \
        set(ic["bundle"]["verified"])
    rv = set(ic["runtime_identity"]["verified"])
    assert {"runtime_signature@sc", "prologue_sha256@run_info", "icache_linesize@run_info",
            "cpuinfo@run_info", "qemu_exe_stat.size@run_info", "qemu_sha256@file",
            "plugin_sha256@file"} <= rv
    assert ic["runtime_identity"]["unverified"] == []
    for r in minimal.regions:
        assert validate_region(r) == []


def test_t_b1_8_recovery_oracle(minimal):
    checks = [c for uc in minimal.checks.values() for c in uc.recovery_checks]
    assert len(checks) == 336 + 58 + 3  # every raddr and every helper-call return
    assert all(c.ok for c in checks)
    assert sum(len(u.recovery) for u in minimal.snapshot.units()) == len(checks)


def test_t_b1_4_graph_roundtrip_and_linking_oracle(minimal):
    snap = minimal.snapshot
    doc = export_graph(snap)
    data = graph_bytes(doc)
    assert roundtrip_ok(data)
    assert check_graph(load_graph(data), snap) == []
    for e in doc["edges"]:
        assert "dst" not in e and e["exit"].get("id")
        if e["exit"]["kind"] != "goto_tb":
            assert e["resolution"] == "dynamic"
    # edges of one source are in numeric exit order (x2 before x10)
    from scopt.graph import _exit_order
    last: dict[str, tuple] = {}
    for e in doc["edges"]:
        k = _exit_order(e["exit"].get("id"))
        assert last.get(e["src"], (-1,)) <= k
        last[e["src"]] = k
    # every goto_tb exit of a captured unit is an edge
    n_goto = sum(1 for u in snap.units() for s in u.sites if s.kind.name == "GOTO_TB")
    assert sum(1 for e in doc["edges"] if e["exit"]["kind"] == "goto_tb") == n_goto

    _need(DIAG)
    diag = json.loads(DIAG.read_text())
    assert diag["exit_code"] == 0 and diag["all_traces_in_snapshot"]
    by_slot = {u.slot: u for u in snap.units()}
    static = {(e["src"], e["exit"]["slot"]): e for e in doc["edges"]
              if e["exit"]["kind"] == "goto_tb"}
    assert diag["linking"]
    for ln in diag["linking"]:  # every runtime link is a static edge with dst a candidate
        src, dst = by_slot[ln["src_slot"]], by_slot[ln["dst_slot"]]
        e = static[(src.uid, ln["index"])]
        assert dst.uid in e["candidates"], ln
    # the two-TB pair from the first Linking line
    ln = diag["linking"][0]
    a, b = by_slot[ln["src_slot"]], by_slot[ln["dst_slot"]]
    pair = export_graph(snap, units=[a.uid, b.uid])
    pb = graph_bytes(pair)
    assert roundtrip_ok(pb) and check_graph(load_graph(pb), snap) == []
    e = next(x for x in pair["edges"] if x["src"] == a.uid and x["exit"]["kind"] == "goto_tb"
             and x["exit"]["slot"] == ln["index"])
    assert e["candidates"] == [b.uid] and e["resolution"] == "candidates_unique"


def test_reimport_byte_deterministic(minimal, tmp_path):
    again = import_snapshot(str(PREP), bundle=str(BUNDLE), runtime_identity=str(RI),
                            store=str(tmp_path))
    d1, d2 = Path(minimal.store_dir), Path(again.store_dir)
    f1 = sorted(str(p.relative_to(d1)) for p in d1.rglob("*") if p.is_file())
    f2 = sorted(str(p.relative_to(d2)) for p in d2.rglob("*") if p.is_file())
    assert f1 == f2
    from scopt.formats.sc_v5 import sha256_sparse

    for f in f1:
        if f == "cache.sc":
            assert sha256_sparse(d1 / f) == sha256_sparse(d2 / f) == sha256_sparse(
                PREP / "cache/minimal.sc")
        else:
            assert (d1 / f).read_bytes() == (d2 / f).read_bytes(), f
    assert open_snapshot(d1).snapshot_id == minimal.snapshot.snapshot_id


@pytest.mark.parametrize("name", sorted(FREERTOS))
def test_freertos_capture_imports(name):
    sc, scms, kw = FREERTOS[name]
    _need(sc, *scms, *(Path(v) for v in kw.values()))
    res = import_snapshot(sc=str(sc), scms=[str(x) for x in scms], **kw)
    s = res.summary
    assert s["capture"]["binding"] == {"bound": s["records"]["valid"]} and s["records"]["valid"] > 1000
    assert s["status"]["ok_relocatable"] == s["records"]["valid"]
    assert s["pin_reasons"] == {}
    assert s["origins"] == {"elf_verified": s["records"]["valid"]}
    doc = export_graph(res.snapshot)
    assert roundtrip_ok(graph_bytes(doc))


def test_foreign_capture_does_not_bind_or_reject(tmp_path):
    """Leftover captures next to the prep run (§2.5.8) are reported and left
    out; the snapshot imports exactly as without them.

    * FreeRTOS on1.scm: same binary, so its RUN_INFO agrees with the header,
      but none of its chunks binds (``no_chunk_binds``);
    * the round-1 minimal capture of the WP3 binary: other runtime_signature
      (``run_info_mismatch:runtime_signature``).
    """
    from scopt.importer.import_cache import ImportRejected

    frt = FREERTOS["qint-on1+on2"][1][0]
    _need(PREP / "capture.scm", frt, WP3_MINIMAL_SCM)
    base = import_snapshot(sc=str(PREP / "cache/minimal.sc"), scms=[str(PREP / "capture.scm")])
    res = import_snapshot(sc=str(PREP / "cache/minimal.sc"),
                          scms=[str(PREP / "capture.scm"), str(frt), str(WP3_MINIMAL_SCM)])
    s = res.summary
    assert s["capture"]["binding"] == {"bound": 163}
    assert s["status"]["ok_relocatable"] == 163
    codes = {(p["file"], p["code"]) for p in s["capture"]["problems"]}
    assert codes == {(frt.name, "no_chunk_binds"),
                     (WP3_MINIMAL_SCM.name, "run_info_mismatch:runtime_signature")}
    assert s["capture"]["files"] == base.summary["capture"]["files"]
    assert res.regions == base.regions
    # alone, a capture with another RUN_INFO rejects the snapshot (T-B1-9 RUN_INFO case)
    with pytest.raises(ImportRejected) as e:
        import_snapshot(sc=str(PREP / "cache/minimal.sc"), scms=[str(WP3_MINIMAL_SCM)])
    assert e.value.reason == "run_info_mismatch:runtime_signature"
    # alone, a same-binary capture of another .sc: every slot no_meta (pinned)
    only = import_snapshot(sc=str(PREP / "cache/minimal.sc"), scms=[str(frt)])
    assert only.summary["status"] == {"no_meta": 163, "ok_relocatable": 0, "pinned": 0}
