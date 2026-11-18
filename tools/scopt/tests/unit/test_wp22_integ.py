"""WP22 integration stage: offline tests of the stress-test checks and of the
frozen gate binary record (plan ``## WP22``, design §5.5-5.6, R13).

The dynamic evidence itself is in ``runs/wp22/integ/`` (``wp22_stress.py``,
``wp22_ext_xcheck.py``); these tests pin the logic that turns gdb observations
into verdicts (so a vacuous or wrong check is caught without QEMU) and the
gate-binary contract that later WPs read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
QEMU_TESTS = HERE.parent / "qemu"
ROOT = Path("/data/extra_copy/fastdyn")
GATE_JSON = ROOT / "runs/wp22/gate-binary.json"


@pytest.fixture(scope="module")
def W():
    if not (ROOT / "fixtures/b3/b3.nm").is_file():
        pytest.skip("fixtures/b3 not present")
    sys.path.insert(0, str(QEMU_TESTS))
    import wp22_stress

    return wp22_stress


def _st(*, armed=0, R=1, dest=0, at_reset=True, built=0, vdest=(0, 0), invalid=False,
        forbidden=False):
    variants = [{"id": 0, "tc": 0x2000, "built": built, "general_live": 7,
                 "state": None if not built else
                 {"invalid": False, "slots": [{"dest": d, "target": 0, "reset": 0,
                                               "at_reset": not d} for d in vdest]}}]
    return {"loaded": True, "forbidden": forbidden,
            "slots": [{"i": 0, "n": 0, "R": R, "R_invalid": invalid, "dest": dest,
                       "at_reset": at_reset}],
            "variants": variants,
            "links": [{"armed": bool(armed), "disarmed_epoch": False}]}


def _summ(st):
    vs = [v for v in st["variants"] if v.get("state") and not v["state"]["invalid"]]
    return {"loaded": True, "forbidden": st["forbidden"],
            "armed": sum(1 for lk in st["links"] if lk["armed"]),
            "any_live": bool(any(s["R"] for s in st["slots"]) or
                             any(v["built"] for v in st["variants"])),
            "live_slots": sum(1 for s in st["slots"] if s["R"] and not s.get("R_invalid")),
            "variant_slots": sum(1 for v in vs for sl in v["state"]["slots"] if sl),
            "linked_variant_slots": sum(1 for v in vs for sl in v["state"]["slots"]
                                        if sl and sl["dest"]),
            "anything_live": True}


def _run(events, stats):
    return {"host": {"events": events}, "stats": stats}


def _stats(**kw):
    base = {k: 0 for k in ("disarm_tlb_events", "disarm_tlb_links", "flush_resets",
                           "disarm_flush_links", "slots_unlinked", "variant_slots_unlinked",
                           "disarm_forbid_events", "disarm_forbid_links", "invariant_checks",
                           "invariant_violations")}
    base.update(kw)
    return base


def test_event_checks_derive_the_loader_counters(W):
    b = _st(armed=1, dest=5, at_reset=False, built=0x1fc0, vdest=(9, 0))
    a = _st(armed=0, built=0x1fc0)
    ev = {"ev": "tcg_supercache_mapping_changed", "seg": 1, "b": _summ(b),
          "a": dict(_summ(a), disarm_bad=[])}
    st = _stats(disarm_tlb_events=1, disarm_tlb_links=1, slots_unlinked=1,
                variant_slots_unlinked=2, invariant_checks=1)
    items, info = W.event_checks(_run([ev], st))
    assert all(ok for _, ok, _ in items), [(c, d) for c, ok, d in items if not ok]
    assert info["derived"]["disarm_tlb_links"] == 1
    # a counter that disagrees with the observed events fails
    items, _ = W.event_checks(_run([ev], dict(st, disarm_tlb_links=0)))
    assert not all(ok for _, ok, _ in items)


def test_event_checks_fail_on_a_bad_after_state(W):
    b = _st(armed=1, dest=5, at_reset=False)
    ev = {"ev": "tcg_supercache_mapping_changed", "seg": 1, "b": _summ(b),
          "a": dict(_summ(b), disarm_bad=["slot 0 dest=5 at_reset=False"])}
    items, _ = W.event_checks(_run([ev], None))
    bad = [c for c, ok, _ in items if not ok]
    assert any("after every TLB-flush" in c for c in bad)


def test_event_checks_flush_and_forbid(W):
    b = _st(armed=1, dest=5, at_reset=False)
    flush = {"ev": "tcg_supercache_runtime_reset", "seg": 1, "b": _summ(b),
             "a": {"loaded": True, "armed": 0, "anything_live": False, "disarm_bad": []}}
    forbid = {"ev": "tcg_supercache_v6_forbid", "seg": 2,
              "b": dict(_summ(_st(R=0)), any_live=False),
              "a": {"loaded": True, "forbidden": True, "disarm_bad": []}}
    st = _stats(flush_resets=1, disarm_flush_links=1)
    items, info = W.event_checks(_run([flush, forbid], st))
    assert all(ok for _, ok, _ in items), [(c, d) for c, ok, d in items if not ok]
    # a forbid that leaves forbidden false fails
    forbid2 = dict(forbid, a={"loaded": True, "forbidden": False, "disarm_bad": []})
    items, _ = W.event_checks(_run([flush, forbid2], st))
    assert not all(ok for _, ok, _ in items)
    # a flush that leaves a record live fails
    flush2 = dict(flush, a={"loaded": True, "armed": 0, "anything_live": True})
    items, _ = W.event_checks(_run([flush2], st))
    assert not all(ok for _, ok, _ in items)


def test_disarmed_ok_full_state(W):
    assert W._disarmed_ok(_st()) == []
    assert W._disarmed_ok(_st(armed=1))
    assert W._disarmed_ok(_st(dest=3, at_reset=False))
    assert W._disarmed_ok(_st(built=0x1fc0, vdest=(0, 7)))
    # an invalidated R keeps its own slot (G9): not a violation
    assert W._disarmed_ok(_st(dest=3, at_reset=False, invalid=True)) == []


def test_fixture_call_sites(W):
    assert W.call_returns("phase_b3_14", "b3_res") == [0x980, 0x988]
    S = W.nm()
    assert S["vc_b3_1"] == 0x2F4 and S["f_b3_1"] == 0x10000
    names = [s.name for s in W.art_specs()]
    assert names == ["b3-1", "b3-1-general", "b3-1-identity", "b3-6r", "b3-12", "b3-13",
                     "b3-14", "b3-14g"]
    # b3_res masks interrupts around log_pair (WP24 review 3): R/C come from the
    # listing, not from fixed offsets
    assert W.insn_addrs("b3_res", "pop") == [S["b3_res"] + 0x14]
    assert W.insn_addrs("b3_res", "msr") == [S["b3_res"] + 0x10]
    assert W.call_sites("phase_b3_14", "f_b3_14") == [S["vc_b3_14"] - 6]
    spec = {s.name: s for s in W.art_specs()}
    assert [(g.r, g.targets) for g in spec["b3-14g"].grows] == [
        (S["vc_b3_14"] - 6, (("SPEC", S["f_b3_14"]),)),
        (S["f_b3_14"], (("GENERAL", S["vc_b3_14"]),))]
    assert spec["b3-14"].grows[1].r == 0x96


def test_gate_binary_record():
    if not GATE_JSON.is_file():
        pytest.skip("runs/wp22/gate-binary.json not written yet (WP22 freeze)")
    g = json.loads(GATE_JSON.read_text())
    sha = g["exe_sha256"].removeprefix("sha256:")
    assert len(sha) == 64 and g["tag"] == "qint-v6-gate-1"
    from scopt.validate import v6_capable_exes

    assert sha in v6_capable_exes()
    exe = Path(g["exe_path"])
    if exe.is_file():
        import hashlib

        h = hashlib.sha256()
        with open(exe, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        assert h.hexdigest() == sha, "the gate binary changed after the freeze"
    repo = Path(g["worktree"])
    if (repo / ".git").exists():
        c = subprocess.run(["git", "-C", str(repo), "rev-parse", "qint-v6-gate-1^{commit}"],
                           capture_output=True, text=True)
        assert c.returncode == 0 and c.stdout.strip() == g["commit"]
    from scopt.formats.envlayout_v1 import find_envlayout

    lay = find_envlayout("sha256:" + sha)
    assert lay is not None, "the gate binary's EnvLayout is packaged"
