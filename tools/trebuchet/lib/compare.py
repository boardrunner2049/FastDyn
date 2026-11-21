#!/usr/bin/env python3
"""compare.py OUT.json REPS VARIANT NAME=ARTIFACT...: paired comparison of caches for one image.

Every NAME is one arm: the given artifact (or snapshot) directory deployed as the cache of
image VARIANT.  A block is one final input and one launch of every arm in random order;
one warm-up block per input is not recorded.  The checks and the invalid-campaign rule are
those of bench.py: sixteen reported registers, run proof, zero appended blocks.
Run with the python of the scopt environment (see trebuchet.sh).
"""
import json, os, random, shutil, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from result_schema import expected_values
from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, launch

E = Path(os.environ["TREBUCHET_WORK"]); WORK = E / "scopt"; Q = os.environ["TREBUCHET_QEMU"]
out = Path(sys.argv[1]); REPS = int(sys.argv[2]); V = sys.argv[3]
ARMS = dict(a.split("=", 1) for a in sys.argv[4:])
INP = E / "inputs/final/freertos-mps2-compute"
SCRATCH = Path("/dev/shm/trebuchet-compare"); shutil.rmtree(SCRATCH, ignore_errors=True); SCRATCH.mkdir()
refs = {n: expected_values((WORK / V / "ref" / f"final-{n}.guest.norm").read_text().split("\n")) for n in (1, 2, 3)}
serial = 0

def one(arm, n):
    global serial; serial += 1
    rd = create_run_dir(E / "cfg" / V / "fw.toml", SCRATCH / f"r{serial}", qemu=Q, label="compare")
    deploy(ARMS[arm], rd)
    doc = launch(rd, prove=True, inputs={"input": INP / f"final-{n}.bin"})
    failures = []
    if doc["exit_code"] != 0 or doc["timed_out"] or len(doc["bench"]) != 1:
        failures.append(f"exit code {doc['exit_code']}, timed out {doc['timed_out']}, {len(doc['bench'])} benchmark windows")
    try:
        got = expected_values(doc["guest_norm"])
        failures += [f"report value {i} = 0x{g:08x}, expected 0x{w:08x}" for i, (g, w) in enumerate(zip(got, refs[n])) if g != w]
    except ValueError as error:
        failures.append(str(error))
    if doc["verdict"] != "PASS" or doc["appended"] != 0:
        failures.append(f"cache proof {doc['verdict']}, {doc['appended']} blocks appended")
    shutil.rmtree(SCRATCH / f"r{serial}", ignore_errors=True)
    return {"guest_ns": doc["bench"][0]["elapsed_ns"] if doc["bench"] else None,
            "process_ns": int(doc["timings"]["wall_s"] * 1e9), "ok": not failures, "failures": failures}

for n in (1, 2, 3):
    for arm in ARMS:
        r = one(arm, n)
        if not r["ok"]:
            sys.exit(f"warm-up launch failed its checks ({arm}, final-{n}): {r['failures']}")
rng = random.Random(20261121); samples = []
for rep in range(REPS):
    for n in (1, 2, 3):
        order = list(ARMS); rng.shuffle(order)
        for pos, arm in enumerate(order):
            r = one(arm, n); r.update(arm=arm, input=f"final-{n}", block=rep * 3 + n - 1, position=pos); samples.append(r)
        out.write_text(json.dumps({"variant": V, "arms": {k: Path(v).name for k, v in ARMS.items()},
                                   "valid": all(s["ok"] for s in samples), "samples": samples}, indent=1))
bad = [s for s in samples if not s["ok"]]
for s in bad:
    print("FAILED", s["arm"], s["input"], "block", s["block"], "; ".join(s["failures"]))
if bad:
    sys.exit(1)
blocks = sorted({s["block"] for s in samples}); by = {(s["block"], s["arm"]): s for s in samples}
names = list(ARMS); base = names[0]
print(f"{'arm':12s} {'guest ms':>9s}   speedup over {base} [95 % interval]")
for a in names:
    ratios = [by[(b, base)]["guest_ns"] / by[(b, a)]["guest_ns"] for b in blocks]
    boot = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(10000))
    print(f"{a:12s} {statistics.median(by[(b, a)]['guest_ns'] for b in blocks) / 1e6:9.1f}   {statistics.median(ratios):.3f}x [{boot[249]:.3f}, {boot[9749]:.3f}]")
