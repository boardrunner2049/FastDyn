"""Only validate training scheduling and protocol structure, without timing an application."""
from itertools import combinations
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from training_contrasts import orders


def test_every_training_contrast_has_balanced_orders():
    policy = json.loads(Path(__file__).with_name("training-contrasts.json").read_text())
    assert policy["case"] == "update-package-version" and policy["role"] == "training"
    for group in policy["groups"].values():
        arms = ["warm"]
        for spec in group["arms"]:
            assert spec["parent"] in arms and spec["name"] not in arms
            arms.append(spec["name"])
        for before, after in group["contrasts"]:
            assert before in arms and after in arms and before != after
        schedule = orders(arms, random.Random(policy["timing"]["seed"]), policy["timing"]["blocks"])
        assert len(schedule) == 12
        assert all(set(row) == set(arms) for row in schedule)
        for a, b in combinations(arms, 2):
            assert sum(row.index(a) < row.index(b) for row in schedule) == 6
