#!/usr/bin/env python3
"""bench.py OUT.json REPS: timed end-to-end campaign on the three final inputs.

Arms (one launch of each per block, order randomized per block; a block = one input):
  stock-O0          stock QEMU 8.2.2 (mps2-an385), -O0 image
  fd-O0             FastDyn, -O0 image, SuperCache off
  fd-fb             FastDyn, FirmBOLT image, SuperCache off
  fd-fb-warm        FastDyn, FirmBOLT image, ordinary warm SuperCache
  fd-fb-scopt       FastDyn, FirmBOLT image, warm SuperCache selected by scopt
extra context arms: stock-fb, fd-O0-warm.
Guest work: FastDyn's benchmark window ([BENCH]); for stock QEMU the same window found with the
gdbstub (stock.py).  Process: wall time of the FastDyn frontend process, or of stock QEMU from
start until the work is done.  Run with the python of the scopt environment (see trebuchet.sh).
"""
import glob, json, os, random, shutil, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stock
from result_schema import expected_values, stats_mismatches
from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, launch

E = Path(os.environ["TREBUCHET_WORK"]); WORK = E / "scopt"; Q = os.environ["TREBUCHET_QEMU"]
INP = E / "inputs/final/freertos-mps2-compute"
out = Path(sys.argv[1]); REPS = int(sys.argv[2])
SCRATCH = Path("/dev/shm/trebuchet-bench"); shutil.rmtree(SCRATCH, ignore_errors=True); SCRATCH.mkdir()

def snapshot(v):
    return glob.glob(str(WORK / v / "store/snapshots/*"))[0]

FB = "O0-fb"
sel = json.loads((WORK / FB / "tune.json").read_text())
selected = sel["selected_artifact"]
ARMS = {
    "stock-O0": ("stock", "O0"), "stock-fb": ("stock", FB),
    "fd-O0": ("nocache", "O0"), "fd-fb": ("nocache", FB),
    "fd-O0-warm": ("cache", "O0", snapshot("O0")),
    "fd-fb-warm": ("cache", FB, snapshot(FB)),
    "fd-fb-scopt": ("cache", FB, selected),
}
refs = {v: {n: (WORK / v / "ref" / f"final-{n}.guest.norm").read_text().split("\n")[:-1] for n in (1, 2, 3)} for v in ("O0", FB)}
expect = {}
samples = []; serial = 0

def fastdyn(kind, v, artifact, inp, ref):
    global serial; serial += 1
    root = SCRATCH / f"r{serial}"
    cfg = E / "cfg" / v / ("fw.toml" if kind == "cache" else "nocache.toml")
    rd = create_run_dir(cfg, root, qemu=Q, label="bench")
    if kind == "cache":
        deploy(artifact, rd)
    doc = launch(rd, prove=(kind == "cache"), inputs={"input": inp})
    failures = []
    if doc["exit_code"] != 0 or doc["timed_out"]:
        failures.append(f"exit code {doc['exit_code']}, timed out {doc['timed_out']}")
    if len(doc["bench"]) != 1:
        failures.append(f"{len(doc['bench'])} benchmark windows")
    try:
        got = expected_values(doc["guest_norm"])
    except ValueError as error:
        failures.append(str(error))
    else:
        want = expected_values(ref)
        failures += [f"report value {i} = 0x{g:08x}, expected 0x{w:08x}" for i, (g, w) in enumerate(zip(got, want)) if g != w]
    if kind == "cache":
        if doc["verdict"] != "PASS":
            failures.append(f"cache proof verdict {doc['verdict']}")
        if doc["appended"] != 0:
            failures.append(f"{doc['appended']} translation blocks appended")
    r = {"guest_ns": doc["bench"][0]["elapsed_ns"] if doc["bench"] else None,
         "process_ns": int(doc["timings"]["wall_s"] * 1e9), "ok": not failures, "failures": failures}
    shutil.rmtree(root, ignore_errors=True)
    return r

def stock_arm(v, inp, ref, key):
    elf = str(E / "fw" / v / "RTOSDemo.out")
    a = stock.run(elf, str(inp), icount=True, window=True, expect_ns=expect.get((key, "g")))
    b = stock.run(elf, str(inp), icount=True, window=False, expect_ns=expect.get((key, "w")))
    expect[(key, "g")] = a["ns"]; expect[(key, "w")] = b["ns"]
    want = expected_values(ref)
    failures = [f"window run: {m}" for m in stats_mismatches(a["stats"], want)]
    failures += [f"process run: {m}" for m in stats_mismatches(b["stats"], want)]
    return {"guest_ns": a["ns"], "process_ns": b["ns"], "ok": not failures, "failures": failures,
            "uncertainty_ns": max(a["uncertainty_ns"], b["uncertainty_ns"])}

def one(arm, n):
    spec = ARMS[arm]; inp = INP / f"final-{n}.bin"; ref = refs[spec[1]][n]
    if spec[0] == "stock":
        return stock_arm(spec[1], inp, ref, (arm, n))
    return fastdyn(spec[0], spec[1], spec[2] if len(spec) > 2 else None, inp, ref)

assert refs["O0"] == refs[FB], "FirmBOLT image and O0 image disagree on a final input"
rng = random.Random(20261121)
for n in (1, 2, 3):                       # warm-up block per input (also calibrates the stock polling), not recorded
    for arm in ARMS:
        r = one(arm, n)
        if not r["ok"]:
            sys.exit(f"warm-up launch failed its checks ({arm}, final-{n}): {r['failures']}")
for rep in range(REPS):
    for n in (1, 2, 3):
        order = list(ARMS); rng.shuffle(order)
        for pos, arm in enumerate(order):
            r = one(arm, n); r.update(arm=arm, input=f"final-{n}", block=rep * 3 + n - 1, position=pos); samples.append(r)
        out.write_text(json.dumps({"selection": {"status": sel.get("status"), "reason": sel.get("reason"), "selected": selected},
                                   "valid": all(s["ok"] for s in samples), "samples": samples}, indent=1))
    print("rep", rep, "done", time.strftime("%H:%M:%S"), flush=True)
bad = [s for s in samples if not s["ok"]]
print("samples", len(samples), "bad", len(bad))
if bad:
    # The samples and the reasons stay in the file; the campaign is not a valid comparison.
    for s in bad:
        print("FAILED", s["arm"], s["input"], "block", s["block"], "; ".join(s["failures"]))
    sys.exit(1)
