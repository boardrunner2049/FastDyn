#!/usr/bin/env python3
"""Generate ``docs/o4-evidence.json``: the timing evidence behind "O4 remains the best".

This script is the only writer of that file (O4-design.md 2.10).  Its input is a *spec*,
a list of campaigns, each naming the driver's per-workload ``summary.json`` and
``plan.json``, the pre-registered ``decide.json`` computed from them, and, for a campaign
timed before O4 existed, the arm that stood for O4 and the record proving that arm's
``cache.sc`` equals O4's.  The output embeds the spec, pins every source by sha256 and has
one row per (arm, workload or ``pooled``) of each campaign's BEST family:

  revision, campaign, workload, reference ("O4"), arm, d, se, lb95, ub95, p,
  holm_family, holm_rank, verdict (faster_than_o4 | slower_than_o4 | not_shown |
  same_bytes), summary_sha256, decide_sha256, the driver's own median ratio, interval and
  verdict for continuity (as it reported them, plus ``o4_over_arm`` = time(O4) / time(arm)),
  and ``via`` when O4 was timed under another name.

Re-running on the pinned sources reproduces the file byte for byte:

  o4_evidence.py build --spec SPEC.json [--out docs/o4-evidence.json]
  o4_evidence.py check [--evidence docs/o4-evidence.json]    # exit 0 same, 1 differs,
                                                             # 3 a source is absent
  o4_evidence.py adapt-round2 SRC_RUN DST_RUN --reference ARM

``adapt-round2`` prepares a run of a campaign that predates O4 for ``decide.py
--unregistered``: it copies ``observations.jsonl`` byte for byte and writes a plan whose
reference and A/A arm are both ``ARM`` (that campaign had no copy of O4 to pool with, so
the VALID check is vacuous), ``x_campaign = "A"``, no burn-in and no same-bytes list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

SCHEMA = "scopt.o4-evidence/2"
SPEC_SCHEMA = "scopt.o4-evidence-spec/1"
GENERATOR = "scripts/o4_evidence.py"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "docs" / "o4-evidence.json"
VERDICTS = ("faster_than_o4", "slower_than_o4", "not_shown", "same_bytes")


class SourceMissing(Exception):
    pass


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise SourceMissing(str(p))
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load(path: str | Path):
    sha256_file(path)
    return json.loads(Path(path).read_text())


def _driver_ratio(summary, reference_arm, arm):
    """The driver's own paired comparison of ``reference_arm`` and ``arm``, as it reported
    it ("A/B" = time(A) / time(B)), plus ``o4_over_arm`` = time(O4) / time(arm)."""
    paired = summary.get("paired", {})
    for key, inverted in ((f"{reference_arm}/{arm}", False), (f"{arm}/{reference_arm}", True)):
        if key in paired:
            row = paired[key]
            return {"comparison": key, "median": row["median"], "interval": row["interval"],
                    "verdict": row.get("verdict"), "n_blocks": row.get("n_blocks"),
                    "o4_over_arm": 1 / row["median"] if inverted else row["median"]}
    return None


def build(spec) -> dict:
    """The evidence document for ``spec`` (raises SourceMissing when a source is absent)."""
    if spec.get("schema") != SPEC_SCHEMA:
        raise ValueError(f"spec schema must be {SPEC_SCHEMA}")
    sources, rows = [], []

    def pin(path, campaign, kind, section=None):
        digest = sha256_file(path)
        entry = {"path": str(path), "sha256": digest, "campaign": campaign["id"], "kind": kind,
                 "timing_md": campaign["timing_md"], "section": section or campaign["section"]}
        if entry not in sources:
            sources.append(entry)
        return digest

    for campaign in spec["campaigns"]:
        cid, revision, ref = campaign["id"], campaign["revision"], campaign["reference_arm"]
        decide = _load(campaign["decide"])
        decide_sha = pin(campaign["decide"], campaign, "decide")
        inputs = {Path(i["dir"]).resolve(): i for i in decide["inputs"] if i.get("chosen", True)}
        summaries, summary_sha = {}, {}
        for run in campaign["runs"]:
            w = run["workload"]
            summaries[w] = _load(run["summary"])
            summary_sha[w] = pin(run["summary"], campaign, "summary", run.get("section"))
            pin(run["plan"], campaign, "plan", run.get("section"))
            got = inputs.get(Path(run["decide_input"]).resolve())
            if got is None or got["workload"] != w or got["reference"] != ref:
                raise ValueError(f"{cid}/{w}: {run['decide_input']} is not a decide.json input "
                                 f"with reference {ref}")
            plan_sha = pin(Path(run["decide_input"]) / "plan.json", campaign, "decide_plan",
                           run.get("section"))
            obs_sha = pin(Path(run["decide_input"]) / "observations.jsonl", campaign,
                          "observations", run.get("section"))
            if (got["plan_sha256"], got["observations_sha256"]) != (plan_sha, obs_sha):
                raise ValueError(f"{cid}/{w}: decide.json was computed from other inputs")
            if run.get("observations"):
                if pin(run["observations"], campaign, "observations_original",
                       run.get("section")) != obs_sha:
                    raise ValueError(f"{cid}/{w}: the decide input's observations differ from "
                                     f"{run['observations']}")
        best = decide["best"][campaign["decide_campaign"]]
        family = f"{cid}:BEST"
        pooled_sha = ",".join(summary_sha[w] for w in sorted(summary_sha))
        for r in best["rows"]:
            scope = r["scope"]
            row = {"revision": revision, "campaign": cid, "workload": scope, "reference": "O4",
                   "arm": r["arm"], "verdict": r["verdict"], "decide_sha256": decide_sha,
                   "summary_sha256": summary_sha[scope] if scope in summary_sha else pooled_sha,
                   "registered": decide["registered"]}
            if r["verdict"] not in VERDICTS:
                raise ValueError(f"{cid}: verdict {r['verdict']!r}")
            if r["verdict"] != "same_bytes":
                row.update({k: r[k] for k in ("d", "se", "lb95", "ub95", "p")},
                           holm_family=family, holm_rank=r["holm_rank"],
                           holm_family_size=r["holm_family_size"],
                           reference_bias=r.get("reference_bias"))
                row["driver"] = (_driver_ratio(summaries[scope], ref, r["arm"])
                                 if scope in summaries else None)
            if campaign.get("via"):
                row["via"] = campaign["via"]
            rows.append(row)
        identity = campaign.get("identity")
        if identity:
            doc = _load(identity)
            identity_sha = pin(identity, campaign, "identity")
            for w in sorted(doc["workloads"], key=lambda w: list(summary_sha).index(w)):
                cmp_row = doc["workloads"][w]
                if cmp_row["o4_cache_sha256"] != cmp_row["reference_cache_sha256"]:
                    raise ValueError(f"{cid}/{w}: O4 and {ref} caches differ")
                rows.append({"revision": revision, "campaign": cid, "workload": w,
                             "reference": "O4", "arm": ref, "verdict": "same_bytes",
                             "summary_sha256": summary_sha[w], "decide_sha256": decide_sha,
                             "identity_sha256": identity_sha,
                             "cache_sha256": cmp_row["o4_cache_sha256"],
                             "registered": decide["registered"], "via": campaign.get("via")})
    return {"schema": SCHEMA, "generator": GENERATOR, "spec": spec, "sources": sources,
            "rows": rows}


def render(doc) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + "\n"


def adapt_round2(src: Path, dst: Path, reference: str) -> None:
    plan = json.loads((src / "plan.json").read_text())
    if reference not in plan["arms"]:
        raise SystemExit(f"{src}: {reference} is not an arm of the plan")
    plan.pop("x_same_cache_bytes", None)
    plan.update(x_campaign="A", burn_in_blocks=0,
                aa_control_resolved={"reference": reference, "arm": reference},
                x_o4_seed={"source_plan": str(src / "plan.json"),
                           "source_plan_sha256": sha256_file(src / "plan.json"),
                           "note": f"{reference} stands for O4 (same cache.sc); the campaign had "
                                   "no O4 A/A copy, so reference and aa are the same arm"})
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / "observations.jsonl", dst / "observations.jsonl")
    (dst / "plan.json").write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--spec", type=Path, required=True)
    b.add_argument("--out", type=Path, default=DEFAULT_OUT)
    c = sub.add_parser("check")
    c.add_argument("--evidence", type=Path, default=DEFAULT_OUT)
    a = sub.add_parser("adapt-round2")
    a.add_argument("src", type=Path)
    a.add_argument("dst", type=Path)
    a.add_argument("--reference", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "adapt-round2":
        adapt_round2(args.src, args.dst, args.reference)
        return 0
    if args.cmd == "build":
        args.out.write_text(render(build(json.loads(args.spec.read_text()))))
        return 0
    current = args.evidence.read_text()
    try:
        again = render(build(json.loads(current)["spec"]))
    except SourceMissing as e:
        print(f"source absent: {e}", file=sys.stderr)
        return 3
    if again != current:
        print(f"{args.evidence} is not what {GENERATOR} generates from its sources",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
