"""Checkpoint and validation contract for Trebuchet campaigns."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4


ARMS = ("stock-O0", "fd-O0", "fd-fb", "fd-fb-warm", "fd-fb-scopt", "stock-fb", "fd-O0-warm")
INPUTS = ("final-1", "final-2", "final-3")
SCHEMA = "trebuchet.campaign/2"


def write_json(path, value):
    """Publish one complete JSON document on the same filesystem."""
    path = Path(path)
    with NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(value, stream, indent=1, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def campaign_plan(repetitions):
    return {"inputs": list(INPUTS), "arms": list(ARMS), "repetitions": repetitions}


@contextmanager
def record_campaign(path, workload, repetitions, dataset_id=None):
    doc = {
        "schema": SCHEMA, "run_id": str(uuid4()), "dataset_id": dataset_id,
        "state": "running", "workload": workload, "valid": False, "selection": {},
        "plan": campaign_plan(repetitions),
        "samples": [],
    }
    write_json(path, doc)
    try:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        yield doc
        errors = sample_errors(doc["samples"], doc["plan"])
        if errors:
            raise ValueError("\n".join(errors))
    except BaseException as error:
        doc.update(state="failed", error=f"{type(error).__name__}: {error}")
        write_json(path, doc)
        raise
    doc.update(state="completed", valid=True)
    write_json(path, doc)


def sample_errors(samples, plan):
    arms, inputs, repetitions = plan["arms"], plan["inputs"], plan["repetitions"]
    missing = {(block, arm) for block in range(len(inputs) * repetitions) for arm in arms}
    seen = set()
    positions = set()
    errors = []
    for s in samples:
        block, arm, pos = s.get("block"), s.get("arm"), s.get("position")
        key = block, arm
        if key in seen:
            errors.append(f"duplicate: {arm} in block {block}")
        elif key not in missing or type(block) is not int:
            errors.append(f"unexpected: {arm} in block {block}")
        else:
            missing.remove(key)
            if s.get("input") != inputs[block % len(inputs)]:
                errors.append(f"wrong input for {arm} in block {block}")
        seen.add(key)
        if type(pos) is not int or not 0 <= pos < len(arms) or (block, pos) in positions:
            errors.append(f"invalid or duplicate position {pos} in block {block}")
        positions.add((block, pos))
        if s.get("ok") is not True:
            errors.append(f"failed: {arm} {s.get('input')} block {block}: " +
                          "; ".join(s.get("failures") or ["check failed"]))
        if any(type(s.get(key)) is not int or s[key] <= 0 for key in ("process_ns", "guest_ns")):
            errors.append(f"invalid measurement for {arm} in block {block}")
    return errors + [f"missing: {arm} in block {block}" for block, arm in sorted(missing)]


def campaign_errors(doc):
    if doc.get("schema") == "trebuchet.campaign/1":
        # Published legacy campaigns all used the same independently documented plan.
        plan = {"inputs": INPUTS, "arms": ARMS, "repetitions": 8}
        errors = ["campaign marked invalid"] if doc.get("valid") is False else []
    elif doc.get("schema") == SCHEMA:
        errors = []
        if doc.get("state") != "completed" or doc.get("valid") is not True:
            errors.append(f"campaign state {doc.get('state')}: {doc.get('error', 'not completed and validated')}")
        if not doc.get("run_id"):
            errors.append("missing campaign run identity")
        plan = doc.get("plan")
        if not isinstance(plan, dict) or plan.get("inputs") != list(INPUTS) or (
            not isinstance(plan.get("arms"), list) or sorted(plan["arms"]) != sorted(ARMS)
        ) or type(plan.get("repetitions")) is not int or plan["repetitions"] < 1:
            return errors + ["invalid campaign plan"]
    else:
        return ["unknown campaign schema or missing campaign plan"]
    return errors + sample_errors(doc["samples"], plan)
