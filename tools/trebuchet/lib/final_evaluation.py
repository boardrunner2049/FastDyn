"""Measure a frozen joint selection on previously reserved operational scenarios."""
import argparse
import itertools
import json
import math
import random
import statistics
import sys
from pathlib import Path

from campaign import write_json
from joint_selection import ApplicationTrial, pinned, positive, prepare, require
from scopt.run.launcher import file_sha256


def summaries(blocks, reference, chosen, maximize, rng, secondary):
    results = {}
    for metric in ("value", secondary, "wall_s"):
        pairs = [(b[reference][metric], b[chosen][metric]) for b in blocks]
        require(all(positive(a) and positive(b) for a, b in pairs), "invalid final metric")
        ratios = [b / a if maximize and metric == "value" else a / b for a, b in pairs]
        # Stratify by scenario, keep each measured pair together, and resample blocks.
        samples = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(2000))
        results[metric] = {"reference_median": statistics.median(a for a, _ in pairs),
                           "selected_median": statistics.median(b for _, b in pairs),
                           "median_gain": statistics.median(ratios),
                           "bootstrap_95": [samples[49], samples[1949]]}
    return results


def evaluate(plan, registry, selection, incumbent_registry, trial, out):
    base, contenders, incumbent = prepare(registry, plan, incumbent_registry)
    require(selection["state"] == "completed" and selection["objective"] == plan["objective"],
            "missing completed selection for this objective")
    choice = selection["selected"]
    require(any(choice == row for row in (base, *contenders)) and selection["fallback"] == base
            and selection["incumbent"] == incumbent, "selection does not match pinned deployments")
    secondary = plan.get("secondary_measurement", "guest_window_ns")
    require(secondary in ("qemu_lifetime_ns", "guest_window_ns"), "unknown secondary measurement")
    references = {"ordinary": base}
    if incumbent is not None:
        references["incumbent"] = incumbent
    arms = {**references, "selected": choice}
    # Represent identical deployments by one arm and disclose that comparison.
    unique = {}
    aliases = {}
    for name, row in arms.items():
        key = row["deployment_identity"]
        aliases[name] = unique.setdefault(key, name)
    permutations = list(itertools.permutations(unique.values()))
    count = plan["pairs"]["final"]
    require(type(count) is int and count >= 12 and count % len(permutations) == 0,
            "final block count must balance every order")
    out.mkdir(parents=True, exist_ok=False)
    report = {"schema": "trebuchet.final/1", "state": "running", "application": registry["application"],
              "configuration": registry["configuration"], "family": plan["family"], "objective": plan["objective"],
              "plan": plan, "arms": arms, "aliases": aliases, "scenarios": {}, "promoted": selection["promoted"]}
    write_json(out / "final.json", report)
    rng = random.Random(plan["final_seed"])
    bootstrap = random.Random(plan["final_seed"])
    try:
        with (out / "observations.jsonl").open("x") as log:
            seq = 0
            for case in plan["scenarios"]["final"]:
                orders = permutations * (count // len(permutations))
                rng.shuffle(orders)
                blocks = []
                for i, order in enumerate([*([permutations[0]] * plan["warmup_pairs"]), *orders], -plan["warmup_pairs"]):
                    block = {}
                    for arm in order:
                        seq += 1
                        record = {"case": case["id"], "block": i, "order": order, "arm": arm,
                                  "identity": arms[arm]["deployment_identity"]}
                        try:
                            block[arm] = trial(arms[arm], case, out / f"trial-{seq}")
                            record["result"] = block[arm]
                        except Exception as exc:
                            record["error"] = str(exc)
                            raise
                        finally:
                            print(json.dumps(record, allow_nan=False), file=log, flush=True)
                    if i >= 0:
                        blocks.append(block)
                comparisons = {}
                for ref in references:
                    if aliases[ref] == aliases["selected"]:
                        comparisons[ref] = {"identical": True, "gain": 1.0,
                                            "absolute": {m: statistics.median(b[aliases[ref]][m] for b in blocks)
                                                         for m in ("value", secondary, "wall_s")}}
                    else:
                        comparisons[ref] = summaries(blocks, aliases[ref], aliases["selected"],
                                                     plan["objective"]["direction"] == "maximize", bootstrap, secondary)
                report["scenarios"][case["id"]] = {"weight": case["weight"], "blocks": len(blocks),
                                                    "comparisons": comparisons}
                if all("phase_costs" in result for block in blocks for result in block.values()):
                    report["scenarios"][case["id"]]["median_phase_costs"] = {
                        name: {metric: statistics.median(b[name]["phase_costs"][metric] for b in blocks)
                               for metric in blocks[0][name]["phase_costs"]} for name in unique.values()}
                write_json(out / "final.json", report)
        report["state"] = "completed"
    except BaseException as exc:
        report.update(state="failed", error=str(exc))
        raise
    finally:
        write_json(out / "final.json", report)
    return report


def table(manifest):
    """Require the complete, predeclared corpus and all final blocks before aggregating."""
    require(manifest["state"] in ("completed", "blocked"), "incomplete suite")
    families = {}
    unavailable = set()
    admitted = 0
    lines = ["| Application | Final scenario | Reference | Selected deployment | Paired gain |",
             "|---|---|---|---|---:|"]
    measurements = []
    seen = set()
    for case in manifest["applications"]:
        app = case["application"]
        require(app not in seen, "repeated application")
        seen.add(app)
        if case["status"] == "blocked":
            lines.append(f"| {app} | {case['reason']} | — | blocked | — |")
            continue
        require(case["status"] == "admitted", "unknown admission status")
        admitted += 1
        d = json.loads(pinned(case["final"]).read_text())
        protocol = json.loads(pinned(case["protocol"]).read_text())
        require(d["state"] == "completed" and d["application"] == app and d["family"] == case["family"]
                and d["plan"] == protocol and d["objective"] == protocol["objective"], "final evidence mismatch")
        expected = {c["id"]: c["weight"] for c in protocol["scenarios"]["final"]}
        require(set(expected) == set(d["scenarios"]), "incomplete final scenarios")
        app_gain = 0.0
        for name, scenario in d["scenarios"].items():
            require(scenario["blocks"] == protocol["pairs"]["final"] and expected[name] == scenario["weight"],
                    "incomplete final block plan")
            references = {"ordinary", "incumbent"} if protocol.get("promotion_enabled", True) else {"ordinary"}
            require(set(scenario["comparisons"]) == references, "missing or unexpected final comparator")
            for ref, comparison in scenario["comparisons"].items():
                gain = 1.0 if comparison.get("identical") else comparison["value"]["median_gain"]
                require(positive(gain), "invalid final gain")
                interval = "" if comparison.get("identical") else " [%0.3f, %0.3f]" % tuple(comparison["value"]["bootstrap_95"])
                lines.append(f"| {app} | {name} | {ref} | {d['arms']['selected']['id']} | {gain:.3f}×{interval} |")
                secondary = protocol.get("secondary_measurement", "guest_window_ns")
                for metric, label in (("value", protocol["objective"]["unit"]),
                                      (secondary, "ns"), ("wall_s", "s")):
                    if comparison.get("identical"):
                        before = after = comparison["absolute"][metric]
                        ratio = "same deployment"
                    else:
                        values = comparison[metric]
                        before, after = values["reference_median"], values["selected_median"]
                        low, high = values["bootstrap_95"]
                        ratio = f"{values['median_gain']:.3f}× [{low:.3f}, {high:.3f}]"
                    title = protocol["objective"]["name"] if metric == "value" else metric
                    measurements.append(f"| {app} | {name} | {ref} | {title} ({label}) | "
                                        f"{before:.6g} | {after:.6g} | {ratio} |")
                if ref == "incumbent":
                    app_gain += expected[name] * math.log(gain) / sum(expected.values())
        require(positive(case["weight"]), "invalid application weight")
        if protocol.get("promotion_enabled", True):
            families.setdefault(case["family"], []).append((case["weight"], app_gain))
        else:
            unavailable.add(case["family"])
            lines.append(f"| {app} | pre-change comparison | unavailable | ordinary-only diagnostic | — |")
    require(bool(admitted) == (manifest["state"] == "completed"), "suite state disagrees with admissions")
    if families:
        gains = []
        for family, values in families.items():
            if family in unavailable:
                continue
            gain = math.exp(sum(w*x for w,x in values)/sum(w for w,_ in values))
            lines.append(f"| **{family}** | family aggregate | incumbent | | **{gain:.3f}×** |")
            gains.append(gain)
        if not unavailable:
            lines.append(f"| **Admitted families** | geometric mean | incumbent | | **{statistics.geometric_mean(gains):.3f}×** |")
    if measurements:
        lines.extend(["", "| Application | Scenario | Reference | Metric | Reference median | Selected median | Paired gain [95% interval] |",
                      "|---|---|---|---|---:|---:|---:|", *measurements])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("final")
    p.add_argument("registry", type=Path)
    p.add_argument("protocol", type=Path)
    p.add_argument("selection", type=Path, help="directory holding frozen selection, registry and protocol")
    p.add_argument("output", type=Path)
    sub.add_parser("report").add_argument("corpus", type=Path)
    args = parser.parse_args()
    if args.command == "report":
        result = json.loads(args.corpus.read_text())
        original = json.loads((args.corpus.parent / "plan.json").read_text())
        require([{k: v for k, v in r.items() if k != "final"} for r in result["applications"]]
                == original["applications"], "suite differs from frozen corpus")
        print(table(result))
        sys.exit()
    protocol = json.loads(args.protocol.read_text())
    registry = json.loads(args.registry.read_text())
    require(file_sha256(args.registry) == protocol["registry_sha256"] and
            json.loads((args.selection / "registry.json").read_text()) == registry and
            json.loads((args.selection / "plan.json").read_text()) == protocol, "selection inputs changed")
    inc = json.loads(pinned(protocol["incumbent_registry"]).read_text()) if "incumbent_registry" in protocol else None
    evaluate(protocol, registry, json.loads((args.selection / "selection.json").read_text()), inc,
             ApplicationTrial(registry, protocol), args.output)
