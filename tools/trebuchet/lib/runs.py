#!/usr/bin/env python3
"""Reference runs and selection cases (python of the scopt environment; see trebuchet.sh).

  runs.py refs VARIANT     reference guest transcripts of the ordinary warm cache for every input
  runs.py cases VARIANT    selection case files (ranking: heldout-1; confirmation: heldout-2, heldout-3)
"""
import glob, json, os, shutil, sys
from pathlib import Path
from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, launch

E = Path(os.environ["TREBUCHET_WORK"]); WORK = E / "scopt"
INP = Path(os.environ["TREBUCHET_INPUTS"]); WL = os.environ["TREBUCHET_WORKLOAD"]
INPUTS = {f"{s}-{i}": INP / s / WL / f"{s}-{i}.bin"
          for s in ("train", "heldout", "final") for i in (1, 2, 3)}
Q = os.environ["TREBUCHET_QEMU"]

def snapshot(v):
    return glob.glob(str(WORK / v / "store/snapshots/*"))[0]

def run_once(artifact, cfg, root, inp):
    rd = create_run_dir(cfg, root, qemu=Q, label="trebuchet")
    deploy(artifact, rd)
    return launch(rd, prove=True, inputs={"input": inp})

def refs(v):
    cfg = E / "cfg" / v / "fw.toml"; out = WORK / v / "ref"; out.mkdir(exist_ok=True)
    for name, inp in INPUTS.items():
        root = WORK / v / "ref-runs" / name
        if root.exists():
            shutil.rmtree(root)
        doc = run_once(snapshot(v), cfg, root, inp)
        assert doc["exit_code"] == 0 and doc["verdict"] == "PASS" and doc["appended"] == 0, (name, doc["verdict"], doc.get("appended"))
        (out / f"{name}.guest.norm").write_text("\n".join(doc["guest_norm"]) + "\n")
        print(v, name, "bench_ms", doc["bench"][0]["elapsed_ns"] / 1e6, "lines", len(doc["guest_norm"]), flush=True)

def cases(v):
    cfg = str(E / "cfg" / v / "fw.toml"); ref = WORK / v / "ref"
    def case(n):
        return {"name": n, "config": cfg, "bank_inputs": {"input": str(INPUTS[n])},
                "expected_guest": str(ref / f"{n}.guest.norm")}
    train = [str(INPUTS[f"train-{i}"]) for i in (1, 2, 3)]
    (WORK / v / "rank-cases.json").write_text(json.dumps({"training_inputs": train, "inputs": [case("heldout-1")]}, indent=1))
    (WORK / v / "confirm-cases.json").write_text(json.dumps({"training_inputs": train + [str(INPUTS["heldout-1"])],
                                                             "inputs": [case("heldout-2"), case("heldout-3")]}, indent=1))

if __name__ == "__main__":
    {"refs": refs, "cases": cases}[sys.argv[1]](sys.argv[2])
