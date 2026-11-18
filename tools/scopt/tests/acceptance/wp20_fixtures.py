"""WP20 integration acceptance on Tier-0 snapshots of the WP24 fixtures (``fixtures/b3``).

    python -m tests.acceptance.wp20_fixtures FX_ROOT [--out VERDICT_DIR]

``FX_ROOT`` is the output of ``tests/qemu/wp20_fixture_snapshots.py``
(``FX_ROOT/<mode>/<config>/snap.json``). For every snapshot this writes certified
CalleeSummary rows (WP19 V14 certifier plugged in, as ``return-edges summaries
--certify``) and the two-sided proposer/hostsym cross-check
(:mod:`scopt.analysis.fact_xcheck`), then evaluates the WP20 integration criteria of
``design/IMPLEMENTATION-PLAN-B3B4.md`` §WP20:

* ``patch_fact``: on F-B3-1/1p/2/3/3b/4/8 every return unit whose return
  instruction carries the ``r0 <- 0`` modifier has ``r0 == 0`` with
  ``source = patch`` (plan entry at that vaddr) and ``status = certified``;
* ``f5``: on F-B3-6 the unpatched ``f5`` (``f_b3_6b``) gets no ``r0 == 0`` fact and
  no ``source = patch`` fact (its own ``movs r0,#5`` fact is reported), while the
  patched ``f0`` (``f_b3_6a``) has the certified patch fact;
* ``killed``: on F-B3-5 the return units of ``f_b3_5`` refuse ``r0`` with
  ``fact_killed_by_helper`` and carry no ``r0`` fact;
* ``suffix``: on F-B3-20 the full unit (starts at ``f_b3_20``, contains
  ``movs r0,#1``) has ``r0 == 1`` (``guest_code``, certified) and every suffix unit
  (starts after ``f_b3_20``, ends at ``f_b3_20_ret``) refuses ``r0`` with
  ``fact_not_local_to_return_tb``; the count of suffix units present is reported
  (non-vacuous only when > 0);
* ``agree``: on every snapshot the certifier tally has no
  ``fact_derivation_mismatch`` and the cross-check has ``mismatch == 0``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from scopt.paths import fastdyn_root

FX = (fastdyn_root() / "fixtures/b3")
PATCH_FACT = {"1": ["f_b3_1_ret"], "1p": ["f_b3_1p_ret"], "2": ["f_b3_2_ret"],
              "3": ["f_b3_3_ret"], "3b": ["f_b3_3b_ret", "f_b3_3c_ret"], "4": ["f_b3_4_ret"],
              "8": ["f_b3_8_ret"]}


def _b3conf():
    if str(FX) not in sys.path:
        sys.path.insert(0, str(FX))
    import b3conf  # noqa: PLC0415
    return b3conf


def analyse(snap: dict[str, Any], out: Path | None = None) -> dict[str, Any]:
    """Certified summaries + cross-check of one captured fixture snapshot."""
    from scopt.analysis.fact_xcheck import xcheck_snapshot
    from scopt.commands.b3_edges import Ctx
    from scopt.formats.callee_summary_v1 import DEFAULT_RELPATH, write_callee_summaries

    c = Ctx(snap["snapshot"], profile=snap.get("profile"), elf=None, prep=snap["prep"],
            modifiers=None, virtuals=None)
    cert = c.certifier()
    rows = c.summaries(certify=cert)
    xc = xcheck_snapshot(c.s.units(), c.facts, c.s, c.layout)
    units = {u.uid: {"start": u.vaddrs[0] if u.vaddrs else None, "icount": u.icount,
                     "guest_size": u.guest_size, "cflags": u.key.cflags}
             for u in c.s.units()}
    edges = {(r.uid, r.exit_id): r for r in c.edges().exits}
    res = {"snapshot": c.s.snapshot_id, "rows": len(rows),
           "facts": sum(len(r["facts"]) for r in rows),
           "certifier": dict(sorted(cert.tally.items())),
           "sources": dict(Counter(f["source"] for r in rows for f in r["facts"])),
           "refused": dict(Counter(x["reason"] for r in rows for x in r["refused"])),
           "xcheck": {k: v for k, v in xc.items() if k != "rows"}}
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        res["summaries"] = write_callee_summaries(out / DEFAULT_RELPATH, rows)["path"]
        (out / "xcheck.json").write_text(json.dumps(xc, indent=1, sort_keys=True))
    return {"res": res, "rows": rows, "units": units, "edges": edges, "xcheck": xc}


def _rows_at(a: dict[str, Any], vaddr: int) -> list[dict[str, Any]]:
    return [r for r in a["rows"] if int(r["return_insn_vaddr"], 16) == vaddr]


def _fact(r: dict[str, Any], field: str) -> dict[str, Any] | None:
    return next((f for f in r["facts"] if f["field"] == field), None)


def _refusal(r: dict[str, Any], field: str) -> list[str]:
    return [x["reason"] for x in r["refused"] if x.get("field") == field]


def _brief(r: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
    u = a["units"].get(r["return_unit"], {})
    e = a["edges"].get((r["return_unit"], r["exit_id"]))
    return {"unit": r["return_unit"], "start": None if u.get("start") is None else
            f"0x{u['start']:x}", "icount": u.get("icount"),
            "cflags": None if u.get("cflags") is None else f"0x{u['cflags']:x}",
            "return_insn": r["return_insn"], "admitted": r["admitted"],
            "exit_reasons": [x["code"] for x in r["exit_reasons"]],
            "top_candidate": None if e is None or not e.candidates or
            e.candidates[0].vaddr is None else
            f"0x{e.candidates[0].vaddr:x} n={e.candidates[0].count}",
            "facts": [f"{f['field']}=={f['value']} {f['source']} {f['status']}"
                      + (f" plan@{f['plan_entry']['vaddr']}" if f.get("plan_entry") else "")
                      for f in r["facts"]],
            "refused_r0": _refusal(r, "r0")}


def check_patch_fact(cfg: str, a: dict[str, Any]) -> dict[str, Any]:
    b = _b3conf()
    elf = b.BY_NAME[cfg].elf
    out: dict[str, Any] = {"sites": []}
    ok = True
    for sym in PATCH_FACT[cfg]:
        v = b.resolve(elf, sym)
        rows = _rows_at(a, v)
        good = []
        for r in rows:
            f = _fact(r, "r0")
            good.append(f is not None and int(f["value"], 16) == 0 and f["source"] == "patch"
                        and f["status"] == "certified" and f.get("cert_hash")
                        and int(f["plan_entry"]["vaddr"], 16) == v)
        site_ok = bool(rows) and all(good)
        ok &= site_ok
        out["sites"].append({"symbol": sym, "vaddr": f"0x{v:x}", "units": len(rows),
                             "pass": site_ok, "rows": [_brief(r, a) for r in rows]})
    out["pass"] = ok
    return out


def check_f5(a: dict[str, Any]) -> dict[str, Any]:
    b = _b3conf()
    f5 = _rows_at(a, b.resolve("b3.elf", "f_b3_6b_ret"))
    f0 = _rows_at(a, b.resolve("b3.elf", "f_b3_6a_ret"))
    f5_ok = bool(f5) and all(
        not any(f["source"] == "patch" for f in r["facts"]) and
        not any(f["field"] == "r0" and int(f["value"], 16) == 0 for f in r["facts"])
        for r in f5)
    f0_ok = bool(f0) and all((_fact(r, "r0") or {}).get("source") == "patch" and
                             _fact(r, "r0")["status"] == "certified" for r in f0)
    bxr3 = b.resolve("b3.elf", "b3_6_bxr3")
    bx = [{"unit": r.uid, "exit_class": r.exit_class, "class_detail": r.class_detail,
           "admitted": r.admitted, "reasons": list(r.reason_codes)}
          for r in a["edges"].values() if r.exit_vaddr == bxr3]
    return {"pass": f5_ok and f0_ok, "f5_pass": f5_ok, "f0_pass": f0_ok,
            "f5_rows": [_brief(r, a) for r in f5], "f0_rows": [_brief(r, a) for r in f0],
            "bx_r3_exits": bx}


def check_killed(a: dict[str, Any]) -> dict[str, Any]:
    b = _b3conf()
    rows = _rows_at(a, b.resolve("b3.elf", "f_b3_5_ret"))
    ok = bool(rows) and all(_fact(r, "r0") is None and
                            "fact_killed_by_helper" in _refusal(r, "r0") for r in rows)
    return {"pass": ok, "units": len(rows), "rows": [_brief(r, a) for r in rows],
            "xcheck_r0": [x for x in a["xcheck"]["rows"] if x["field"] == "r0" and
                          x["uid"] in {r["return_unit"] for r in rows}]}


def check_suffix(a: dict[str, Any]) -> dict[str, Any]:
    b = _b3conf()
    start = b.resolve("b3.elf", "f_b3_20") & ~1
    ret = b.resolve("b3.elf", "f_b3_20_ret")
    rows = _rows_at(a, ret)
    full, suffix, other = [], [], []
    for r in rows:
        s = a["units"].get(r["return_unit"], {}).get("start")
        (full if s == start else suffix if s is not None and start < s <= ret else other).append(r)
    full_ok = bool(full) and all((_fact(r, "r0") or {}).get("value") == "0x1" and
                                 _fact(r, "r0")["source"] == "guest_code" and
                                 _fact(r, "r0")["status"] == "certified" for r in full)
    suf_ok = all(_fact(r, "r0") is None and
                 "fact_not_local_to_return_tb" in _refusal(r, "r0") for r in suffix)
    return {"pass": full_ok and suf_ok and not other, "non_vacuous": bool(suffix),
            "full_pass": full_ok, "suffix_pass": suf_ok, "suffix_units": len(suffix),
            "full": [_brief(r, a) for r in full], "suffix": [_brief(r, a) for r in suffix],
            "other": [_brief(r, a) for r in other]}


REQUIRED = tuple(PATCH_FACT) + ("5", "6", "20")


def evaluate(fx_root: Path, out: Path | None = None,
             configs: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Evaluate every snapshot under ``fx_root`` (or only ``configs``)."""
    snaps = [json.loads(p.read_text()) for p in sorted(fx_root.glob("*/*/snap.json"))]
    if configs is not None:
        snaps = [s for s in snaps if s["config"] in configs]
    per: list[dict[str, Any]] = []
    crit: dict[str, Any] = {"patch_fact": {}, "f5": {}, "killed": {}, "suffix": {}}
    missing = []
    for s in snaps:
        tag = f"{s['mode']}/{s['config']}"
        if not s.get("snapshot"):
            missing.append(tag)
            continue
        a = analyse(s, None if out is None else out / s["mode"] / s["config"])
        r = a["res"]
        r.update(mode=s["mode"], config=s["config"], fixture=s.get("fixture"))
        per.append(r)
        if s["config"] in PATCH_FACT:
            crit["patch_fact"][tag] = check_patch_fact(s["config"], a)
        elif s["config"] == "6":
            crit["f5"][tag] = check_f5(a)
        elif s["config"] == "5":
            crit["killed"][tag] = check_killed(a)
        elif s["config"] == "20":
            crit["suffix"][tag] = check_suffix(a)
    cert_mism = sum(r["certifier"].get("fact_derivation_mismatch", 0) for r in per)
    xc_mism = sum(r["xcheck"]["mismatch"] for r in per)
    tot = Counter()
    for r in per:
        tot.update(r["xcheck"]["tally"])
    verdict = {
        "snapshots": len(per), "missing": missing, "per_snapshot": per,
        "criteria": crit,
        "agree": {"pass": cert_mism == 0 and xc_mism == 0 and bool(per),
                  "certifier_fact_derivation_mismatch": cert_mism,
                  "xcheck_mismatch": xc_mism, "xcheck_tally": dict(tot),
                  "facts_certified": sum(r["certifier"].get("certified", 0) for r in per),
                  "facts_inconclusive": sum(r["certifier"].get("inconclusive", 0) for r in per)},
    }
    for k in ("patch_fact", "f5", "killed", "suffix"):
        verdict[f"{k}_pass"] = bool(crit[k]) and all(v["pass"] for v in crit[k].values())
    return verdict


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WP20 integration acceptance on fixture snapshots")
    ap.add_argument("fx_root", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    v = evaluate(a.fx_root, a.out)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "eval.json").write_text(json.dumps(v, indent=1, sort_keys=True, default=str))
    print(json.dumps({k: v[k] for k in v if k.endswith("_pass") or k in ("agree", "snapshots",
                                                                      "missing")},
                     indent=1, sort_keys=True))
    ok = all(v[k] for k in v if k.endswith("_pass")) and v["agree"]["pass"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
