#!/usr/bin/env python3
"""
probe_pwm.py — Boardrunner VIO actuator (PWM out) READ-side probe.

The IOMCU endpoint (boardrunner/boardrunner_sdk/model/ap_iomcu_uart_endpoint.py)
captures firmware PWM writes on USART6 (IOMCU protocol pages 2/3/54) and
publishes each of the 16 output channels to a per-channel shm segment at
boardrunner://vehicle/0/actuator/pwm/<N>. This probe reads those segments.

Direction is REVERSED versus sensor probes: firmware is the WRITER, we are
the READER. There is no `inject` subcommand — a probe cannot make the
firmware say "here is my PWM command".

Subcommands:

    show       Print current PWM for one URI, one channel, or --all.
    verify     Two-part activity + range check on one or all channels.
               PASS iff:
                 (1) header seq climbs between two samples spaced --duration
                     seconds apart (firmware is actively writing), AND
                 (2) pulse_us is in a plausible PWM range (default 800..2200).

Examples:

    # Show all 16 channels once
    probe_pwm.py show --all

    # Show a single channel
    probe_pwm.py show --channel 2

    # Verify channel 2 (throttle typically) is live and in-range
    probe_pwm.py verify --channel 2

    # Verify all 16 channels
    probe_pwm.py verify --all --duration 3
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    open_segment, read_header,
    read_actuator_sample, read_actuator_sample_stable,
    uri_to_shm_path,
    BR_SHM_MAGIC, BR_SHM_VERSION, ACTUATOR_SAMPLE_SIZE,
    BR_URI_SCHEME,
)

DEFAULT_URI_BASE = "boardrunner://vehicle/0/actuator/pwm"
NUM_PWM_CHANNELS = 16
PLAUSIBLE_PWM_MIN = 800    # matches AP_IOMCU_uart_endpoint clamp
PLAUSIBLE_PWM_MAX = 2200


def _fmt_flags(flags: int) -> str:
    parts = []
    if flags & 0x1: parts.append("HEALTHY")
    if flags & 0x2: parts.append("CALIBRATED")
    if flags & 0x4: parts.append("STALE")
    return "|".join(parts) if parts else "0"


def _resolve_uri(args) -> str:
    """Resolve --uri OR --channel N to a full URI."""
    if getattr(args, "uri", None):
        return args.uri
    if getattr(args, "channel", None) is not None:
        base = getattr(args, "uri_base", DEFAULT_URI_BASE) or DEFAULT_URI_BASE
        return "%s/%d" % (base.rstrip("/"), args.channel)
    return None


def _all_channel_uris(uri_base: str = DEFAULT_URI_BASE):
    base = uri_base.rstrip("/")
    return ["%s/%d" % (base, i) for i in range(NUM_PWM_CHANNELS)]


def _open_or_none(uri):
    """Return (mm, err_msg). mm is None on failure, err_msg says why."""
    try:
        shm_path = uri_to_shm_path(uri)
    except ValueError as e:
        return None, "URI parse error: %s" % (e,)
    if not os.path.exists(shm_path):
        return None, ("shm segment %s does not exist. "
                      "Endpoint may not be publishing to this URI." % (shm_path,))
    try:
        mm = open_segment(shm_path, size=4096)
    except Exception as e:
        return None, "open_segment failed: %s" % (e,)
    return mm, None


def _validate_header(mm, uri) -> str:
    """Return an error string, or None if header looks good."""
    hdr = read_header(mm)
    if hdr["magic"] != BR_SHM_MAGIC:
        return ("bad magic 0x%08x (expected 0x%08x)"
                % (hdr["magic"], BR_SHM_MAGIC))
    if hdr["version"] != BR_SHM_VERSION:
        return "version %d != %d" % (hdr["version"], BR_SHM_VERSION)
    if hdr["payload_size"] != ACTUATOR_SAMPLE_SIZE:
        return ("payload_size %d != %d (not an actuator segment?)"
                % (hdr["payload_size"], ACTUATOR_SAMPLE_SIZE))
    return None


# =============================================================================
#  show subcommand
# =============================================================================

def _show_one(uri, mm) -> int:
    err = _validate_header(mm, uri)
    if err is not None:
        print("[probe_pwm] %s ERROR: %s" % (uri, err), file=sys.stderr)
        return 4
    hdr = read_header(mm)
    sample = read_actuator_sample(mm)
    print("%s" % uri)
    print("  header    seq=%d writer_pid=%d last_write_ns=%d"
          % (hdr["seq"], hdr["writer_pid"], hdr["last_write_ns"]))
    print("  payload   seq=%d channel=%d pulse_us=%d flags=%s ts_ns=%d"
          % (sample["seq"], sample["channel"], sample["pulse_us"],
             _fmt_flags(sample["flags"]), sample["ts_ns"]))
    return 0


def cmd_show(args) -> int:
    if args.all:
        uris = _all_channel_uris(args.uri_base)
    else:
        uri = _resolve_uri(args)
        if uri is None:
            print("[probe_pwm] show: pass --uri, --channel N, or --all",
                  file=sys.stderr)
            return 2
        uris = [uri]

    rc = 0
    for uri in uris:
        mm, err = _open_or_none(uri)
        if mm is None:
            print("[probe_pwm] %s: %s" % (uri, err), file=sys.stderr)
            rc = 4
            continue
        try:
            rc |= _show_one(uri, mm)
        finally:
            mm.close()
    return rc


# =============================================================================
#  Snapshot helpers — read all requested channels once, close, return dicts.
# =============================================================================

def _snapshot_channels(uris):
    """Open each URI, read one seqlock-safe sample and header, close.

    Returns a dict:
        {uri: {"present": bool, "err": str_or_None,
               "hdr": {...} or None, "sample": {...} or None}}

    Doing all opens in a tight loop (no per-channel sleeps) so the whole
    snapshot represents state at approximately one instant. The subsequent
    snapshot compares against this to compute deltas.
    """
    out = {}
    for uri in uris:
        entry = {"present": False, "err": None, "hdr": None, "sample": None}
        mm, err = _open_or_none(uri)
        if mm is None:
            entry["err"] = err
            out[uri] = entry
            continue
        try:
            herr = _validate_header(mm, uri)
            if herr is not None:
                entry["err"] = herr
                out[uri] = entry
                continue
            entry["present"] = True
            entry["hdr"] = read_header(mm)
            # stable read may return None if seq==0 (never published)
            entry["sample"] = read_actuator_sample_stable(mm) or read_actuator_sample(mm)
        finally:
            mm.close()
        out[uri] = entry
    return out


# =============================================================================
#  verify subcommand — take two snapshots, compare, classify each channel.
# =============================================================================
#
# Direction is inverted vs sensor probes: firmware is the writer. We just
# watch and classify what firmware chooses to send. Four possible states per
# channel:
#
#   PASS  active + pulse_us in [pwm_min, pwm_max]     (real live PWM output)
#   PASS  active + pulse_us == 0                      (SERVOn_FUNCTION=0, disabled)
#   FAIL  active + pulse_us out of range and non-zero (bad value on the wire)
#   QUIET seq unchanged                               (firmware never wrote this channel)
#
# QUIET is informational — many channels are legitimately not written when
# firmware only issues `count=8 offset=0` frames (ch 8..15 stay untouched
# forever). QUIET is not a FAIL by default; use --require-channel N to
# demand activity on a specific channel.

def _classify(entry0, entry1, pwm_min, pwm_max):
    """Given two per-channel entries, return (state, details_str)."""
    if not entry1["present"]:
        return ("MISSING", entry1["err"] or "segment absent")
    hdr0 = entry0["hdr"]; hdr1 = entry1["hdr"]
    s1 = entry1["sample"]
    seq_delta = hdr1["seq"] - hdr0["seq"]
    active = seq_delta > 0
    pulse = s1["pulse_us"] if s1 else 0

    if not active:
        return ("QUIET", "seq=%d (unchanged) pulse_us=%d" % (hdr1["seq"], pulse))

    if pulse == 0:
        return ("DISABLED",
                "seq Δ=%d pulse_us=0 (channel not assigned)" % (seq_delta,))

    if pwm_min <= pulse <= pwm_max:
        return ("LIVE",
                "seq Δ=%d pulse_us=%d in [%d..%d]"
                % (seq_delta, pulse, pwm_min, pwm_max))

    return ("BAD",
            "seq Δ=%d pulse_us=%d OUT OF [%d..%d]"
            % (seq_delta, pulse, pwm_min, pwm_max))


def cmd_verify(args) -> int:
    if args.all:
        uris = _all_channel_uris(args.uri_base)
    else:
        uri = _resolve_uri(args)
        if uri is None:
            print("[probe_pwm] verify: pass --uri, --channel N, or --all",
                  file=sys.stderr)
            return 2
        uris = [uri]

    print("[probe_pwm] snapshot t0")
    snap0 = _snapshot_channels(uris)
    t0 = time.monotonic()
    print("[probe_pwm] waiting %.2fs..." % args.duration)
    time.sleep(args.duration)
    print("[probe_pwm] snapshot t1")
    snap1 = _snapshot_channels(uris)
    dt = time.monotonic() - t0

    # Classify each channel and count.
    counts = {"LIVE": 0, "DISABLED": 0, "QUIET": 0, "BAD": 0, "MISSING": 0}
    live_channels = []
    disabled_channels = []
    bad_channels = []
    for uri in uris:
        state, detail = _classify(snap0[uri], snap1[uri], args.pwm_min, args.pwm_max)
        counts[state] += 1
        tag = uri.rsplit("/", 1)[-1]
        print("  ch %3s  %-8s  %s" % (tag, state, detail))
        if state == "LIVE":
            live_channels.append(tag)
        elif state == "DISABLED":
            disabled_channels.append(tag)
        elif state == "BAD":
            bad_channels.append(tag)

    print()
    print("[probe_pwm] observation window: %.2fs" % dt)
    print("[probe_pwm] summary:  LIVE=%d  DISABLED=%d  QUIET=%d  BAD=%d  MISSING=%d"
          % (counts["LIVE"], counts["DISABLED"], counts["QUIET"],
             counts["BAD"], counts["MISSING"]))
    if live_channels:
        print("  LIVE     channels (real PWM): %s" % (",".join(live_channels),))
    if disabled_channels:
        print("  DISABLED channels (writing 0): %s" % (",".join(disabled_channels),))
    if bad_channels:
        print("  BAD      channels: %s" % (",".join(bad_channels),))

    # Overall PASS iff: at least one LIVE channel AND no BAD channels.
    # Plus: --require-channel N (if given) must be LIVE or DISABLED (i.e.
    # actively written by firmware).
    overall_ok = (counts["LIVE"] > 0) and (counts["BAD"] == 0)
    if args.require_channel is not None:
        req_uri = "%s/%d" % (args.uri_base.rstrip("/"), args.require_channel)
        if req_uri in snap1:
            req_state, _ = _classify(snap0[req_uri], snap1[req_uri],
                                     args.pwm_min, args.pwm_max)
            if req_state not in ("LIVE", "DISABLED"):
                print("  --require-channel %d state=%s (expected LIVE|DISABLED)"
                      % (args.require_channel, req_state))
                overall_ok = False
        else:
            print("  --require-channel %d not in probed set" % args.require_channel)
            overall_ok = False

    print()
    print("[probe_pwm] verdict: %s" % ("PASS" if overall_ok else "FAIL"))
    return 0 if overall_ok else 1


# =============================================================================
#  override subcommand — active E2E: MAVLink RC_CHANNELS_OVERRIDE -> shm delta
# =============================================================================
#
# This is the "did we actually influence firmware output" test. Steps:
#
#   1. baseline: read all 16 shm channels once
#   2. optional: switch mode to MANUAL (Rover custom mode 0) so RC input maps
#      to servo outputs
#   3. optional: clear FS_THR_ENABLE so radio-failsafe doesn't glue outputs
#      to trim
#   4. start a background thread sending RC_CHANNELS_OVERRIDE at --rate-hz.
#      Steering (RC ch 1) and throttle (RC ch 3) get non-trim values; the
#      rest are held at 1500 to avoid tripping range failsafes.
#   5. wait --duration seconds
#   6. final: read all 16 shm channels
#   7. release: send RC_CHANNELS_OVERRIDE with all channels == 0
#   8. report per-channel deltas; PASS if any channel's pulse_us shifted
#      by at least --min-delta µs from its baseline value.

def cmd_override(args) -> int:
    try:
        from common import MavlinkObserver
    except ImportError:
        print("[probe_pwm] override needs common.MavlinkObserver", file=sys.stderr)
        return 2

    uris = _all_channel_uris(args.uri_base)

    # 1. Baseline (before override starts).
    print("[probe_pwm] override: baseline snapshot")
    snap0 = _snapshot_channels(uris)

    # Connect to MAVLink. The observer already starts a heartbeat pump — that's
    # necessary so ArduPilot accepts commands from us.
    print("[probe_pwm] override: connecting MAVLink at %s" % args.url)
    obs = MavlinkObserver(args.url, timeout_s=args.mav_timeout).connect()
    mav = obs._mav

    # 2. Optional: switch to MANUAL. Rover custom modes: MANUAL=0, HOLD=4, etc.
    if args.set_mode_manual:
        target_sys = mav.target_system or 1
        print("[probe_pwm] override: setting mode=MANUAL (Rover custom_mode=0)")
        mav.mav.command_long_send(
            target_sys, 1,
            176,      # MAV_CMD_DO_SET_MODE
            0,
            1,        # base_mode: MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            0,        # custom_mode: MANUAL
            0, 0, 0, 0, 0,
        )
        time.sleep(0.5)

    # 3. Optional: clear throttle failsafe so overridden channels take effect.
    if args.clear_failsafe:
        print("[probe_pwm] override: setting FS_THR_ENABLE=0")
        mav.mav.param_set_send(
            mav.target_system or 1, 1,
            b"FS_THR_ENABLE",
            0.0,
            9,   # MAV_PARAM_TYPE_REAL32
        )
        time.sleep(0.5)

    # 4. Start override pump.
    import threading
    stop = threading.Event()
    def _pump():
        period = 1.0 / max(args.rate_hz, 1.0)
        # RC channel numbering: RC1=steering, RC3=throttle for typical Rover
        # RCMAP defaults. Others held at 1500 (neutral) so ArduPilot's radio
        # sanity checks don't complain.
        while not stop.is_set():
            try:
                mav.mav.rc_channels_override_send(
                    mav.target_system or 1, 1,
                    args.steering_us,   # RC1: steering
                    1500,               # RC2
                    args.throttle_us,   # RC3: throttle
                    1500, 1500, 1500, 1500, 1500,  # RC4..RC8
                )
            except Exception as e:
                sys.stderr.write("[probe_pwm] override send failed: %s\n" % e)
            stop.wait(period)

    thread = threading.Thread(target=_pump, daemon=True)
    print("[probe_pwm] override: pumping RC steering=%d throttle=%d at %.1f Hz"
          % (args.steering_us, args.throttle_us, args.rate_hz))
    thread.start()

    # Let the override propagate; ArduPilot may need a few cycles to accept.
    time.sleep(1.0)

    # 5. Wait for the observation window.
    print("[probe_pwm] override: observing for %.2fs..." % args.duration)
    time.sleep(args.duration)

    # 6. Final snapshot.
    print("[probe_pwm] override: final snapshot")
    snap1 = _snapshot_channels(uris)

    # 7. Release overrides.
    stop.set()
    thread.join(timeout=2.0)
    try:
        mav.mav.rc_channels_override_send(
            mav.target_system or 1, 1,
            0, 0, 0, 0, 0, 0, 0, 0,
        )
    except Exception:
        pass
    obs.close()

    # 8. Report deltas.
    print()
    print("[probe_pwm] per-channel deltas (baseline -> final):")
    shifted = []
    for uri in uris:
        tag = uri.rsplit("/", 1)[-1]
        e0 = snap0[uri]; e1 = snap1[uri]
        if not (e0["present"] and e1["present"]):
            print("  ch %3s  MISSING" % tag)
            continue
        p0 = e0["sample"]["pulse_us"] if e0["sample"] else 0
        p1 = e1["sample"]["pulse_us"] if e1["sample"] else 0
        d = p1 - p0
        seq_d = e1["hdr"]["seq"] - e0["hdr"]["seq"]
        mark = " *" if abs(d) >= args.min_delta else ""
        print("  ch %3s  pulse_us %5d -> %5d (Δ=%+d)  seqΔ=%d%s"
              % (tag, p0, p1, d, seq_d, mark))
        if abs(d) >= args.min_delta:
            shifted.append((tag, p0, p1, d))

    print()
    if shifted:
        print("[probe_pwm] verdict: PASS — %d channel(s) shifted by ≥%d µs:"
              % (len(shifted), args.min_delta))
        for tag, p0, p1, d in shifted:
            print("  ch %s: %d -> %d (%+d)" % (tag, p0, p1, d))
        return 0
    else:
        print("[probe_pwm] verdict: FAIL — no channel shifted by ≥%d µs."
              % args.min_delta)
        print("  Try: --set-mode-manual  (RC input drives outputs only in "
              "MANUAL/ACRO/STEERING)")
        print("       --clear-failsafe   (radio failsafe glues outputs to trim)")
        return 1


# =============================================================================
#  argparse glue
# =============================================================================

def _add_uri_selection(p):
    """Shared --uri / --channel / --all / --uri-base selection flags."""
    g = p.add_mutually_exclusive_group()
    g.add_argument("--uri",
                   help="full URI, e.g. %s/3" % DEFAULT_URI_BASE)
    g.add_argument("--channel", type=int,
                   help="channel index 0..%d (combines with --uri-base)"
                        % (NUM_PWM_CHANNELS - 1))
    g.add_argument("--all", action="store_true",
                   help="operate on all %d channels" % NUM_PWM_CHANNELS)
    p.add_argument("--uri-base", default=DEFAULT_URI_BASE,
                   help="URI base for --channel / --all (default: %s)"
                        % DEFAULT_URI_BASE)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_pwm.py",
        description=__doc__.splitlines()[1] if __doc__ else "",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="print current PWM for URI(s)")
    _add_uri_selection(p_show)
    p_show.set_defaults(func=cmd_show)

    p_ver = sub.add_parser("verify",
                           help="passive activity + range check on URI(s)")
    _add_uri_selection(p_ver)
    p_ver.add_argument("--duration", type=float, default=3.0,
                       help="observation window in seconds (default: 3.0)")
    p_ver.add_argument("--pwm-min", type=int, default=PLAUSIBLE_PWM_MIN,
                       help="min plausible pulse_us (default: %d)"
                            % PLAUSIBLE_PWM_MIN)
    p_ver.add_argument("--pwm-max", type=int, default=PLAUSIBLE_PWM_MAX,
                       help="max plausible pulse_us (default: %d)"
                            % PLAUSIBLE_PWM_MAX)
    p_ver.add_argument("--require-channel", type=int, default=None,
                       help="fail overall if this channel index is QUIET "
                            "(not written by firmware)")
    p_ver.set_defaults(func=cmd_verify)

    p_ovr = sub.add_parser(
        "override",
        help="active E2E: send RC_CHANNELS_OVERRIDE, watch shm shift")
    p_ovr.add_argument("--uri-base", default=DEFAULT_URI_BASE,
                       help="URI base (default: %s)" % DEFAULT_URI_BASE)
    p_ovr.add_argument("--url", default="udpin:127.0.0.1:14550",
                       help="MAVLink endpoint (default: udpin:127.0.0.1:14550)")
    p_ovr.add_argument("--mav-timeout", type=float, default=10.0,
                       help="heartbeat wait timeout in seconds")
    p_ovr.add_argument("--duration", type=float, default=3.0,
                       help="observation window while overriding (default: 3.0)")
    p_ovr.add_argument("--rate-hz", type=float, default=10.0,
                       help="RC override send rate (default: 10 Hz)")
    p_ovr.add_argument("--steering-us", type=int, default=1700,
                       help="RC ch1 override value (default: 1700)")
    p_ovr.add_argument("--throttle-us", type=int, default=1800,
                       help="RC ch3 override value (default: 1800)")
    p_ovr.add_argument("--min-delta", type=int, default=50,
                       help="min |Δpulse_us| to count as shifted (default: 50)")
    p_ovr.add_argument("--set-mode-manual", action="store_true",
                       help="send DO_SET_MODE(MANUAL) before overriding")
    p_ovr.add_argument("--clear-failsafe", action="store_true",
                       help="set FS_THR_ENABLE=0 before overriding")
    p_ovr.set_defaults(func=cmd_override)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
