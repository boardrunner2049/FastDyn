"""Pair two previously frozen recipes rebuilt on the same host for one reserved edit."""
import json
import random

from run_campaign import HERE, historical_optimizer, pin, require, write_json
from candidates import build
from scopt.run.warm_selection import _paired_evidence, _paired_orders
from final_evaluation import summaries


def compare_rebuilt(out, plan, policy, cases, trial_factory):
    dest = out / "rebuilt-promotion"
    dest.mkdir()
    protocol = dict(policy, scenarios=cases, images={})
    fixed, trials = {}, {}
    for name in ("historical", "current"):
        frozen = policy["frozen_arms"][name]
        image, recipe = frozen["id"].split("/")
        compiler = pipeline = None
        metadata = {}
        if name == "historical":
            _, source, archive, compiler = historical_optimizer(out, policy["incumbent_revision"])
            pipeline = source / "pipelines"
            metadata.update(source_revision=policy["incumbent_revision"], source_archive=pin(archive))
        recipes = ["warm"] if recipe == "warm" else ["warm", recipe]
        rebuild = dict(plan, images={image: plan["images"][image]}, recipes=recipes, **metadata)
        path = dest / (name + "-plan.json")
        write_json(path, rebuild)
        registry = build(path, dest / name, optimizer=compiler, pipeline_dir=pipeline, fixed_image=image,
                         fixed_recipe=recipe)
        row, = [r for r in registry["candidates"] if r["id"] == frozen["id"]]
        require(row["status"] == "ready", f"frozen deployment could not be rebuilt: {row['id']}")
        require(row["firmware_sha256"] == frozen["firmware_sha256"], "fixed guest ELF differs")
        fixed[name] = row
        protocol["images"][image] = {k: row[k] for k in ("config_sha256", "firmware_sha256", "profile_id")}
        protocol["images"][image]["observer"] = pin(HERE / "observe.py")
        trials[name] = registry
    write_json(dest / "arms.json", fixed)
    write_json(out / "protocol.json", protocol)
    trials = {name: trial_factory(registry, protocol) for name, registry in trials.items()}
    case, = cases["promotion"]
    seed = policy["promotion_seed"]
    pairs = policy["pairs"]["promotion"]
    rng = random.Random(seed)
    orders = [_paired_orders(2, rng)[0] for _ in range(policy["warmup_pairs"])] + _paired_orders(pairs, rng)
    samples, blocks = [], []
    sequence = 0
    with (dest / "observations.jsonl").open("x") as log:
        for i, order in enumerate(orders, -policy["warmup_pairs"]):
            r = {"first": order[0], "second": order[1]}
            block = {}
            for arm in order:
                name = "current" if arm == "candidate" else "historical"
                row = fixed[name]
                record = {"role": "promotion", "case": case["id"], "block": i, "order": order,
                          "arm": name, "identity": row["deployment_identity"]}
                sequence += 1
                try:
                    result = trials[name](row, case, dest / f"trial-{sequence}")
                    record["result"] = result
                    r[arm] = result["value"]
                    block[name] = result
                except Exception as exc:
                    record["error"] = str(exc)
                    raise
                finally:
                    print(json.dumps(record), file=log, flush=True)
            if i >= 0:
                samples.append(r)
                blocks.append(block)
    decision = _paired_evidence(samples, alpha=policy["alpha"])
    gain = decision["median_paired_speedup"]
    metrics = summaries(blocks, "historical", "current", False, random.Random(seed + 1), "qemu_lifetime_ns")
    cpu_blocks = [{arm: dict(result, value=result["phase_costs"]["process_tree_cpu_ns"])
                   for arm, result in block.items()} for block in blocks]
    metrics["process_tree_cpu_ns"] = summaries(cpu_blocks, "historical", "current", False, random.Random(seed + 2),
                                              "qemu_lifetime_ns")["value"]
    write_json(dest / "comparison.json", {"case": case["id"], "pairs": pairs, "metrics": metrics,
               "paired_test": decision, "passes_supplemental_gate": decision["passes"] and gain >= policy["minimum_gain"],
               "scope": "rebuilt implementations on fixed guests; leaves primary strict promotion disposition unchanged"})
