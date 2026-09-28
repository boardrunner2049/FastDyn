"""
common.py — shared wire-format constants and helpers for Boardrunner VIO probes.

Mirrors the on-shm layout defined in
    include/boardrunner/canonical.h
Any change to the C header MUST be reflected here (bump BR_SHM_VERSION and
update the struct format strings).

These probes bypass Gazebo entirely and drive the shm segments directly.
They exist to prove the shm → channel → packer → SPI slave model → firmware
path works for a specific sensor family, independent of physics simulation
correctness.
"""
import mmap
import os
import struct
import time

# -----------------------------------------------------------------------------
# Wire constants (mirror include/boardrunner/canonical.h)
# -----------------------------------------------------------------------------

BR_SHM_MAGIC       = 0x42525653          # 'BRVS'
BR_SHM_VERSION     = 1
BR_SHM_PAGE_SIZE   = 4096
BR_URI_SCHEME      = "boardrunner://"

# Sample flags
BR_SAMPLE_HEALTHY     = 1 << 0
BR_SAMPLE_CALIBRATED  = 1 << 1
BR_SAMPLE_STALE       = 1 << 2

# BrShmHeader — 64 bytes
#   uint32 magic, version, payload_size, slot_count, flags, writer_pid  (24)
#   uint64 seq, last_write_ns                                           (16)
#   uint32 reserved[6]                                                  (24)
HEADER_FMT   = "<6I2Q6I"
HEADER_SIZE  = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 64, f"BrShmHeader size drift: {HEADER_SIZE}"

# Per-modality payload struct formats — MUST mirror canonical.h layouts.
IMU_SAMPLE_FMT      = "<2Q7fI"     # 48 B  seq+ts + 7 floats + flags
IMU_SAMPLE_SIZE     = struct.calcsize(IMU_SAMPLE_FMT)
#
#   BoardrunnerBaroSample  (32 B):
#     u64 seq, u64 ts_ns, f32 pressure_pa, f32 temp_c, u32 flags, u32 _pad
BARO_SAMPLE_FMT     = "<2Q2fII"    # 32 B
BARO_SAMPLE_SIZE    = struct.calcsize(BARO_SAMPLE_FMT)
assert BARO_SAMPLE_SIZE == 32, f"BoardrunnerBaroSample size drift: {BARO_SAMPLE_SIZE}"

#   BoardrunnerActuatorSample (32 B):
#     u64 seq, u64 ts_ns, u32 channel, u32 pulse_us, u32 flags, u32 _pad
# NOTE: direction is REVERSED versus sensors — firmware is the WRITER via the
# IOMCU endpoint, probe is the READER. So this schema is used read-only from
# the Python side; no inject_actuator_stream helper exists on purpose.
ACTUATOR_SAMPLE_FMT  = "<2Q4I"     # 32 B
ACTUATOR_SAMPLE_SIZE = struct.calcsize(ACTUATOR_SAMPLE_FMT)
assert ACTUATOR_SAMPLE_SIZE == 32, f"BoardrunnerActuatorSample size drift: {ACTUATOR_SAMPLE_SIZE}"

# Field offsets inside BrShmHeader
OFF_MAGIC          = 0
OFF_VERSION        = 4
OFF_PAYLOAD_SIZE   = 8
OFF_SLOT_COUNT     = 12
OFF_FLAGS          = 16
OFF_WRITER_PID     = 20
OFF_SEQ            = 24
OFF_LAST_WRITE_NS  = 32
OFF_PAYLOAD_START  = HEADER_SIZE   # payload slot starts right after header


# -----------------------------------------------------------------------------
# URI → shm path
# -----------------------------------------------------------------------------

_SHM_NAME_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def uri_to_shm_name(uri: str) -> str:
    """URI → shm object name. Mirrors br_uri_to_shm_name in transport/shm.c."""
    if not uri.startswith(BR_URI_SCHEME):
        raise ValueError(f"URI must start with {BR_URI_SCHEME!r}: {uri!r}")
    tail = uri[len(BR_URI_SCHEME):]
    if not tail:
        raise ValueError(f"URI has no body: {uri!r}")
    out = ["/br_"]
    for ch in tail:
        if ch == "/":
            out.append("_")
        elif ch in _SHM_NAME_ALLOWED:
            out.append(ch)
        else:
            raise ValueError(
                f"URI contains char {ch!r} not allowed in shm name: {uri!r}"
            )
    return "".join(out)


def uri_to_shm_path(uri: str) -> str:
    """URI → /dev/shm/... path (what POSIX shm exposes on Linux)."""
    return "/dev/shm" + uri_to_shm_name(uri)


# -----------------------------------------------------------------------------
# Segment open + header inspection
# -----------------------------------------------------------------------------

def open_segment(path: str, size: int = BR_SHM_PAGE_SIZE) -> mmap.mmap:
    """Open an existing shm segment and mmap it. Does NOT create.

    Fails clearly if the segment doesn't exist — that means the C-side
    slave never attached, which points at either a TOML data_source typo,
    a plumbing bug (Python parser / cJSON extractor / spi_helper.c),
    or FastDyn wasn't started.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"shm segment {path} does not exist.\n"
            f"  This means the C-side slave model never attached its channel.\n"
            f"  Check: is FastDyn running? Does the slave's TOML entry have\n"
            f"         a data_source line? Is the URI-to-shm-name mapping\n"
            f"         consistent between TOML and this probe?"
        )
    fd = os.open(path, os.O_RDWR)
    try:
        mm = mmap.mmap(fd, size)
    finally:
        os.close(fd)
    return mm


def read_header(mm: mmap.mmap) -> dict:
    """Return the shm header as a dict."""
    (magic, version, payload_size, slot_count, flags, writer_pid,
     seq, last_write_ns, _r0, _r1, _r2, _r3, _r4, _r5) = struct.unpack_from(HEADER_FMT, mm, 0)
    return dict(
        magic=magic, version=version, payload_size=payload_size,
        slot_count=slot_count, flags=flags, writer_pid=writer_pid,
        seq=seq, last_write_ns=last_write_ns,
    )


def sample_seq(mm: mmap.mmap) -> int:
    """Just the current seq value — fast path."""
    return struct.unpack_from("<Q", mm, OFF_SEQ)[0]


# -----------------------------------------------------------------------------
# SPSC seqlock writer — mirror of the writer half of transport/shm.c
# -----------------------------------------------------------------------------

def _seqlock_write(mm: mmap.mmap, payload_bytes: bytes, seq: int) -> int:
    """Perform one SPSC seqlock commit. Returns new even seq.

    Protocol (matches C reader in transport/shm.c):
        seq += 1                        # to odd; reader retries while odd
        write payload
        write last_write_ns
        seq += 1                        # to even; commit
    """
    if len(payload_bytes) + OFF_PAYLOAD_START > BR_SHM_PAGE_SIZE:
        raise ValueError(f"payload too large for one page: {len(payload_bytes)}")

    seq_odd = seq + 1
    struct.pack_into("<Q", mm, OFF_SEQ, seq_odd)

    ts_ns = time.monotonic_ns()
    mm[OFF_PAYLOAD_START : OFF_PAYLOAD_START + len(payload_bytes)] = payload_bytes
    struct.pack_into("<Q", mm, OFF_LAST_WRITE_NS, ts_ns)

    seq_even = seq_odd + 1
    struct.pack_into("<Q", mm, OFF_SEQ, seq_even)
    return seq_even


# -----------------------------------------------------------------------------
# IMU-specific helpers
# -----------------------------------------------------------------------------

def read_imu_sample(mm: mmap.mmap) -> dict:
    """Snapshot the IMU payload. NOT seqlock-safe — for diagnostics only."""
    (seq, ts_ns, ax, ay, az, gx, gy, gz, temp_c, flags) = struct.unpack_from(
        IMU_SAMPLE_FMT, mm, OFF_PAYLOAD_START
    )
    return dict(
        seq=seq, ts_ns=ts_ns,
        ax_mps2=ax, ay_mps2=ay, az_mps2=az,
        gx_radps=gx, gy_radps=gy, gz_radps=gz,
        temp_c=temp_c, flags=flags,
    )


def inject_imu_stream(mm: mmap.mmap,
                      ax_mps2: float, ay_mps2: float, az_mps2: float,
                      gx_radps: float, gy_radps: float, gz_radps: float,
                      temp_c: float,
                      duration_s: float,
                      rate_hz: float = 1000.0,
                      flags: int = BR_SAMPLE_HEALTHY,
                      stop_event=None) -> int:
    """Write a constant IMU sample repeatedly for duration_s at rate_hz.

    Returns the count of samples written. Uses the SPSC seqlock protocol
    that the C reader in icm20602_spi.c / mpu9250_spi.c / icm20948_spi.c
    (via BR_IMU_READ) expects.

    Payload's embedded 'seq' matches the header seq after each commit —
    lets readers detect missed samples if they care.

    Pass a threading.Event as stop_event to allow early termination —
    useful when running injection in a background thread while a foreground
    thread collects response data.
    """
    seq = sample_seq(mm)
    period_s = 1.0 / rate_hz if rate_hz > 0 else 0.0
    end = time.monotonic() + duration_s
    n = 0

    while time.monotonic() < end:
        if stop_event is not None and stop_event.is_set():
            break

        ts_ns = time.monotonic_ns()
        seq_odd = seq + 1
        seq_even = seq_odd + 1

        payload = struct.pack(
            IMU_SAMPLE_FMT,
            seq_even, ts_ns,
            ax_mps2, ay_mps2, az_mps2,
            gx_radps, gy_radps, gz_radps,
            temp_c,
            flags,
        )
        seq = _seqlock_write(mm, payload, seq)
        n += 1
        if period_s > 0:
            time.sleep(period_s)

    return n


# -----------------------------------------------------------------------------
# State save / restore — used by verify subcommand to leave shm untouched
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Baro helpers
# -----------------------------------------------------------------------------

def read_baro_sample(mm: mmap.mmap) -> dict:
    """Snapshot the baro payload. NOT seqlock-safe — for diagnostics only."""
    (seq, ts_ns, pressure_pa, temp_c, flags, _pad) = struct.unpack_from(
        BARO_SAMPLE_FMT, mm, OFF_PAYLOAD_START
    )
    return dict(
        seq=seq, ts_ns=ts_ns,
        pressure_pa=pressure_pa, temp_c=temp_c,
        flags=flags,
    )


def inject_baro_stream(mm: mmap.mmap,
                       pressure_pa: float,
                       temp_c: float,
                       duration_s: float,
                       rate_hz: float = 100.0,
                       flags: int = BR_SAMPLE_HEALTHY,
                       stop_event=None) -> int:
    """Write a constant baro sample repeatedly for duration_s at rate_hz.

    100 Hz default — baro on a real MS5611 runs at ~50 Hz max.
    Same SPSC seqlock protocol as inject_imu_stream.
    """
    seq = sample_seq(mm)
    period_s = 1.0 / rate_hz if rate_hz > 0 else 0.0
    end = time.monotonic() + duration_s
    n = 0

    while time.monotonic() < end:
        if stop_event is not None and stop_event.is_set():
            break
        ts_ns = time.monotonic_ns()
        seq_odd = seq + 1
        seq_even = seq_odd + 1

        payload = struct.pack(
            BARO_SAMPLE_FMT,
            seq_even, ts_ns,
            pressure_pa, temp_c,
            flags, 0,          # _pad
        )
        seq = _seqlock_write(mm, payload, seq)
        n += 1
        if period_s > 0:
            time.sleep(period_s)

    return n


def save_baro_slot(mm: mmap.mmap) -> dict:
    """Snapshot the baro payload for later restore."""
    return read_baro_sample(mm)


def restore_baro_slot(mm: mmap.mmap, saved: dict, brief_s: float = 0.5):
    """Write the saved baro sample back to shm for ~brief_s."""
    inject_baro_stream(mm, saved['pressure_pa'], saved['temp_c'],
                       brief_s, 100.0)


# -----------------------------------------------------------------------------
# IMU state save / restore
# -----------------------------------------------------------------------------

def save_imu_slot(mm: mmap.mmap) -> dict:
    """Snapshot the current payload for later restore. Non-invasive."""
    return read_imu_sample(mm)


def restore_imu_slot(mm: mmap.mmap, saved: dict, brief_s: float = 0.5):
    """Write the saved sample back to shm for ~brief_s at 1 kHz.

    A brief burst ensures the reader latches the restored value even if it
    was in the middle of an SPI transfer. Called at the end of verify.
    """
    inject_imu_stream(
        mm,
        saved['ax_mps2'], saved['ay_mps2'], saved['az_mps2'],
        saved['gx_radps'], saved['gy_radps'], saved['gz_radps'],
        saved['temp_c'],
        brief_s,
        1000.0,
    )


# -----------------------------------------------------------------------------
# MAVLink observer — reads ATTITUDE from a running FastDyn session
# -----------------------------------------------------------------------------

DEFAULT_MAVLINK_URL = "udpin:127.0.0.1:14550"


class MavlinkObserver:
    """Lightweight ATTITUDE collector over MAVLink.

    Default connection target is a UDP socket on 127.0.0.1:14550, which
    matches this MAVProxy invocation:

        mavproxy.py --master=/tmp/usart2_host --baudrate 57600 \\
                    --out=udp:127.0.0.1:14550

    That way both MAVProxy (for human observation) and this probe (for
    automated verification) can consume the same telemetry stream.

    IMPORTANT: ArduPilot only streams high-rate telemetry (ATTITUDE,
    RC_CHANNELS, etc.) to endpoints that identify themselves as a GCS by
    sending periodic heartbeats. Bare `wait_heartbeat` gives us the AP's
    HEARTBEAT stream but not much else. So we run a background heartbeat
    sender at 1 Hz identifying ourselves as MAV_TYPE_GCS — after that
    ArduPilot streams full telemetry to the port we're listening on.
    """

    _HEARTBEAT_PERIOD_S = 1.0
    # MAVLink constants (avoid a hard dep on mavutil.mavlink at class-definition time).
    _MAV_TYPE_GCS         = 6
    _MAV_AUTOPILOT_INVALID = 8

    def __init__(self, url: str = DEFAULT_MAVLINK_URL, timeout_s: float = 10.0):
        try:
            from pymavlink import mavutil
        except ImportError:
            raise RuntimeError(
                "pymavlink is not installed in this Python.\n"
                "  If FastDyn's venv exists at ./fastdyn-env, run with:\n"
                "    ./fastdyn-env/bin/python3 tests/integration/vio_probes/probe_imu.py ...\n"
                "  Otherwise: pip install --user --break-system-packages pymavlink"
            )
        self._mavutil = mavutil
        self._url = url
        self._timeout_s = timeout_s
        self._mav = None
        self._hb_stop = None
        self._hb_thread = None

    def connect(self):
        """Open the MAVLink connection, start heartbeat pump, wait for AP heartbeat.

        After heartbeat, request 20 Hz ATTITUDE streaming so `verify` can
        gather a few samples in a 2-3 s window. Without this, ArduPilot
        forwards ATTITUDE to us only at the default background rate (~0.2 Hz
        for TELEM1 through the current link stack), which is too slow.
        """
        import threading

        # pymavlink URL conventions:
        #   udpin:HOST:PORT     — listen on port
        #   udpout:HOST:PORT    — send to host:port
        #   /path/to/pty        — open as serial (needs --baud arg)
        if self._url.startswith("/"):
            self._mav = self._mavutil.mavlink_connection(
                self._url, baud=57600, source_system=255)
        else:
            self._mav = self._mavutil.mavlink_connection(
                self._url, source_system=255)

        # Start heartbeat sender BEFORE waiting for heartbeat — MAVProxy needs
        # to know we're here before it forwards telemetry back.
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

        hb = self._mav.wait_heartbeat(timeout=self._timeout_s)
        if hb is None:
            self._hb_stop.set()
            raise RuntimeError(
                f"no MAVLink heartbeat from {self._url!r} within {self._timeout_s}s.\n"
                f"  Is FastDyn running and MAVProxy forwarding to that endpoint?\n"
                f"  Suggested MAVProxy invocation:\n"
                f"    mavproxy.py --master=/tmp/usart2_host --baudrate 57600 \\\n"
                f"                --out=udp:127.0.0.1:14550"
            )

        # Ask ArduPilot to stream several messages at 20 Hz. Interval in µs.
        # Message IDs (from MAVLink common):
        #   ATTITUDE          = 30
        #   SCALED_IMU        = 26
        #   RAW_IMU           = 27   (ArduPilot uses this for IMU instance 0)
        #   SCALED_IMU2       = 116  (IMU instance 1)
        #   SCALED_IMU3       = 129  (IMU instance 2)
        #   SCALED_PRESSURE   = 29   (baro instance 0)
        #   SCALED_PRESSURE2  = 137  (baro instance 1)
        #   SCALED_PRESSURE3  = 143  (baro instance 2)
        # MAV_CMD_SET_MESSAGE_INTERVAL = 511.
        target_sys = hb.get_srcSystem() if hasattr(hb, 'get_srcSystem') else 1
        for msg_id in (30, 26, 27, 116, 129, 29, 137, 143):
            try:
                self._mav.mav.command_long_send(
                    target_sys, 1,   # target_system, target_component
                    511,             # MAV_CMD_SET_MESSAGE_INTERVAL
                    0,               # confirmation
                    msg_id,          # param1: message ID
                    50000,           # param2: interval in µs (20 Hz)
                    0, 0, 0, 0, 0,
                )
            except Exception:
                pass  # not fatal

        return self

    def _heartbeat_loop(self):
        """Send a heartbeat every second so ArduPilot streams telemetry to us."""
        while self._hb_stop is not None and not self._hb_stop.is_set():
            try:
                self._mav.mav.heartbeat_send(
                    self._MAV_TYPE_GCS,
                    self._MAV_AUTOPILOT_INVALID,
                    0, 0, 0,
                )
            except Exception:
                pass
            time.sleep(self._HEARTBEAT_PERIOD_S)

    def collect_attitude(self, duration_s: float,
                         min_count: int = 0,
                         hard_timeout_s: float = None) -> list:
        """Collect ATTITUDE messages, either time-limited or count-driven.

        Args:
            duration_s: nominal collection window in seconds. If min_count is
                0 (default) this is a hard time limit.
            min_count: if > 0, keep collecting until at least this many
                messages are gathered OR hard_timeout_s expires. Useful when
                the underlying link is slow and you'd rather wait than fail.
            hard_timeout_s: absolute deadline when min_count > 0. Defaults
                to 4× duration_s if unset.
        """
        if min_count > 0 and hard_timeout_s is None:
            hard_timeout_s = duration_s * 4.0
        deadline = time.monotonic() + (hard_timeout_s if min_count > 0 else duration_s)
        min_deadline = time.monotonic() + duration_s  # earliest we'll return
        msgs = []
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            # Stop early if we have enough samples AND spent minimum window
            if min_count > 0 and len(msgs) >= min_count and now >= min_deadline:
                break
            remaining = deadline - now
            m = self._mav.recv_match(
                type='ATTITUDE', blocking=True,
                timeout=min(0.3, max(0.01, remaining)),
            )
            if m is not None:
                msgs.append(m)
        return msgs

    def close(self):
        if self._hb_stop is not None:
            self._hb_stop.set()
            if self._hb_thread is not None:
                self._hb_thread.join(timeout=2.0)
            self._hb_stop = None
            self._hb_thread = None
        if self._mav is not None:
            try:
                self._mav.close()
            except Exception:
                pass
            self._mav = None


def attitude_mean(msgs) -> dict:
    """Mean roll/pitch/yaw (and rates) over a list of ATTITUDE messages."""
    if not msgs:
        return None
    n = len(msgs)
    return dict(
        n=n,
        roll=sum(m.roll for m in msgs) / n,
        pitch=sum(m.pitch for m in msgs) / n,
        yaw=sum(m.yaw for m in msgs) / n,
        rollspeed=sum(m.rollspeed for m in msgs) / n,
        pitchspeed=sum(m.pitchspeed for m in msgs) / n,
        yawspeed=sum(m.yawspeed for m in msgs) / n,
    )


# -----------------------------------------------------------------------------
# SCALED_IMU / SCALED_IMU2 / SCALED_IMU3 — raw per-instance sensor readings
# -----------------------------------------------------------------------------
#
# These messages report UNFILTERED accel/gyro directly from AP_InertialSensor
# for each IMU instance (in mG for accel, mrad/s for gyro). Unlike ATTITUDE,
# they aren't run through DCM's slow filter — a change in shm shows up almost
# immediately as a change in the raw values. Perfect for byte-level verification.
#
# MAVLink message-name → IMU instance mapping.
#
# ArduPilot quirk: for the PRIMARY IMU (instance 0), ArduPilot publishes
# RAW_IMU (id 27) instead of SCALED_IMU (id 26). The wire units are still
# mG for accel and mrad/s for gyro (ArduPilot deviates from the raw-LSB
# MAVLink v1 spec). So we track BOTH message types and map:
#   RAW_IMU      → instance 0 (primary)
#   SCALED_IMU   → instance 0 (secondary path, if published)
#   SCALED_IMU2  → instance 1
#   SCALED_IMU3  → instance 2
#
# Both message families expose the same field names: xacc/yacc/zacc (mG)
# and xgyro/ygyro/zgyro (mrad/s).

SCALED_IMU_TYPES = ('RAW_IMU', 'SCALED_IMU', 'SCALED_IMU2', 'SCALED_IMU3')

_TYPE_TO_INSTANCE = {
    'RAW_IMU':     0,
    'SCALED_IMU':  0,
    'SCALED_IMU2': 1,
    'SCALED_IMU3': 2,
}


def _scaled_imu_to_dict(msg) -> dict:
    """Extract accel/gyro fields from RAW_IMU or SCALED_IMU-family message."""
    return dict(
        instance=_TYPE_TO_INSTANCE[msg.get_type()],
        msg_type=msg.get_type(),
        xacc=msg.xacc,   # mG
        yacc=msg.yacc,
        zacc=msg.zacc,
        xgyro=msg.xgyro, # mrad/s
        ygyro=msg.ygyro,
        zgyro=msg.zgyro,
    )


def collect_scaled_imu_snapshot(obs, timeout_s: float = 30.0) -> dict:
    """Wait until we have one SCALED_IMU_N reading for each instance we can find.

    Returns dict {instance: {xacc, yacc, zacc, xgyro, ygyro, zgyro, ...}}
    Only includes instances that actually publish. Doesn't wait past timeout.
    """
    snap = {}
    end = time.monotonic() + timeout_s
    while len(snap) < 3 and time.monotonic() < end:
        m = obs._mav.recv_match(
            type=list(SCALED_IMU_TYPES), blocking=True,
            timeout=min(1.0, end - time.monotonic()))
        if m is None:
            continue
        d = _scaled_imu_to_dict(m)
        snap[d['instance']] = d
    return snap


def drain_scaled_imu(obs, duration_s: float) -> int:
    """Drain SCALED_IMU-family messages for duration_s, discard. Returns count."""
    n = 0
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        m = obs._mav.recv_match(
            type=list(SCALED_IMU_TYPES), blocking=True,
            timeout=min(0.3, max(0.01, end - time.monotonic())))
        if m is not None:
            n += 1
    return n


# -----------------------------------------------------------------------------
# SCALED_PRESSURE / SCALED_PRESSURE2 / SCALED_PRESSURE3 — per-baro-instance
# -----------------------------------------------------------------------------
#
# ArduPilot publishes:
#   SCALED_PRESSURE   → baro instance 0 (primary)
#   SCALED_PRESSURE2  → baro instance 1
#   SCALED_PRESSURE3  → baro instance 2
#
# Fields: press_abs (hPa, i.e. 100 Pa), press_diff (hPa), temperature (0.01 °C)

SCALED_PRESSURE_TYPES = ('SCALED_PRESSURE', 'SCALED_PRESSURE2', 'SCALED_PRESSURE3')

_PRESSURE_TYPE_TO_INSTANCE = {
    'SCALED_PRESSURE':  0,
    'SCALED_PRESSURE2': 1,
    'SCALED_PRESSURE3': 2,
}


def _scaled_pressure_to_dict(msg) -> dict:
    return dict(
        instance=_PRESSURE_TYPE_TO_INSTANCE[msg.get_type()],
        msg_type=msg.get_type(),
        press_abs_hPa=msg.press_abs,           # hPa (mbar)
        press_diff_hPa=msg.press_diff,         # hPa
        temperature_cC=msg.temperature,        # 0.01 °C
    )


# -----------------------------------------------------------------------------
# Actuator PWM helpers — READ ONLY. Firmware writes via the IOMCU endpoint.
# -----------------------------------------------------------------------------

def read_actuator_sample(mm: mmap.mmap) -> dict:
    """Snapshot the actuator payload. NOT seqlock-safe — diagnostics only.

    For a torn-read-safe read, use `read_actuator_sample_stable`.
    """
    (seq, ts_ns, channel, pulse_us, flags, _pad) = struct.unpack_from(
        ACTUATOR_SAMPLE_FMT, mm, OFF_PAYLOAD_START
    )
    return dict(
        seq=seq, ts_ns=ts_ns, channel=channel, pulse_us=pulse_us, flags=flags,
    )


def read_actuator_sample_stable(mm: mmap.mmap, retries: int = 32) -> dict:
    """Seqlock reader: retries on odd or torn seq, returns the header seq too.

    Uses the SPSC protocol from transport/shm.c — matches the C reader half.
    Returns None if a stable read isn't achievable within `retries` (usually
    means no writer has ever published; the header seq stays at 0).
    """
    for _ in range(retries):
        s0 = struct.unpack_from("<Q", mm, OFF_SEQ)[0]
        if s0 == 0:
            return None  # no writer has published yet
        if s0 & 1:
            continue     # writer mid-write
        sample = read_actuator_sample(mm)
        s1 = struct.unpack_from("<Q", mm, OFF_SEQ)[0]
        if s0 == s1:
            sample['header_seq'] = s0
            return sample
    return None


def collect_scaled_pressure_snapshot(obs, timeout_s: float = 30.0) -> dict:
    """Wait for at least one SCALED_PRESSURE_N reading per available instance."""
    snap = {}
    end = time.monotonic() + timeout_s
    while len(snap) < 3 and time.monotonic() < end:
        m = obs._mav.recv_match(
            type=list(SCALED_PRESSURE_TYPES), blocking=True,
            timeout=min(1.0, end - time.monotonic()))
        if m is None:
            continue
        d = _scaled_pressure_to_dict(m)
        snap[d['instance']] = d
    return snap


def drain_scaled_pressure(obs, duration_s: float) -> int:
    """Drain SCALED_PRESSURE-family messages for duration_s, discard."""
    n = 0
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        m = obs._mav.recv_match(
            type=list(SCALED_PRESSURE_TYPES), blocking=True,
            timeout=min(0.3, max(0.01, end - time.monotonic())))
        if m is not None:
            n += 1
    return n
