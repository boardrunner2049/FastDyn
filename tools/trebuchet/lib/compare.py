#!/usr/bin/env python3
"""compare.py OUT.json REPS VARIANT NAME=ARTIFACT...: paired comparison of caches for one image.

Every NAME is one arm: the given artifact (or snapshot) directory deployed as the cache of
image VARIANT.  A block is one final input and one launch of every arm in random order;
one warm-up block per input is not recorded.  The checks and the invalid-campaign rule are
those of bench.py: sixteen reported registers, run proof, zero appended blocks.

The first arm is the base.  Unless TREBUCHET_COMPARE_AA=0, an A/A arm `<base>-aa` deploys
the base artifact again under another name (each launch gets its own cache copy); its
paired ratio is the harness noise floor.  REPS*3 blocks below TREBUCHET_MIN_BLOCKS
(default 8) are refused unless TREBUCHET_ALLOW_FEW_BLOCKS=1, which labels the result
underpowered.  The primary statistic is the paired block-ratio median with a seeded
bootstrap interval, sign test and verdict (scopt.run.paired_stats).
Run with the python of the scopt environment (see trebuchet.sh).
"""
import json, os, random, shutil, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scopt.run import paired_stats

SEED = 20261121


def arms_with_aa(arms, enabled=True):
    """(arms with the A/A copy of the first arm appended, (aa, base) or None)."""
    arms = dict(arms)
    base = next(iter(arms))
    if not enabled:
        return arms, None
    name = paired_stats.aa_arm_name(base, arms)
    arms[name] = arms[base]
    return arms, (name, base)


def protocol_check(reps, environ):
    """Warning text for an allowed short campaign; raises UnderpoweredError otherwise."""
    return paired_stats.check_min_blocks(
        reps * 3, int(environ.get("TREBUCHET_MIN_BLOCKS") or paired_stats.DEFAULT_MIN_BLOCKS),
        environ.get("TREBUCHET_ALLOW_FEW_BLOCKS") == "1")


def summarize(samples, names, aa=None, min_blocks=paired_stats.DEFAULT_MIN_BLOCKS, seed=SEED):
    """Paired base/arm guest-time ratios per block (above 1: the arm is faster)."""
    blocks = sorted({s["block"] for s in samples})
    by = {(s["block"], s["arm"]): s for s in samples}
    base = names[0]

    def ratios(arm):
        return [by[(b, base)]["guest_ns"] / by[(b, arm)]["guest_ns"] for b in blocks]

    floor = None
    if aa:
        values = ratios(aa[0])
        floor = paired_stats.aa_floor(values, rng=random.Random(seed ^ 0xAA), min_blocks=min_blocks)
        floor.update(arm=aa[0], reference=base, ratio=f"{base}/{aa[0]}", blocks=values)
    paired = {}
    for i, arm in enumerate(names):
        if arm == base or (aa and arm == aa[0]):
            continue
        values = ratios(arm)
        paired[arm] = dict(paired_stats.summarize_pair(values, rng=random.Random(seed ^ i),
                                                       floor=floor, min_blocks=min_blocks),
                           ratio=f"{base}/{arm}", blocks=values)
    medians = {a: statistics.median(by[(b, a)]["guest_ns"] for b in blocks) / 1e6 for a in names}
    return {"paired": paired, "noise_floor": floor, "median_guest_ms": medians}


def main():
    from result_schema import expected_values
    from scopt.run.deploy import deploy
    from scopt.run.launcher import create_run_dir, launch

    E = Path(os.environ["TREBUCHET_WORK"]); WORK = E / "scopt"; Q = os.environ["TREBUCHET_QEMU"]
    out = Path(sys.argv[1]); REPS = int(sys.argv[2]); V = sys.argv[3]
    ARMS, aa = arms_with_aa(dict(a.split("=", 1) for a in sys.argv[4:]),
                            os.environ.get("TREBUCHET_COMPARE_AA", "1") != "0")
    try:
        warning = protocol_check(REPS, os.environ)
    except paired_stats.UnderpoweredError as error:
        sys.exit(str(error).replace("--allow-few-blocks", "TREBUCHET_ALLOW_FEW_BLOCKS=1"))
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    min_blocks = int(os.environ.get("TREBUCHET_MIN_BLOCKS") or paired_stats.DEFAULT_MIN_BLOCKS)
    WL = os.environ["TREBUCHET_WORKLOAD"]; INP = Path(os.environ["TREBUCHET_INPUTS"]) / "final" / WL
    SCRATCH = Path("/dev/shm/trebuchet-compare"); shutil.rmtree(SCRATCH, ignore_errors=True); SCRATCH.mkdir()
    refs = {n: expected_values((WORK / V / "ref" / f"final-{n}.guest.norm").read_text().split("\n"), WL) for n in (1, 2, 3)}
    serial = 0

    def one(arm, n):
        nonlocal serial; serial += 1
        rd = create_run_dir(E / "cfg" / V / "fw.toml", SCRATCH / f"r{serial}", qemu=Q, label="compare")
        deploy(ARMS[arm], rd)
        doc = launch(rd, prove=True, inputs={"input": INP / f"final-{n}.bin"})
        failures = []
        if doc["exit_code"] != 0 or doc["timed_out"] or len(doc["bench"]) != 1:
            failures.append(f"exit code {doc['exit_code']}, timed out {doc['timed_out']}, {len(doc['bench'])} benchmark windows")
        try:
            got = expected_values(doc["guest_norm"], WL)
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
    rng = random.Random(SEED); samples = []
    doc = {"variant": V, "arms": {k: Path(v).name for k, v in ARMS.items()},
           "aa_control": {"arm": aa[0], "reference": aa[1]} if aa else None}
    for rep in range(REPS):
        for n in (1, 2, 3):
            order = list(ARMS); rng.shuffle(order)
            for pos, arm in enumerate(order):
                r = one(arm, n); r.update(arm=arm, input=f"final-{n}", block=rep * 3 + n - 1, position=pos); samples.append(r)
            out.write_text(json.dumps({**doc, "valid": all(s["ok"] for s in samples), "samples": samples}, indent=1))
    bad = [s for s in samples if not s["ok"]]
    for s in bad:
        print("FAILED", s["arm"], s["input"], "block", s["block"], "; ".join(s["failures"]))
    if bad:
        sys.exit(1)
    names = list(ARMS); base = names[0]
    result = summarize(samples, names, aa, min_blocks)
    protocol = paired_stats.protocol_record(
        blocks=REPS * 3, min_blocks=min_blocks,
        allow_few_blocks=os.environ.get("TREBUCHET_ALLOW_FEW_BLOCKS") == "1",
        rounds=paired_stats.DEFAULT_ROUNDS, seed=SEED, aa_control=doc["aa_control"],
        ratio=f"{base}/arm guest time; above 1 means the arm is faster", warning=warning)
    out.write_text(json.dumps({**doc, "valid": True, "samples": samples, **result,
                               "protocol": protocol}, indent=1))
    print(f"{'arm':12s} {'guest ms':>9s}   speedup over {base} [95 % interval] blocks>1 sign-p verdict")
    print(f"{base:12s} {result['median_guest_ms'][base]:9.1f}")
    rows = list(result["paired"].items())
    if result["noise_floor"]:
        rows.append((f"A/A {aa[0]}", result["noise_floor"]))
    for a, s in rows:
        ms = result["median_guest_ms"].get(a, result["median_guest_ms"].get(s.get("arm")))
        print(f"{a:12s} {ms:9.1f}   {s['median']:.3f}x [{s['ci95'][0]:.3f}, {s['ci95'][1]:.3f}] "
              f"{s['blocks_above_one']}/{s['n_blocks']} {s['sign_test_p']:.3g} {s['verdict']}")


if __name__ == "__main__":
    main()
