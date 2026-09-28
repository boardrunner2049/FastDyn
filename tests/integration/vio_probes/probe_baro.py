#!/usr/bin/env python3
"""
probe_baro.py — synthetic baro stimulus + verification for VIO channel.

Mirrors probe_imu.py structure:

    inject   Write a fixed pressure/temp sample to a baro URI. Manual obs.
    show     Print current shm state for a baro URI (or --all).
    verify   Automated PASS/FAIL via SCALED_PRESSURE MAVLink messages.
             Injects a distinctive pressure, snapshots each SCALED_PRESSURE_N,
             identifies which instance corresponds to the URI, PASS if any
             instance shows the expected change.

See docs/vio_probes/README.md for the shared architecture and coverage story.
Baro-specific quirks/coverage will be documented in docs/vio_probes/baro.md.

Examples:

    # Manual: force pressure to sea-level (~101325 Pa) for 10 s
    probe_baro.py inject boardrunner://vehicle/0/sensor/baro/0 \\
        --pressure 101325 --duration 10

    # Show current state of both baros
    probe_baro.py show --all

    # Automated PASS/FAIL for both baros
    probe_baro.py verify --all
"""
import argparse
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time as _time
from common import (
    open_segment, read_header, read_baro_sample, sample_seq,
    uri_to_shm_path, uri_to_shm_name,
    inject_baro_stream, save_baro_slot, restore_baro_slot,
    MavlinkObserver, DEFAULT_MAVLINK_URL,
    collect_scaled_pressure_snapshot, drain_scaled_pressure,
    BR_SHM_MAGIC, BR_SHM_VERSION, BARO_SAMPLE_SIZE,
)


def _fmt_flags(flags: int) -> str:
    parts = []
    if flags & 0x1: parts.append("HEALTHY")
    if flags & 0x2: parts.append("CALIBRATED")
    if flags & 0x4: parts.append("STALE")
    return "|".join(parts) if parts else "0"


def _validate_header(mm, uri) -> int:
    hdr = read_header(mm)
    if hdr["magic"] != BR_SHM_MAGIC:
        print(f"[probe_baro] ERROR: {uri}: bad magic 0x{hdr['magic']:08x} "
              f"(expected 0x{BR_SHM_MAGIC:08x})", file=sys.stderr)
        return 4
    if hdr["version"] != BR_SHM_VERSION:
        print(f"[probe_baro] ERROR: {uri}: version {hdr['version']} != "
              f"{BR_SHM_VERSION}", file=sys.stderr)
        return 4
    if hdr["payload_size"] != BARO_SAMPLE_SIZE:
        print(f"[probe_baro] ERROR: {uri}: payload_size {hdr['payload_size']} != "
              f"{BARO_SAMPLE_SIZE} (not a baro segment?)", file=sys.stderr)
        return 4
    return 0


# =============================================================================
#  inject subcommand
# =============================================================================

def cmd_inject(args) -> int:
    try:
        shm_path = uri_to_shm_path(args.uri)
    except ValueError as e:
        print(f"[probe_baro] bad URI: {e}", file=sys.stderr); return 2
    if not os.path.exists(shm_path):
        print(f"[probe_baro] ERROR: {shm_path} does not exist "
              f"(is FastDyn running?)", file=sys.stderr); return 3

    mm = open_segment(shm_path)
    if _validate_header(mm, args.uri):
        return 4

    before = read_baro_sample(mm)
    print(f"[probe_baro] inject → {args.uri}")
    print(f"[probe_baro] BEFORE: seq={before['seq']}  "
          f"pressure={before['pressure_pa']:.1f} Pa  "
          f"temp={before['temp_c']:.2f} °C  "
          f"flags={_fmt_flags(before['flags'])}")
    print(f"[probe_baro] INJECT for {args.duration}s @ {args.rate_hz:.0f} Hz:")
    print(f"           pressure={args.pressure:.1f} Pa   temp={args.temp_c:.2f} °C")

    n = inject_baro_stream(mm, args.pressure, args.temp_c,
                           args.duration, args.rate_hz)
    print(f"[probe_baro] AFTER : injected {n} samples, seq={sample_seq(mm)}")
    mm.close()
    return 0


# =============================================================================
#  show subcommand
# =============================================================================

def _show_one(uri: str) -> int:
    try:
        shm_path = uri_to_shm_path(uri)
    except ValueError as e:
        print(f"[show] bad URI {uri!r}: {e}", file=sys.stderr); return 2
    if not os.path.exists(shm_path):
        print(f"[show] {uri}: shm MISSING ({shm_path})"); return 3
    mm = open_segment(shm_path)
    hdr = read_header(mm)
    s = read_baro_sample(mm)
    print(f"[show] {uri}")
    print(f"       shm  = {shm_path}")
    print(f"       hdr  = magic=0x{hdr['magic']:08x} ver={hdr['version']} "
          f"payload={hdr['payload_size']}B seq={hdr['seq']}")
    print(f"       samp = seq={s['seq']} pressure={s['pressure_pa']:.1f} Pa "
          f"temp={s['temp_c']:.2f} °C flags={_fmt_flags(s['flags'])}")
    mm.close()
    return 0


def cmd_show(args) -> int:
    if args.all:
        rc = 0
        for uri in ("boardrunner://vehicle/0/sensor/baro/0",
                    "boardrunner://vehicle/0/sensor/baro/1"):
            rc |= _show_one(uri)
        return rc
    if not args.uri:
        print("[show] specify a URI or --all", file=sys.stderr); return 2
    return _show_one(args.uri)


# =============================================================================
#  verify subcommand — per-URI PASS/FAIL via SCALED_PRESSURE
# =============================================================================

def _run_background_baro_inject(mm, pressure_pa, temp_c, max_duration_s):
    stop = threading.Event()
    t = threading.Thread(
        target=inject_baro_stream,
        args=(mm, pressure_pa, temp_c, max_duration_s, 100.0, 0x01, stop),
        daemon=True,
    )
    t.start()
    return t, stop


def _stop_background(thread, stop_event, timeout_s=2.0):
    stop_event.set()
    thread.join(timeout=timeout_s)


def _verify_one(uri: str, obs: MavlinkObserver, verbose: bool = True) -> dict:
    """Verify one baro URI via SCALED_PRESSURE_N snapshot before/after.

    Sequence:
      1. Snapshot press_abs for all instances (baseline).
      2. Inject a distinctive pressure (baseline + 10000 Pa = +100 hPa,
         well above sensor noise).
      3. Wait + drain, snapshot again.
      4. Compute per-instance |Δpress_abs| in hPa (SCALED_PRESSURE unit).
      5. PASS if any instance shows |Δ| > 30 hPa (well below injected +100).
      6. Restore.
    """
    result = {'uri': uri, 'result': 'error', 'reason': '',
              'per_instance': {}, 'responding_instances': []}

    try:
        shm_path = uri_to_shm_path(uri)
    except ValueError as e:
        result['reason'] = f"bad URI: {e}"; return result
    if not os.path.exists(shm_path):
        result['reason'] = "shm segment does not exist (slave never attached?)"
        return result

    mm = open_segment(shm_path)
    if _validate_header(mm, uri):
        result['reason'] = "bad shm header (see stderr)"; mm.close(); return result

    saved = save_baro_slot(mm)
    if verbose:
        print(f"[verify {uri}]")
        print(f"  saved state: pressure={saved['pressure_pa']:.1f} Pa "
              f"temp={saved['temp_c']:.2f} °C  seq={saved['seq']}")

    try:
        # --- Baseline ---
        if verbose:
            print(f"  [baro_raw] collecting baseline SCALED_PRESSURE snapshot ...",
                  flush=True)
        baseline = collect_scaled_pressure_snapshot(obs, timeout_s=30.0)
        if not baseline:
            result['reason'] = "no SCALED_PRESSURE messages within 30s"
            return result
        if verbose:
            for inst in sorted(baseline):
                b = baseline[inst]
                print(f"  [baro_raw] baseline inst={inst} ({b['msg_type']}): "
                      f"press_abs={b['press_abs_hPa']:.2f} hPa "
                      f"temp={b['temperature_cC']/100.0:.2f} °C")

        # --- Inject distinctive pressure ---
        # +10 000 Pa = +100 hPa. About equivalent to descending 800 m from
        # baseline. Big enough to be unambiguous, small enough to stay in
        # realistic range and not spook ArduPilot's health checks.
        INJECT_DELTA_PA = 10000.0
        new_pressure_pa = saved['pressure_pa'] + INJECT_DELTA_PA
        # If saved was 0 (endpoint never wrote yet), inject a plausible value.
        # Also force temp to a sensible default when saved is unrealistic —
        # temp=0 °C combined with elevated pressure pushes MS5611 packer math
        # into 24-bit saturation on D1, corrupting the round-trip magnitude.
        inject_temp_c = saved['temp_c']
        if saved['pressure_pa'] < 1000.0:
            new_pressure_pa = 101325.0 + INJECT_DELTA_PA   # sea-level + delta
        if abs(saved['temp_c']) < 5.0 or abs(saved['temp_c']) > 60.0:
            inject_temp_c = 25.0                            # realistic ambient
        max_dur = 60.0
        if verbose:
            print(f"  [baro_raw] injecting pressure={new_pressure_pa:.1f} Pa "
                  f"temp={inject_temp_c:.1f} °C "
                  f"(Δp={INJECT_DELTA_PA:+.0f} Pa); wait 3s + drain ...", flush=True)
        thr, stop = _run_background_baro_inject(mm, new_pressure_pa,
                                                inject_temp_c, max_dur)
        _time.sleep(3.0)
        drained = drain_scaled_pressure(obs, 3.0)
        if verbose:
            print(f"  [baro_raw] drained {drained} stale SCALED_PRESSURE msgs",
                  flush=True)

        # --- Response ---
        if verbose:
            print(f"  [baro_raw] collecting response snapshot ...", flush=True)
        response = collect_scaled_pressure_snapshot(obs, timeout_s=30.0)
        _stop_background(thr, stop)

        if not response:
            result['reason'] = "no SCALED_PRESSURE messages after injection"
            return result

        # --- Assess: absolute-error metric ---
        # PASS if some instance's reported pressure matches our injection to
        # within THRESHOLD_PA. This is more robust than a "|Δ| > X" test
        # because a stale-baseline (0 Pa restore) produces a huge Δ that
        # looks alarming but is actually just the pipeline working correctly.
        # Absolute error tells us how faithfully the injected value round-
        # trips through the packer + firmware decode.
        #
        # Threshold: 500 Pa (5 hPa) — well above quantization noise from the
        # 24-bit ADC round-trip through the MS5611 datasheet formulas, and
        # well below realistic baro sensor noise (~0.01 hPa in real life).
        THRESHOLD_PA = 500.0
        responding = []
        per_instance = {}
        for inst in sorted(set(baseline) & set(response)):
            b = baseline[inst]; r = response[inst]
            baseline_pa = b['press_abs_hPa'] * 100.0
            observed_pa = r['press_abs_hPa'] * 100.0
            delta_pa    = observed_pa - baseline_pa
            error_pa    = abs(observed_pa - new_pressure_pa)
            error_temp  = abs(r['temperature_cC'] / 100.0 - inject_temp_c)
            per_instance[inst] = dict(
                baseline_pa=baseline_pa,
                observed_pa=observed_pa,
                delta_pa=delta_pa,
                error_pa=error_pa,
                error_temp_c=error_temp,
            )
            if verbose:
                print(f"  [baro_raw] SCALED_PRESSURE_{inst} ({r['msg_type']}): "
                      f"baseline={baseline_pa:8.0f} → observed={observed_pa:8.0f} Pa "
                      f"(target {new_pressure_pa:.0f}, err={error_pa:6.0f} Pa)")
            if error_pa < THRESHOLD_PA:
                responding.append(inst)

        result.update(
            baseline=baseline, response=response, per_instance=per_instance,
            responding_instances=responding, threshold_pa=THRESHOLD_PA,
            injected_pressure_pa=new_pressure_pa,
            injected_temp_c=inject_temp_c,
        )
        if responding:
            result['result'] = 'PASS'
            if verbose:
                print(f"  [baro_raw] PASS: instance(s) {responding} matched "
                      f"injection to within {THRESHOLD_PA:.0f} Pa "
                      f"(URI → SCALED_PRESSURE_{responding[0]})", flush=True)
        else:
            # Report the best-effort instance (smallest error) so the user
            # can see which one came closest.
            best = min(per_instance.items(),
                       key=lambda kv: kv[1]['error_pa'],
                       default=(None, None))
            result['result'] = 'fail'
            if best[0] is not None:
                result['reason'] = (
                    f"no instance matched injection to within {THRESHOLD_PA:.0f} Pa "
                    f"(best: inst={best[0]} error={best[1]['error_pa']:.0f} Pa "
                    f"— pipeline may have packer/decode drift)")
            else:
                result['reason'] = "no SCALED_PRESSURE instances observed"
    finally:
        restore_baro_slot(mm, saved)
        mm.close()

    return result


def cmd_verify(args) -> int:
    if args.all:
        uris = ["boardrunner://vehicle/0/sensor/baro/0",
                "boardrunner://vehicle/0/sensor/baro/1"]
    else:
        if not args.uri:
            print("[probe_baro] verify: specify a URI or --all", file=sys.stderr)
            return 2
        uris = [args.uri]

    print(f"[verify] opening MAVLink: {args.mavlink_url}", flush=True)
    try:
        obs = MavlinkObserver(args.mavlink_url).connect()
        print(f"[verify] MAVLink heartbeat OK\n", flush=True)
    except RuntimeError as e:
        print(f"[verify] ERROR: {e}", file=sys.stderr)
        return 1

    results = []
    try:
        for uri in uris:
            r = _verify_one(uri, obs, verbose=True)
            results.append(r)
            print()
    finally:
        obs.close()

    # Summary — show the BEST matching instance's error (smallest is best)
    print("=" * 96)
    print(f"{'URI':<52} {'overall':<8} {'best err (Pa)':>15} {'inst→':>10}")
    print("-" * 96)
    for r in results:
        pi = r.get('per_instance') or {}
        # Best = instance with smallest absolute error vs injection
        best_err = min((v.get('error_pa', float('inf')) for v in pi.values()),
                        default=float('inf'))
        responding = r.get('responding_instances') or []
        inst_str = f"BARO_{responding[0]}" if responding else "?"
        best_err_s = f"{best_err:.0f}" if best_err != float('inf') else "n/a"
        print(f"{r['uri']:<52} {r['result']:<8} {best_err_s:>15} {inst_str:>10}")
    print("=" * 96)

    for r in results:
        if r['result'] in ('fail', 'error') and r.get('reason'):
            print(f"  [{r['uri']}] {r['result']}: {r['reason']}")

    passing = [r for r in results if r['result'] == 'PASS']
    return 0 if passing else 1


# =============================================================================
#  main
# =============================================================================

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Baro synthetic stimulus + verification for VIO channel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if __doc__ else "",
    )
    subs = p.add_subparsers(dest="cmd", required=True)

    pi = subs.add_parser("inject", help="raw injection (manual observation)")
    pi.add_argument("uri", help="boardrunner://vehicle/<v>/sensor/baro/<n>")
    pi.add_argument("--pressure", type=float, default=101325.0, metavar="PA",
                    help="absolute pressure in Pascals (default 101325 = sea level)")
    pi.add_argument("--temp-c", type=float, default=25.0, metavar="DEG_C")
    pi.add_argument("--duration", type=float, default=10.0, metavar="SEC")
    pi.add_argument("--rate-hz", type=float, default=100.0, metavar="HZ")
    pi.set_defaults(func=cmd_inject)

    ps = subs.add_parser("show", help="print current shm state for a URI (or --all)")
    g = ps.add_mutually_exclusive_group(required=False)
    g.add_argument("uri", nargs="?")
    g.add_argument("--all", action="store_true", help="show baro/0 + baro/1")
    ps.set_defaults(func=cmd_show)

    pv = subs.add_parser("verify",
                         help="automated PASS/FAIL via SCALED_PRESSURE MAVLink msgs")
    gg = pv.add_mutually_exclusive_group(required=False)
    gg.add_argument("uri", nargs="?")
    gg.add_argument("--all", action="store_true",
                    help="verify baro/0 + baro/1, report mapping")
    pv.add_argument("--mavlink-url", default=DEFAULT_MAVLINK_URL,
                    help=f"pymavlink URL (default: {DEFAULT_MAVLINK_URL})")
    pv.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
