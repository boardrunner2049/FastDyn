#!/usr/bin/env python3
"""
probe_imu.py — synthetic IMU stimulus + automatic verification for VIO.

Two subcommands:

    inject   Write a fixed IMU sample repeatedly to a shm URI. Non-blocking
             wrt AHRS state. Useful for manual observation via MAVProxy.

    verify   Automated PASS/FAIL check. Injects a 30° accel tilt on a URI,
             reads MAVLink ATTITUDE before/during, restores original state,
             and prints whether AHRS actually responded to the injection.
             Uses accel tilt (immune to gyro-bias calibration).

Both bypass Gazebo entirely — they exercise the VIO channel + slave model +
firmware path only. Failure to respond in `verify` narrows the fault to
either the shm plumbing, the packer, or a non-primary IMU.

Prereq for verify:
    FastDyn running.
    MAVProxy running with UDP output forwarded to 127.0.0.1:14550, e.g.:
        mavproxy.py --master=/tmp/usart2_host --baudrate 57600 \\
                    --out=udp:127.0.0.1:14550

Examples:

    # Manual: fire 1.5 rad/s yaw for 5 s
    probe_imu.py inject boardrunner://vehicle/0/sensor/imu/2 --gyro-z 1.5

    # Automatic: run tilt probe on IMU 2, print PASS/FAIL
    probe_imu.py verify boardrunner://vehicle/0/sensor/imu/2

    # Automatic: verify all three IMU URIs, identify primary
    probe_imu.py verify --all
"""
import argparse
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    open_segment, read_header, read_imu_sample, sample_seq,
    uri_to_shm_path, uri_to_shm_name,
    inject_imu_stream, save_imu_slot, restore_imu_slot,
    MavlinkObserver, attitude_mean, DEFAULT_MAVLINK_URL,
    collect_scaled_imu_snapshot, drain_scaled_imu, SCALED_IMU_TYPES,
    BR_SHM_MAGIC, BR_SHM_VERSION, IMU_SAMPLE_SIZE,
)


def _fmt_flags(flags: int) -> str:
    parts = []
    if flags & 0x1: parts.append("HEALTHY")
    if flags & 0x2: parts.append("CALIBRATED")
    if flags & 0x4: parts.append("STALE")
    return "|".join(parts) if parts else "0"


def _validate_header(mm, uri) -> int:
    """Return 0 if header looks like a valid IMU segment, non-zero exit code otherwise."""
    hdr = read_header(mm)
    if hdr["magic"] != BR_SHM_MAGIC:
        print(f"[probe_imu] ERROR: {uri}: bad magic 0x{hdr['magic']:08x} "
              f"(expected 0x{BR_SHM_MAGIC:08x} — 'BRVS')", file=sys.stderr)
        return 4
    if hdr["version"] != BR_SHM_VERSION:
        print(f"[probe_imu] ERROR: {uri}: version {hdr['version']} != {BR_SHM_VERSION}",
              file=sys.stderr)
        return 4
    if hdr["payload_size"] != IMU_SAMPLE_SIZE:
        print(f"[probe_imu] ERROR: {uri}: payload_size {hdr['payload_size']} != "
              f"{IMU_SAMPLE_SIZE} (not an IMU segment?)", file=sys.stderr)
        return 4
    return 0


# =============================================================================
#  inject subcommand
# =============================================================================

def cmd_inject(args) -> int:
    try:
        shm_path = uri_to_shm_path(args.uri)
    except ValueError as e:
        print(f"[probe_imu] bad URI: {e}", file=sys.stderr); return 2
    if not os.path.exists(shm_path):
        print(f"[probe_imu] ERROR: {shm_path} does not exist "
              f"(is FastDyn running?)", file=sys.stderr); return 3

    mm = open_segment(shm_path)
    rc = _validate_header(mm, args.uri)
    if rc:
        return rc

    before = read_imu_sample(mm)
    print(f"[probe_imu] inject → {args.uri}")
    print(f"[probe_imu] BEFORE: seq={before['seq']}  "
          f"accel=({before['ax_mps2']:+.3f}, {before['ay_mps2']:+.3f}, "
          f"{before['az_mps2']:+.3f})  gyro=({before['gx_radps']:+.3f}, "
          f"{before['gy_radps']:+.3f}, {before['gz_radps']:+.3f})  "
          f"temp={before['temp_c']:.1f}  flags={_fmt_flags(before['flags'])}")
    print(f"[probe_imu] INJECT for {args.duration}s @ {args.rate_hz:.0f} Hz:")
    print(f"           accel=({args.accel_x:+.3f}, {args.accel_y:+.3f}, {args.accel_z:+.3f}) m/s²")
    print(f"           gyro =({args.gyro_x:+.3f}, {args.gyro_y:+.3f}, {args.gyro_z:+.3f}) rad/s")
    print(f"           temp ={args.temp_c:.1f} °C")

    n = inject_imu_stream(
        mm,
        args.accel_x, args.accel_y, args.accel_z,
        args.gyro_x,  args.gyro_y,  args.gyro_z,
        args.temp_c,
        args.duration, args.rate_hz,
    )

    print(f"[probe_imu] AFTER : injected {n} samples, seq={sample_seq(mm)}")
    mm.close()
    return 0


# =============================================================================
#  verify subcommand — automated PASS/FAIL
# =============================================================================

import time as _time  # local alias so `time` module isn't shadowed by anything else


def _angular_delta(a: float, b: float) -> float:
    """Shortest angular distance |a - b| in radians, wrap-safe (∈ [0, π])."""
    d = a - b
    return abs(math.atan2(math.sin(d), math.cos(d)))


def _run_background_inject(mm, ax, ay, az, gx, gy, gz, temp_c, max_duration_s):
    """Fire an inject_imu_stream in a daemon thread. Returns (thread, stop_event).

    Caller must .set() stop_event and .join() thread when done.
    """
    stop_evt = threading.Event()
    t = threading.Thread(
        target=inject_imu_stream,
        args=(mm, ax, ay, az, gx, gy, gz, temp_c,
              max_duration_s, 1000.0, 0x01, stop_evt),
        daemon=True,
    )
    t.start()
    return t, stop_evt


def _stop_background(thread, stop_event, timeout_s=2.0):
    stop_event.set()
    thread.join(timeout=timeout_s)


def _test_accel_raw(mm, obs, saved, verbose):
    """Test the accel bytes via RAW `SCALED_IMU` messages, not DCM attitude.

    Why: DCM's accel-correction filter resists sudden post-boot attitude
    changes (treats them as motion, dampens response). Raw SCALED_IMU_N
    is unfiltered — a change to shm shows up immediately in xacc/yacc/zacc
    (mG) for the corresponding IMU instance.

    Sequence:
      1. Snapshot xacc/yacc/zacc for all 3 SCALED_IMU_N instances (baseline).
      2. Inject a distinctive accel signature: ay = +5.0 m/s² (~510 mG).
         Keep az / gx / gy / gz at saved values so we don't spook AHRS.
      3. Drain queued messages, wait for injection to propagate.
      4. Snapshot again (response).
      5. For each instance, compute |Δaccel| magnitude in mG.
      6. Any instance showing |Δaccel| > 300 mG maps to this URI. PASS.
      7. If NONE respond, this URI's accel bytes aren't reaching firmware.
    """
    result = {'test': 'accel_raw', 'pass': False, 'reason': ''}

    # --- Baseline snapshot ---
    if verbose: print(f"  [accel_raw] collecting baseline SCALED_IMU_N snapshot ...",
                      flush=True)
    baseline = collect_scaled_imu_snapshot(obs, timeout_s=30.0)
    if not baseline:
        result['reason'] = "no SCALED_IMU messages arrived within 30s"
        return result
    if verbose:
        for inst in sorted(baseline):
            b = baseline[inst]
            print(f"  [accel_raw] baseline inst={inst}: "
                  f"accel=({b['xacc']:+d}, {b['yacc']:+d}, {b['zacc']:+d}) mG")

    # --- Inject distinctive ay ---
    INJECT_AY = 5.0  # m/s² → ~510 mG on whichever axis it lands after mount rot
    max_dur = 60.0
    if verbose:
        print(f"  [accel_raw] injecting ay={INJECT_AY} (az kept at "
              f"{saved['az_mps2']:+.2f}); wait 3s + drain ...", flush=True)
    thr, stop_evt = _run_background_inject(
        mm,
        saved['ax_mps2'], INJECT_AY, saved['az_mps2'],
        saved['gx_radps'], saved['gy_radps'], saved['gz_radps'],
        saved['temp_c'], max_dur,
    )
    _time.sleep(3.0)                 # let firmware read fresh shm
    drained = drain_scaled_imu(obs, 3.0)  # discard queued pre-injection msgs
    if verbose:
        print(f"  [accel_raw] drained {drained} stale SCALED_IMU msgs", flush=True)

    # --- Response snapshot ---
    if verbose: print(f"  [accel_raw] collecting response SCALED_IMU_N snapshot ...",
                      flush=True)
    response = collect_scaled_imu_snapshot(obs, timeout_s=30.0)
    _stop_background(thr, stop_evt)

    if not response:
        result['reason'] = "no SCALED_IMU messages after injection"
        return result

    # --- Assess: any instance with |Δaccel| > threshold responded ---
    THRESHOLD_MG = 300
    responding = []
    per_instance = {}
    for inst in sorted(set(baseline) & set(response)):
        b = baseline[inst]; r = response[inst]
        dx = r['xacc'] - b['xacc']
        dy = r['yacc'] - b['yacc']
        dz = r['zacc'] - b['zacc']
        mag = math.sqrt(dx*dx + dy*dy + dz*dz)
        per_instance[inst] = dict(dx=dx, dy=dy, dz=dz, mag_mG=mag)
        if verbose:
            print(f"  [accel_raw] SCALED_IMU_{inst}: "
                  f"Δaccel=({dx:+d}, {dy:+d}, {dz:+d}) mG, "
                  f"|Δ|={mag:.0f} mG")
        if mag > THRESHOLD_MG:
            responding.append(inst)

    result.update(
        baseline=baseline, response=response, per_instance=per_instance,
        responding_instances=responding, threshold_mg=THRESHOLD_MG,
    )
    if responding:
        result['pass'] = True
        if verbose:
            print(f"  [accel_raw] PASS: instance(s) {responding} responded "
                  f"(URI → SCALED_IMU_{responding[0]})", flush=True)
    else:
        result['reason'] = (f"no SCALED_IMU_N showed |Δaccel| > {THRESHOLD_MG} mG "
                            f"(accel bytes not reaching firmware)")
    return result


def _test_gyro_raw(mm, obs, saved, verbose):
    """Test the gyro bytes via RAW `SCALED_IMU_N` / RAW_IMU messages.

    Non-primary IMUs don't affect AHRS.yawspeed (AHRS only listens to the
    primary), so the old attitude-based gyro test only worked for imu/2.
    This raw-readout version works for all URIs.

    Sequence mirrors accel_raw:
      1. Snapshot xgyro/ygyro/zgyro for all instances (baseline).
      2. Inject a distinctive gz = +5.0 rad/s (~5000 mrad/s).
      3. Wait, drain, snapshot.
      4. Whichever instance shows |Δgyro| > 2000 mrad/s corresponds to URI. PASS.
    """
    result = {'test': 'gyro_raw', 'pass': False, 'reason': ''}

    # --- Baseline ---
    if verbose: print(f"  [gyro_raw] collecting baseline SCALED_IMU snapshot ...",
                      flush=True)
    baseline = collect_scaled_imu_snapshot(obs, timeout_s=30.0)
    if not baseline:
        result['reason'] = "no SCALED_IMU/RAW_IMU messages within 30s"
        return result
    if verbose:
        for inst in sorted(baseline):
            b = baseline[inst]
            print(f"  [gyro_raw] baseline inst={inst} ({b['msg_type']}): "
                  f"gyro=({b['xgyro']:+d}, {b['ygyro']:+d}, {b['zgyro']:+d}) mrad/s")

    # --- Inject distinctive gyro ---
    INJECT_GZ = 5.0  # rad/s → +5000 mrad/s
    max_dur = 60.0
    if verbose:
        print(f"  [gyro_raw] injecting gz={INJECT_GZ}; wait 3s + drain ...",
              flush=True)
    thr, stop_evt = _run_background_inject(
        mm,
        saved['ax_mps2'], saved['ay_mps2'], saved['az_mps2'],
        saved['gx_radps'], saved['gy_radps'], +INJECT_GZ,
        saved['temp_c'], max_dur,
    )
    _time.sleep(3.0)
    drained = drain_scaled_imu(obs, 3.0)
    if verbose:
        print(f"  [gyro_raw] drained {drained} stale msgs", flush=True)

    # --- Response ---
    if verbose: print(f"  [gyro_raw] collecting response snapshot ...", flush=True)
    response = collect_scaled_imu_snapshot(obs, timeout_s=30.0)
    _stop_background(thr, stop_evt)

    if not response:
        result['reason'] = "no SCALED_IMU/RAW_IMU messages after injection"
        return result

    # --- Assess ---
    # Threshold: 2000 mrad/s. Injected 5000; secondary IMUs still show it
    # in the raw stream since they're not filtered/bias-corrected there.
    THRESHOLD_MRAD_S = 2000
    responding = []
    per_instance = {}
    for inst in sorted(set(baseline) & set(response)):
        b = baseline[inst]; r = response[inst]
        dx = r['xgyro'] - b['xgyro']
        dy = r['ygyro'] - b['ygyro']
        dz = r['zgyro'] - b['zgyro']
        mag = math.sqrt(dx*dx + dy*dy + dz*dz)
        per_instance[inst] = dict(dx=dx, dy=dy, dz=dz, mag_mrad_s=mag)
        if verbose:
            print(f"  [gyro_raw] SCALED_IMU_{inst} ({r['msg_type']}): "
                  f"Δgyro=({dx:+d}, {dy:+d}, {dz:+d}) mrad/s, "
                  f"|Δ|={mag:.0f}")
        if mag > THRESHOLD_MRAD_S:
            responding.append(inst)

    result.update(
        baseline=baseline, response=response, per_instance=per_instance,
        responding_instances=responding, threshold_mrad_s=THRESHOLD_MRAD_S,
    )
    if responding:
        result['pass'] = True
        if verbose:
            print(f"  [gyro_raw] PASS: instance(s) {responding} responded "
                  f"(URI → instance {responding[0]})", flush=True)
    else:
        result['reason'] = (f"no instance showed |Δgyro| > {THRESHOLD_MRAD_S} "
                            f"mrad/s (gyro bytes not reaching firmware)")
    return result


def _verify_one(uri: str, obs: MavlinkObserver,
                hold_s: float, settle_s: float,
                verbose: bool = True) -> dict:
    """Verify one URI with two sub-tests (accel-flip + gyro-flip).

    Overall PASS requires BOTH sub-tests to pass. Restores shm state at end.
    """
    result = {'uri': uri, 'result': 'error', 'reason': '',
              'accel': None, 'gyro': None}

    try:
        shm_path = uri_to_shm_path(uri)
    except ValueError as e:
        result['reason'] = f"bad URI: {e}"; return result
    if not os.path.exists(shm_path):
        result['reason'] = "shm segment does not exist (slave never attached?)"
        return result

    mm = open_segment(shm_path)
    hdr_rc = _validate_header(mm, uri)
    if hdr_rc:
        result['reason'] = "bad shm header (see stderr)"; mm.close(); return result

    saved = save_imu_slot(mm)
    if verbose:
        print(f"[verify {uri}]")
        print(f"  saved state: accel=({saved['ax_mps2']:+.3f}, "
              f"{saved['ay_mps2']:+.3f}, {saved['az_mps2']:+.3f})  "
              f"gyro=({saved['gx_radps']:+.3f}, {saved['gy_radps']:+.3f}, "
              f"{saved['gz_radps']:+.3f})  seq={saved['seq']}")

    try:
        # --- Test 1: raw accel (via SCALED_IMU_N, bypasses DCM) ---
        accel_r = _test_accel_raw(mm, obs, saved, verbose)
        result['accel'] = accel_r

        # Restore between tests
        restore_imu_slot(mm, saved)
        _time.sleep(1.0)
        obs.collect_attitude(0.3)

        # --- Test 2: raw gyro (via SCALED_IMU_N/RAW_IMU, bypasses AHRS) ---
        gyro_r = _test_gyro_raw(mm, obs, saved, verbose)
        result['gyro'] = gyro_r

        # Overall verdict
        if accel_r['pass'] and gyro_r['pass']:
            result['result'] = 'PASS'
        else:
            failed = []
            if not accel_r['pass']: failed.append(f"accel: {accel_r.get('reason','')}")
            if not gyro_r['pass']:  failed.append(f"gyro: {gyro_r.get('reason','')}")
            result['result'] = 'fail'
            result['reason'] = '; '.join(failed)
    finally:
        restore_imu_slot(mm, saved)
        mm.close()

    return result


def cmd_verify(args) -> int:
    if args.all:
        uris = [
            "boardrunner://vehicle/0/sensor/imu/0",
            "boardrunner://vehicle/0/sensor/imu/1",
            "boardrunner://vehicle/0/sensor/imu/2",
        ]
    else:
        if not args.uri:
            print("[probe_imu] verify: specify a URI or --all", file=sys.stderr)
            return 2
        uris = [args.uri]

    # Open MAVLink ONCE and reuse across URIs — avoids the port-rebind race.
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
            r = _verify_one(uri, obs, args.hold_s, args.settle_s, verbose=True)
            results.append(r)
            print()
    finally:
        obs.close()

    # Summary table
    print("=" * 100)
    print(f"{'URI':<52} {'overall':<8} {'accel mG':>10} {'gyro mrad/s':>12} {'inst→':>8}")
    print("-" * 100)
    best = None
    for r in results:
        a = r.get('accel') or {}
        g = r.get('gyro') or {}
        pi_a = a.get('per_instance') or {}
        pi_g = g.get('per_instance') or {}
        accel_mag = max((v.get('mag_mG', 0) for v in pi_a.values()), default=0)
        gyro_mag = max((v.get('mag_mrad_s', 0) for v in pi_g.values()), default=0)
        # instance mapping — prefer accel's responding instance, fall back to gyro's
        responding = (a.get('responding_instances')
                       or g.get('responding_instances') or [])
        inst_str = f"IMU_{responding[0]}" if responding else "?"
        combined = accel_mag + gyro_mag / 10  # roughly commensurate magnitudes
        print(f"{r['uri']:<52} {r['result']:<8} "
              f"{accel_mag:>10.0f} {gyro_mag:>12.0f} {inst_str:>8}")
        if r['result'] == 'PASS' and (best is None or combined > best.get('_combined', 0)):
            best = dict(r); best['_combined'] = combined
    print("=" * 100)

    passing = [r for r in results if r['result'] == 'PASS']
    if best is not None:
        b_a = best.get('accel') or {}
        insts = b_a.get('responding_instances') or []
        inst_note = f" (maps to SCALED_IMU_{insts[0]})" if insts else ""
        print(f"Primary IMU (largest combined response): {best['uri']}{inst_note}")

    for r in results:
        if r['result'] in ('fail', 'error') and r.get('reason'):
            print(f"  [{r['uri']}] {r['result']}: {r['reason']}")

    return 0 if passing else 1


# =============================================================================
#  show subcommand — print current shm state for a URI (or all)
# =============================================================================

def _show_one(uri: str) -> int:
    try:
        shm_path = uri_to_shm_path(uri)
    except ValueError as e:
        print(f"[show] bad URI {uri!r}: {e}", file=sys.stderr); return 2
    if not os.path.exists(shm_path):
        print(f"[show] {uri}: shm MISSING ({shm_path})")
        return 3
    mm = open_segment(shm_path)
    hdr = read_header(mm)
    s = read_imu_sample(mm)
    print(f"[show] {uri}")
    print(f"       shm  = {shm_path}")
    print(f"       hdr  = magic=0x{hdr['magic']:08x} ver={hdr['version']} "
          f"payload={hdr['payload_size']}B seq={hdr['seq']}")
    print(f"       samp = seq={s['seq']} accel=({s['ax_mps2']:+.3f}, "
          f"{s['ay_mps2']:+.3f}, {s['az_mps2']:+.3f}) "
          f"gyro=({s['gx_radps']:+.3f}, {s['gy_radps']:+.3f}, "
          f"{s['gz_radps']:+.3f}) temp={s['temp_c']:.1f} "
          f"flags={_fmt_flags(s['flags'])}")
    mm.close()
    return 0


def cmd_show(args) -> int:
    if args.all:
        rc = 0
        for uri in ["boardrunner://vehicle/0/sensor/imu/0",
                    "boardrunner://vehicle/0/sensor/imu/1",
                    "boardrunner://vehicle/0/sensor/imu/2"]:
            rc |= _show_one(uri)
        return rc
    if not args.uri:
        print("[show] specify a URI or --all", file=sys.stderr); return 2
    return _show_one(args.uri)


# =============================================================================
#  debug subcommand — dump all message types arriving over MAVLink
# =============================================================================

def cmd_debug(args) -> int:
    """Print a histogram of MAVLink message types received over N seconds.

    Purpose: diagnose why `verify` sees 0 ATTITUDE messages. If HEARTBEAT
    arrives but ATTITUDE doesn't, MAVProxy is filtering the forward or the
    endpoint isn't receiving the full stream. If nothing arrives, the UDP
    forward isn't set up.
    """
    from collections import Counter

    print(f"[debug] opening MAVLink: {args.mavlink_url}", flush=True)
    try:
        obs = MavlinkObserver(args.mavlink_url).connect()
    except RuntimeError as e:
        print(f"[debug] ERROR: {e}", file=sys.stderr); return 1
    print(f"[debug] heartbeat OK; counting messages for {args.duration}s ...",
          flush=True)

    import time
    counts = Counter()
    end = time.monotonic() + args.duration
    # Access the raw mav connection to get any message type
    mav = obs._mav
    while time.monotonic() < end:
        remaining = end - time.monotonic()
        msg = mav.recv_match(blocking=True, timeout=min(0.3, max(0.01, remaining)))
        if msg is not None:
            counts[msg.get_type()] += 1

    obs.close()
    total = sum(counts.values())
    print(f"[debug] total messages: {total}")
    if not counts:
        print("[debug] NO messages received.")
        print("  Likely causes:")
        print("  - MAVProxy `--out=udp:127.0.0.1:14550` not set up")
        print("  - MAVProxy running but not forwarding this endpoint")
        print("  - Firewall blocking loopback UDP (unusual)")
    else:
        print("[debug] per-type counts:")
        for t, n in counts.most_common():
            print(f"    {t:32s} {n}")
        if 'ATTITUDE' not in counts and 'HEARTBEAT' in counts:
            print("\n[debug] DIAGNOSIS: HEARTBEAT arrives but ATTITUDE doesn't.")
            print("  MAVProxy is forwarding SOME messages but filtering ATTITUDE.")
            print("  Try in MAVProxy console:")
            print("    MAV> set streamrate 10")
            print("    MAV> long SET_MESSAGE_INTERVAL 30 100000")
            print("  Or use the direct-PTY mode (kill MAVProxy first).")
    return 0


# =============================================================================
#  main
# =============================================================================

def main(argv=None):
    p = argparse.ArgumentParser(
        description="IMU synthetic stimulus + verification for VIO channel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if __doc__ else "",
    )
    subs = p.add_subparsers(dest="cmd", required=True)

    # inject
    pi = subs.add_parser("inject", help="raw injection (manual observation)")
    pi.add_argument("uri", help="boardrunner://vehicle/<v>/sensor/imu/<n>")
    pi.add_argument("--accel-x", type=float, default=0.0, metavar="M/S2")
    pi.add_argument("--accel-y", type=float, default=0.0, metavar="M/S2")
    pi.add_argument("--accel-z", type=float, default=-9.8, metavar="M/S2",
                    help="default -9.8 (chip Z-down at rest)")
    pi.add_argument("--gyro-x", type=float, default=0.0, metavar="RAD/S")
    pi.add_argument("--gyro-y", type=float, default=0.0, metavar="RAD/S")
    pi.add_argument("--gyro-z", type=float, default=0.0, metavar="RAD/S")
    pi.add_argument("--temp-c", type=float, default=25.0, metavar="DEG_C")
    pi.add_argument("--duration", type=float, default=5.0, metavar="SEC")
    pi.add_argument("--rate-hz", type=float, default=1000.0, metavar="HZ")
    pi.set_defaults(func=cmd_inject)

    # verify
    pv = subs.add_parser("verify",
                         help="automated PASS/FAIL via MAVLink ATTITUDE")
    grp = pv.add_mutually_exclusive_group(required=False)
    grp.add_argument("uri", nargs="?",
                     help="boardrunner://vehicle/<v>/sensor/imu/<n>")
    grp.add_argument("--all", action="store_true",
                     help="verify imu/0..imu/2 sequentially, report primary")
    pv.add_argument("--hold-s", type=float, default=8.0,
                    help="pre-measurement settle window after each injection "
                         "(default 8s; DCM's accel filter takes ~5s to converge)")
    pv.add_argument("--settle-s", type=float, default=4.0,
                    help="ATTITUDE collection window (soft; hard timeout is 6× "
                         "settle_s to cope with slow MAVLink rates) (default 4s)")
    pv.add_argument("--mavlink-url", default=DEFAULT_MAVLINK_URL,
                    help=f"pymavlink URL (default: {DEFAULT_MAVLINK_URL})")
    pv.set_defaults(func=cmd_verify)

    # show — inspect current shm state
    ps = subs.add_parser("show",
                         help="print current shm state for a URI (or --all)")
    grp2 = ps.add_mutually_exclusive_group(required=False)
    grp2.add_argument("uri", nargs="?",
                      help="boardrunner://vehicle/<v>/sensor/imu/<n>")
    grp2.add_argument("--all", action="store_true",
                      help="show imu/0..imu/2")
    ps.set_defaults(func=cmd_show)

    # debug — dump message types
    pd = subs.add_parser("debug",
                         help="dump MAVLink message type histogram for N seconds")
    pd.add_argument("--duration", type=float, default=10.0,
                    help="collection duration in seconds (default 10)")
    pd.add_argument("--mavlink-url", default=DEFAULT_MAVLINK_URL,
                    help=f"pymavlink URL (default: {DEFAULT_MAVLINK_URL})")
    pd.set_defaults(func=cmd_debug)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
