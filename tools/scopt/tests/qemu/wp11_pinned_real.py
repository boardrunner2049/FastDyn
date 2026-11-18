#!/usr/bin/env python3
"""WP11 real-data check of the pinned-unit rules (§9; the F-B2-4 shape on real files).

    wp11_pinned_real.py --gate GATE_DIR --profile PROFILE.json --out OUT

The real snapshots of the B1 gate have no pinned unit, so the rules "pinned /
not relocatable units are never in an order request" and "layout reports them
as ``skipped_unsupported:<reason>`` in ``affected``" are only exercised by
synthetic tests there. This driver makes one real pinned *hot* unit:

1. copies the gate's prep ``.sc`` (sparse), its manifest and its capture into
   ``OUT/prep`` and removes one HELPER_CALL site from the capture of the
   hottest unit (steady entries of ``PROFILE``) that has one, as T-B1-9's
   ``unexplained_reference`` case does; the ``.sc`` is unchanged, so the
   SnapshotId and the profile still apply;
2. ``scopt import`` into a new store ``OUT/store`` (the unit must be pinned);
3. ``b2-layout`` and its ablations via :mod:`wp11_b2_real` (optimize,
   re-validate, V10, report) on that store;
4. checks: the unit is classified hot, never moved, listed in the sidecar's
   ``pinned``, and reported as ``skipped_unsupported:unexplained_reference:<uid>``
   in ``affected`` of ``cluster-hot`` (full pipeline) and of ``order-cold``
   (``--disable cluster-hot``).

Nothing under the gate directory is written. No timing is measured.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))
from scopt.formats import scm_v1 as S  # noqa: E402
from scopt.formats.enums import SiteKind  # noqa: E402
from scopt.formats.sc_v5 import sparse_copy  # noqa: E402
from scopt.model.cache_snapshot import snapshot_dir_name  # noqa: E402
from tests.synth import builders as B  # noqa: E402

_spec = importlib.util.spec_from_file_location("wp11_b2_real", Path(__file__).with_name(
    "wp11_b2_real.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gate", required=True, help="B1 gate workload dir (read-only)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workload", default="minimal", choices=sorted(R.WORKLOADS))
    ap.add_argument("--qemu", default=str(R.INT_QEMU))
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args(argv)
    gate, out = Path(a.gate), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gate_before = R.tree_digest(gate / "prep")

    pj = json.loads((gate / "prep" / "prep.json").read_text())
    sc_src, man_src = Path(pj["sc"]), Path(pj["manifest"])
    scm_src = Path(pj["captures"][0])
    prof = json.loads(Path(a.profile).read_text())
    steady_id = next(str(p["id"]) for p in prof["phases"] if p["name"] == "steady")
    entries = {u["unit"]: int((u.get("phase", {}).get(steady_id) or {}).get("entries", 0))
               for u in prof["units"] if u.get("status") == "exact"}

    # map slot -> UnitId through an import of the untouched files
    from scopt.importer.import_cache import import_snapshot

    clean = out / "prep-clean"
    clean.mkdir(exist_ok=True)
    sparse_copy(sc_src, clean / sc_src.name, fsync=False)
    shutil.copyfile(man_src, clean / man_src.name)
    shutil.copyfile(scm_src, clean / "capture.scm")
    snap0 = import_snapshot(str(clean)).snapshot
    uid_of = {u.slot: u.uid for u in snap0.units()}

    data = scm_src.read_bytes()
    best = None
    for ch in S.parse_scm(data).chunks:
        if not isinstance(ch.body, S.TbChunk):
            continue
        tb = ch.body
        for i, site in enumerate(tb.sites):
            if site.kind == SiteKind.HELPER_CALL:
                uid = uid_of.get(tb.slot)
                n = entries.get(uid, 0)
                if uid and (best is None or (n, uid) > (best[0], best[2])):
                    best = (n, tb.slot, uid, i)
                break
    if best is None:
        print("no HELPER_CALL site in the capture", file=sys.stderr)
        return 2
    n, slot, uid, site_i = best
    prep = out / "prep"
    prep.mkdir(exist_ok=True)
    sparse_copy(sc_src, prep / sc_src.name, fsync=False)
    shutil.copyfile(man_src, prep / man_src.name)
    (prep / "capture.scm").write_bytes(B.drop_site(data, slot, site_i))
    shutil.rmtree(clean)

    drv_args = argparse.Namespace(workload=a.workload, store=str(out / "store"),
                                  snapshot=None, profile=a.profile, out=str(out),
                                  gate_store=None, boot_profile=None, qemu=a.qemu,
                                  timeout=a.timeout)
    d = R.Driver(drv_args)
    d.log(f"tampered: slot {slot} {uid} (steady entries {n}), HELPER_CALL site #{site_i} "
          "removed from the capture")
    rc, txt, _ = d.scopt("import", "import", prep, "--bundle", gate / "bundle.json",
                         "--runtime-identity", gate / "runtime_identity.json", "--store",
                         out / "store", "--json")
    imp = json.loads(txt)
    sid = imp.get("snapshot_id", "")
    drv_args.snapshot = snapshot_dir_name(sid)
    rc_run = d.run()
    res = json.loads((out / "wp11.json").read_text())

    checks = []

    def check(name: str, ok: bool, ev: object) -> None:
        checks.append({"check": name, "ok": bool(ok), "evidence": ev})

    check("import rc 0", rc == 0, rc)
    check("importer pins exactly one unit (unexplained_reference)",
          (imp.get("status") or {}).get("pinned") == 1 and any(
              k.startswith("unexplained_reference") for k in imp.get("pin_reasons") or {}),
          {"status": imp.get("status"), "pin_reasons": imp.get("pin_reasons")})
    want = f"skipped_unsupported:unexplained_reference:{uid}"
    for r in res["candidates"]:
        art = r.get("artifact_dir")
        if not art:
            check(f"{r['label']}: published", False, r.get("reason"))
            continue
        side = json.loads((Path(art) / "cache.sc.scopt.json").read_text())
        pinned = {p["unit"]: p.get("reasons") for p in side["pinned"]}
        pp = {p["pass"]: p for p in side["pipeline"]}
        check(f"{r['label']}: unit in sidecar pinned", uid in pinned, pinned.get(uid))
        check(f"{r['label']}: unit not moved", uid not in side["move_map"],
              side["move_map"].get(uid))
        if r["label"] == "b2-layout":
            check("b2-layout: cluster-hot affected has " + want,
                  want in (pp["cluster-hot"].get("affected") or []),
                  [x for x in pp["cluster-hot"].get("affected") or []
                   if x.startswith("skipped_")])
        if r["label"] == "no-cluster-hot":
            check("no-cluster-hot: order-cold affected has " + want,
                  want in (pp["order-cold"].get("affected") or []),
                  [x for x in pp["order-cold"].get("affected") or []
                   if x.startswith("skipped_")])
    gate_after = R.tree_digest(gate / "prep")
    check("gate prep unchanged", gate_before == gate_after, len(gate_before))
    doc = {"schema": "scopt.wp11_pinned_real/1", "unit": uid, "slot": slot,
           "steady_entries": n, "site_index": site_i, "snapshot_id": sid,
           "import": {k: imp.get(k) for k in ("status", "pin_reasons", "snapshot_id")},
           "driver_verdict": res["verdict"], "checks": checks,
           "verdict": "pass" if rc_run == 0 and all(c["ok"] for c in checks) else "fail"}
    (out / "wp11_pinned.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    d.log(f"pinned verdict {doc['verdict']}")
    return 0 if doc["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
