"""WP6 real-data acceptance: a real minimal profile (WP5 ``scp`` output) ingests
with ``complete=true``.

Inputs: a run root written by WP5's ``tests/qemu/qprof_smoke.py`` (``cold/`` is
the snapshot, ``profile-K/`` the warm profile runs, each with ``run.qprof`` and
the staged ``cache/minimal.sc``). Default ``runs/wp6/real-qint-1``, made with
the integrated binary ``wt/qemu-int/build/qemu-system-arm`` (the runtime for
all B1/B2 runs); override with ``SCOPT_PROFILE_RUN_ROOT`` and
``SCOPT_PROFILE_QEMU``. Skipped when absent.

Identities are real: ``bundle_id``/``contract_id`` from WP8's identity bundle of
the run's ``b0-sc.toml`` and the FastDyn manifest the cold run wrote
(``cold/cache/minimal.sc.fastdyn.json``), and a WP8 RuntimeIdentity of the
QEMU binary, the plugin and the snapshot header. ``prologue_sha256`` and
``cpuinfo`` stay null: they come from a ``.scm`` RUN_INFO, and profile runs do
not capture.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scopt.formats import profile_v1 as P
from scopt.formats.qprof_v1 import read_qprof
from scopt.formats.sc_v5 import read_sc
from tests.conftest import FASTDYN_ROOT, MINIMAL_SC

pytestmark = pytest.mark.real

ROOT = Path(os.environ.get("SCOPT_PROFILE_RUN_ROOT", FASTDYN_ROOT / "runs/wp6/real-qint-1"))
QEMU = Path(os.environ.get("SCOPT_PROFILE_QEMU",
                           FASTDYN_ROOT / "wt/qemu-int/build/qemu-system-arm"))
HOT_VADDR = "0x8018766"
STEADY_MARKER = "0x8003972"


def _runs() -> list[Path]:
    if not (ROOT / "cold/cache/minimal.sc").exists():
        pytest.skip(f"no real profile runs under {ROOT}")
    runs = sorted(p for p in ROOT.glob("profile-*") if (p / "run.qprof").exists())
    if not runs:
        pytest.skip(f"no profile-*/run.qprof under {ROOT}")
    if not QEMU.exists():
        pytest.skip(f"no QEMU binary {QEMU}")
    return runs


def _identities() -> tuple[str, str, dict]:
    from scopt.formats.bundle_v1 import identity_bundle, runtime_identity
    from scopt.run.launcher import FASTDYN_SRC

    toml = ROOT / "b0-sc.toml"
    b = identity_bundle(toml, ROOT / "cold/cache/minimal.sc.fastdyn.json")
    plugin = FASTDYN_SRC / "build/libfastdyn.so"
    rt = runtime_identity(qemu=QEMU, plugin=plugin if plugin.exists() else None,
                          config_text=toml.read_text(),
                          sc_header=read_sc(ROOT / "cold/cache/minimal.sc").header)
    return b["bundle_id"], b["contract_id"], rt


def _ingest(run: Path, ids: tuple[str, str, dict]) -> P.Profile:
    bundle, contract, rt = ids
    return P.ingest(run / "run.qprof", run / "cache/minimal.sc", ROOT / "cold/cache/minimal.sc",
                    bundle_id=bundle, contract_id=contract, runtime_identity=rt,
                    run_id=f"{ROOT.name}/{run.name}")


def test_real_profile_ingests_complete():
    runs = _runs()
    ids = _identities()
    bundle, contract, rt = ids
    q = read_qprof(runs[0] / "run.qprof")
    assert rt["runtime_signature"] == hex(q.header.runtime_signature)
    profiles = [_ingest(r, ids) for r in runs]  # require_flow: refuses broken conservation
    for p in profiles:
        assert p.complete and p.collection["runs"][0]["clean_exit"]
        assert not p.collection["overflow"]
        assert {u["status"] for u in p.units} == {"exact"}
        assert p.domain["count"] == len(p.units) > 0
        f = P.check_flow(p)
        assert f.ok, f.violations
        assert p.total_entries() == p.collection["runs"][0]["total_dispatches"]
        assert p.runtime["qemu_sha256"] == rt["qemu_sha256"]
        assert p.sc_binding["runtime_signature"] == rt["runtime_signature"]
        steady = [ph for ph in p.phases if ph["name"] == "steady"]
        assert steady and steady[0]["reached"] and steady[0]["marker"] == STEADY_MARKER
        hot = [u for u in p.units if HOT_VADDR in u["vaddrs"]]
        assert len(hot) == 1
        boot = hot[0]["phase"]["0"]["entries"]
        assert 19000 <= boot <= 20000 and hot[0]["phase"]["1"]["entries"] == 0
    m = P.merge(profiles)
    assert m.complete and P.check_flow(m).ok
    assert len(m.collection["runs"]) == len(runs)
    with pytest.raises(P.ProfileMergeError):
        P.merge([m, profiles[0]])  # a run already in the merge
    v = P.ProfileView.open(m, ROOT / "cold/cache/minimal.sc", bundle_id=bundle,
                           contract_id=contract, runtime_identity=rt)
    assert v.same_snapshot
    counts = v.phase("steady")
    assert all(c.exact for c in counts.values())
    assert sum(c.value for c in counts.values()) == sum(
        sum(u["phase"]["1"]["entries"] for u in p.units) for p in profiles)


def test_real_profile_rejected_on_another_runtimes_sc():
    """The B0 golden ``minimal.sc`` was written by the B0 binary (another
    runtime_signature). Even with the profile's own runtime_identity_id the
    view refuses it: ``profile_incompatible:runtime``."""
    runs = _runs()
    if not MINIMAL_SC.exists():
        pytest.skip(f"{MINIMAL_SC} not present")
    ids = _identities()
    bundle, contract, rt = ids
    p = _ingest(runs[0], ids)
    b0 = read_sc(MINIMAL_SC).header
    assert b0.runtime_signature != p.runtime_signature
    with pytest.raises(P.ProfileIncompatible) as ei:
        P.ProfileView.open(p, MINIMAL_SC, bundle_id=bundle, contract_id=contract,
                           runtime_identity_id=p.runtime_identity_id)
    assert ei.value.reason == "profile_incompatible:runtime"
