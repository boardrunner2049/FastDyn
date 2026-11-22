"""Run predeclared application comparisons serially with a fixed corpus budget."""
import argparse
import json
from pathlib import Path

from campaign import write_json
from final_evaluation import evaluate, table
from joint_selection import ApplicationTrial, pinned, positive, prepare, require, select
from scopt.run.launcher import file_sha256


def run(corpus, output):
    rows = corpus["applications"]
    budget = corpus["budget"]
    names = [r["application"] for r in rows]
    require(len(names) == len(set(names)) and names, "empty or duplicate application list")
    planned = []
    candidates, offline = 0, 0
    for row in rows:
        if row["status"] == "blocked":
            require(bool(row.get("reason")), "blocked application needs evidence")
            continue
        require(row["status"] == "admitted" and positive(row["weight"]), "invalid admitted application")
        registry = json.loads(pinned(row["registry"]).read_text())
        plan = json.loads(pinned(row["protocol"]).read_text())
        require(plan["registry_sha256"] == row["registry"]["sha256"] and plan["application"] == row["application"]
                and plan["family"] == row["family"], "corpus and protocol disagree")
        inc = json.loads(pinned(plan["incumbent_registry"]).read_text()) if "incumbent_registry" in plan else None
        prepare(registry, plan, inc)
        candidates += len(registry["candidates"])
        offline += registry["offline_seconds"]
        planned.append((row, registry, plan, inc))
    require(candidates <= budget["candidate_limit"] and offline <= budget["offline_seconds"], "suite budget exceeded")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", corpus)
    receipt = {"state": "running", "applications": [dict(r) for r in rows]}
    write_json(output / "dataset.json", receipt)
    try:
        frozen = []
        for index, (row, reg, plan, inc) in enumerate(planned):
            trial = ApplicationTrial(reg, plan)
            selection = select(reg, plan, trial, output / f"selection-{index}", inc)
            frozen.append((row, reg, plan, inc, trial, selection))
        # Freeze every application's choice before opening any final scenario.
        for index, (row, reg, plan, inc, trial, selection) in enumerate(frozen):
            path = output / f"final-{index}"
            evaluate(plan, reg, selection, inc, trial, path)
            entry = receipt["applications"][names.index(row["application"])]
            entry["final"] = {"path": str((path / "final.json").resolve()), "sha256": file_sha256(path / "final.json")}
            write_json(output / "dataset.json", receipt)
        receipt["state"] = "completed" if planned else "blocked"
        print(table(receipt))
    except BaseException as exc:
        receipt.update(state="failed", error=str(exc))
        raise
    finally:
        write_json(output / "dataset.json", receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(json.loads(args.corpus.read_text()), args.output)
