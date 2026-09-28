#!/usr/bin/env python3
"""
imu_gazebo_endpoint.py — Boardrunner VIO endpoint for a Gazebo IMU sensor.

Subscribes to a Gazebo IMU topic (default: r1_rover base_link IMU on the
"runway" world), converts the message to the canonical BoardrunnerImuSample
SI struct, applies an optional chip-mount rotation, and either publishes to
a POSIX shm segment (real mode) or prints to stdout (--dry-run mode).

Phase 2 use — dry-run for Gazebo-integration validation:

    python3 imu_gazebo_endpoint.py --dry-run --rate-hz 5

Phase 3 use — real shm publisher, feeding the ICM20602 model in QEMU:

    python3 imu_gazebo_endpoint.py \\
        --uri boardrunner://vehicle/0/sensor/imu/0 \\
        --mount-rotation roll_180

Wire format matches boardrunner/boardrunner_sdk/include/boardrunner/{canonical,
channel}.h exactly. Any struct/header layout change on the C side must be
mirrored here — see BR_SHM_MAGIC / BR_SHM_VERSION guards below.
"""

import argparse
import mmap
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

# -----------------------------------------------------------------------------
# Wire constants — MUST match include/boardrunner/canonical.h
# -----------------------------------------------------------------------------

BR_SHM_MAGIC = 0x42525653          # 'BRVS'
BR_SHM_VERSION = 1
BR_SHM_PAGE_SIZE = 4096
BR_URI_SCHEME = "boardrunner://"

BR_SAMPLE_HEALTHY = 1 << 0
BR_SAMPLE_CALIBRATED = 1 << 1
# BR_SAMPLE_STALE (1 << 2) is set by transport, not the writer.

# BrShmHeader — 64 bytes exactly.
#   uint32 magic, version, payload_size, slot_count, flags, writer_pid  (24)
#   uint64 seq, last_write_ns                                           (16)
#   uint32 reserved[6]                                                  (24)
_HEADER_FMT = "<6I2Q6I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
assert _HEADER_SIZE == 64, f"BrShmHeader size drift: {_HEADER_SIZE}"

# BoardrunnerImuSample — 48 bytes.
#   uint64 seq, timestamp_ns
#   float ax_mps2, ay_mps2, az_mps2, gx_radps, gy_radps, gz_radps, temp_c
#   uint32 flags
_IMU_SAMPLE_FMT = "<2Q7fI"
_IMU_SAMPLE_SIZE = struct.calcsize(_IMU_SAMPLE_FMT)
assert _IMU_SAMPLE_SIZE == 48, f"BoardrunnerImuSample size drift: {_IMU_SAMPLE_SIZE}"

# Field offsets inside BrShmHeader.
_OFF_MAGIC = 0
_OFF_VERSION = 4
_OFF_PAYLOAD_SIZE = 8
_OFF_SLOT_COUNT = 12
_OFF_FLAGS = 16
_OFF_WRITER_PID = 20
_OFF_SEQ = 24
_OFF_LAST_WRITE_NS = 32
_OFF_RESERVED = 40
_OFF_PAYLOAD_START = _HEADER_SIZE


# -----------------------------------------------------------------------------
# URI → shm object name (mirrors br_uri_to_shm_name in transport/shm.c)
# -----------------------------------------------------------------------------

_SHM_NAME_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def uri_to_shm_name(uri: str) -> str:
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


# -----------------------------------------------------------------------------
# Chip-mount rotations
# -----------------------------------------------------------------------------

# The Gazebo IMU sensor publishes values in the sensor frame (== vehicle frame
# for r1_rover, since the sensor pose is identity on base_link). The physical
# ICM20602 on the CubeBlack is mounted with its Z axis pointing DOWN in vehicle
# frame — that is why the pre-VIO static defaults use az_lsb = -2048 (i.e.
# -1g) at rest. So the endpoint must rotate Gazebo's Z-up sensor frame into
# the chip's Z-down mount before publishing.
#
# For a first cut we only need identity + a single "roll 180" preset. If we
# ever add a second IMU with a different mount, add another preset here.

@dataclass
class MountRotation:
    """3x3 rotation matrix expressed as three column vectors, applied per-axis."""

    name: str
    ax: tuple  # (m00, m01, m02): row 0
    ay: tuple  # (m10, m11, m12): row 1
    az: tuple  # (m20, m21, m22): row 2

    def rotate(self, x: float, y: float, z: float) -> tuple:
        return (
            self.ax[0] * x + self.ax[1] * y + self.ax[2] * z,
            self.ay[0] * x + self.ay[1] * y + self.ay[2] * z,
            self.az[0] * x + self.az[1] * y + self.az[2] * z,
        )


MOUNT_ROTATIONS = {
    "identity": MountRotation(
        name="identity",
        ax=(1.0, 0.0, 0.0),
        ay=(0.0, 1.0, 0.0),
        az=(0.0, 0.0, 1.0),
    ),
    "roll_180": MountRotation(
        # Rotate 180° about vehicle X axis: X→X, Y→-Y, Z→-Z.
        # Matches the ICM20602 default (az_lsb = -2048 at rest, chip Z-down).
        name="roll_180",
        ax=(1.0, 0.0, 0.0),
        ay=(0.0, -1.0, 0.0),
        az=(0.0, 0.0, -1.0),
    ),
    "pitch_180": MountRotation(
        # Rotate 180° about vehicle Y axis: X→-X, Y→Y, Z→-Z.
        name="pitch_180",
        ax=(-1.0, 0.0, 0.0),
        ay=(0.0, 1.0, 0.0),
        az=(0.0, 0.0, -1.0),
    ),
    "yaw_180": MountRotation(
        # Rotate 180° about vehicle Z axis: X→-X, Y→-Y, Z→Z.
        name="yaw_180",
        ax=(-1.0, 0.0, 0.0),
        ay=(0.0, -1.0, 0.0),
        az=(0.0, 0.0, 1.0),
    ),
}


# -----------------------------------------------------------------------------
# Shm channel writer — matches the SPSC seqlock protocol in transport/shm.c
# -----------------------------------------------------------------------------

class ShmImuWriter:
    """POSIX shm writer for BoardrunnerImuSample.

    Uses ``posix_ipc`` for shm_open+ftruncate (matches C ABI exactly).
    Cross-process synchronisation is via the SPSC seqlock in BrShmHeader —
    writer bumps seq to odd, memcpy payload, bumps seq back to even. Readers
    (the ICM20602 model in QEMU) retry until they see two matching even seq
    values around a payload copy.

    On x86_64 our 64-bit stores to a properly aligned mmap slot are naturally
    atomic and TSO-ordered, so the release semantics the C reader assumes
    hold without explicit barriers.
    """

    def __init__(self, uri: str):
        try:
            import posix_ipc  # type: ignore
        except ImportError:
            print(
                "boardrunner: posix_ipc not installed. `pip install posix-ipc`.",
                file=sys.stderr,
            )
            raise

        self._posix_ipc = posix_ipc
        self._shm_name = uri_to_shm_name(uri)
        self._seg_size = BR_SHM_PAGE_SIZE
        self._seq = 0  # writer-side monotonic; drives the seqlock

        # O_CREAT so we tolerate racing the reader; mode 0600 matches C side.
        self._shm = posix_ipc.SharedMemory(
            self._shm_name,
            flags=posix_ipc.O_CREAT,
            mode=0o600,
            size=self._seg_size,
        )
        self._mm = mmap.mmap(self._shm.fd, self._seg_size)
        # fd may be closed after mmap; the mapping keeps the segment alive.
        os.close(self._shm.fd)

        magic = struct.unpack_from("<I", self._mm, _OFF_MAGIC)[0]
        if magic == BR_SHM_MAGIC:
            version = struct.unpack_from("<I", self._mm, _OFF_VERSION)[0]
            payload_size = struct.unpack_from(
                "<I", self._mm, _OFF_PAYLOAD_SIZE
            )[0]
            if version != BR_SHM_VERSION:
                raise RuntimeError(
                    f"shm {self._shm_name}: version {version} != "
                    f"expected {BR_SHM_VERSION}"
                )
            if payload_size != _IMU_SAMPLE_SIZE:
                raise RuntimeError(
                    f"shm {self._shm_name}: payload_size {payload_size} != "
                    f"expected {_IMU_SAMPLE_SIZE}"
                )
            # Seed our seq from whatever's on-disk so we keep monotonicity if
            # a previous writer crashed. Round up to next even to enter a
            # clean state.
            on_disk = struct.unpack_from("<Q", self._mm, _OFF_SEQ)[0]
            self._seq = (on_disk + 2) & ~1
        else:
            self._init_header()

    def _init_header(self):
        struct.pack_into(
            _HEADER_FMT,
            self._mm,
            0,
            BR_SHM_MAGIC,
            BR_SHM_VERSION,
            _IMU_SAMPLE_SIZE,
            1,           # slot_count
            0,           # flags
            os.getpid(), # writer_pid
            0,           # seq (0 == sentinel: no writer has committed yet)
            0,           # last_write_ns
            0, 0, 0, 0, 0, 0,  # reserved[6]
        )
        self._seq = 0

    def write(
        self,
        ax_mps2: float,
        ay_mps2: float,
        az_mps2: float,
        gx_radps: float,
        gy_radps: float,
        gz_radps: float,
        temp_c: float,
        flags: int = BR_SAMPLE_HEALTHY,
    ):
        # 1) Enter update: bump seq to next odd. Reader spins while seq is odd.
        next_odd = self._seq | 1 if self._seq & 1 == 0 else self._seq + 2 | 1
        # Simpler: always compute from a clean even predecessor.
        if self._seq & 1:
            # Should not happen — writer is single-threaded and always leaves
            # seq even after every write(). Guard against a torn prior write.
            self._seq += 1
        next_odd = self._seq + 1

        struct.pack_into("<Q", self._mm, _OFF_SEQ, next_odd)

        # 2) Write payload + header timestamp fields.
        now_ns = time.monotonic_ns()
        struct.pack_into(
            _IMU_SAMPLE_FMT,
            self._mm,
            _OFF_PAYLOAD_START,
            next_odd,          # embedded payload seq (writer-side monotonic)
            now_ns,
            ax_mps2, ay_mps2, az_mps2,
            gx_radps, gy_radps, gz_radps,
            temp_c,
            flags,
        )
        struct.pack_into("<Q", self._mm, _OFF_LAST_WRITE_NS, now_ns)

        # 3) Commit: bump seq to next even. This is the release point that
        # the reader observes for a "clean" payload.
        self._seq = next_odd + 1
        struct.pack_into("<Q", self._mm, _OFF_SEQ, self._seq)

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        # NEVER shm_unlink here — the segment must persist across writer
        # restarts, and the C-side reader may still be attached.


# -----------------------------------------------------------------------------
# Gazebo IMU subscriber
# -----------------------------------------------------------------------------

DEFAULT_TOPIC = (
    "/world/runway/model/r1_rover/link/base_link/sensor/imu_sensor/imu"
)


class ImuEndpoint:
    def __init__(
        self,
        topic: str,
        uri: str,
        mount: MountRotation,
        rate_hz: float,
        temp_c: float,
        dry_run: bool,
    ):
        self.topic = topic
        self.uri = uri
        self.mount = mount
        self.min_interval_s = 1.0 / rate_hz if rate_hz > 0 else 0.0
        self.temp_c = temp_c
        self.dry_run = dry_run
        self.last_emit_time = 0.0
        self.msg_count = 0
        self.writer: Optional[ShmImuWriter] = None

        if not dry_run:
            self.writer = ShmImuWriter(uri)
            print(
                f"boardrunner: writing IMU samples to {uri} "
                f"(shm={self.writer._shm_name})",
                flush=True,
            )
        else:
            print(
                f"boardrunner: DRY-RUN — printing SI samples from {topic!r}; "
                f"target URI would be {uri!r}, mount={mount.name}",
                flush=True,
            )

    def on_imu(self, msg):
        """Callback for gz.msgs.IMU.

        gz.msgs10.imu_pb2.IMU has:
            header.stamp.sec / .nsec
            entity_name
            orientation.{x,y,z,w}
            angular_velocity.{x,y,z}       — rad/s in sensor frame
            linear_acceleration.{x,y,z}    — m/s² in sensor frame (specific force)
        No temperature — we synthesize a plausible constant.
        """
        self.msg_count += 1

        # Throttle to --rate-hz. Gazebo IMU publishes at ~1 kHz; dry-run
        # printing at that rate floods the terminal.
        now = time.monotonic()
        if self.min_interval_s > 0.0 and (now - self.last_emit_time) < self.min_interval_s:
            return
        self.last_emit_time = now

        la = msg.linear_acceleration
        av = msg.angular_velocity

        # Rotate sensor frame → chip mount frame.
        ax_c, ay_c, az_c = self.mount.rotate(la.x, la.y, la.z)
        gx_c, gy_c, gz_c = self.mount.rotate(av.x, av.y, av.z)

        if self.dry_run:
            ts = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
            print(
                f"[{ts:10.3f}] "
                f"raw accel=({la.x:+7.3f},{la.y:+7.3f},{la.z:+7.3f}) "
                f"gyro=({av.x:+7.3f},{av.y:+7.3f},{av.z:+7.3f}) | "
                f"chip accel=({ax_c:+7.3f},{ay_c:+7.3f},{az_c:+7.3f}) "
                f"gyro=({gx_c:+7.3f},{gy_c:+7.3f},{gz_c:+7.3f}) | "
                f"n={self.msg_count}",
                flush=True,
            )
        else:
            self.writer.write(
                ax_c, ay_c, az_c,
                gx_c, gy_c, gz_c,
                self.temp_c,
            )

    def run(self):
        try:
            from gz.transport13 import Node
            from gz.msgs10.imu_pb2 import IMU
        except ImportError as exc:
            print(
                f"boardrunner: gz.transport13/gz.msgs10 import failed: {exc}\n"
                "  Check Gazebo Harmonic Python bindings are on PYTHONPATH.",
                file=sys.stderr,
            )
            sys.exit(2)

        node = Node()
        if not node.subscribe(IMU, self.topic, self.on_imu):
            print(
                f"boardrunner: failed to subscribe to {self.topic!r}",
                file=sys.stderr,
            )
            sys.exit(3)

        print(f"boardrunner: subscribed to {self.topic}", flush=True)

        # Sleep loop; the subscribe callback fires on Gazebo's transport thread.
        stop = [False]

        def _handle_sigterm(_signum, _frame):
            stop[0] = True

        signal.signal(signal.SIGINT, _handle_sigterm)
        signal.signal(signal.SIGTERM, _handle_sigterm)

        try:
            while not stop[0]:
                time.sleep(0.5)
        finally:
            if self.writer is not None:
                self.writer.close()
            print(
                f"boardrunner: stopped after {self.msg_count} IMU messages",
                flush=True,
            )


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Publish Gazebo IMU samples into a Boardrunner VIO shm segment."
    )
    p.add_argument(
        "--topic",
        default=DEFAULT_TOPIC,
        help=f"Gazebo IMU topic (default: {DEFAULT_TOPIC})",
    )
    p.add_argument(
        "--uri",
        default="boardrunner://vehicle/0/sensor/imu/0",
        help="Boardrunner URI (default: %(default)s)",
    )
    p.add_argument(
        "--mount-rotation",
        default="identity",
        choices=sorted(MOUNT_ROTATIONS.keys()),
        help="Chip mount rotation applied to sensor-frame values before publish. "
             "For CubeBlack ICM20602 use 'roll_180' (chip Z-down mount).",
    )
    p.add_argument(
        "--rate-hz",
        type=float,
        default=10.0,
        help="Max publish/print rate. 0 = no throttling. Default 10 Hz.",
    )
    p.add_argument(
        "--temp-c",
        type=float,
        default=25.0,
        help="Synthetic die-temperature in °C (Gazebo IMU has no temp channel).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print SI samples to stdout instead of writing to shm. "
             "Use during Phase 2 topic/unit validation.",
    )
    args = p.parse_args(argv)

    mount = MOUNT_ROTATIONS[args.mount_rotation]

    endpoint = ImuEndpoint(
        topic=args.topic,
        uri=args.uri,
        mount=mount,
        rate_hz=args.rate_hz,
        temp_c=args.temp_c,
        dry_run=args.dry_run,
    )
    endpoint.run()


if __name__ == "__main__":
    main()
