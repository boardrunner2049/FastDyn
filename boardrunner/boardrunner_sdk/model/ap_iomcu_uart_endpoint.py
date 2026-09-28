#!/usr/bin/env python3
# Host-side python endpoint: ap_iomcu_uart_endpoint.py
import argparse
import errno
import mmap
import os
import selectors
import struct
import sys
import termios
import time
import tty

ENV_CANDIDATES = (
    "AP_IOMCU_UART_PTY",
    "UART_PTY",
    "PTY_PATH",
    "SERIAL_PATH",
)

# --- Boardrunner VIO actuator-out publishing ---------------------------------
# Mirrors include/boardrunner/canonical.h:
#   BrShmHeader              (64 B):  6*uint32 + 2*uint64 + 6*uint32
#   BoardrunnerActuatorSample(32 B):  seq, ts_ns, channel, pulse_us, flags, _pad
BR_SHM_MAGIC       = 0x42525653   # 'BRVS'
BR_SHM_VERSION     = 1
BR_SHM_PAGE_SIZE   = 4096
BR_URI_SCHEME      = "boardrunner://"

BR_HEADER_FMT      = "<6I2Q6I"        # 64 B
BR_HEADER_SIZE     = struct.calcsize(BR_HEADER_FMT)
BR_ACTUATOR_FMT    = "<2Q4I"          # 32 B
BR_ACTUATOR_SIZE   = struct.calcsize(BR_ACTUATOR_FMT)
BR_HEADER_OFF_SEQ  = 24               # offset of seq inside BrShmHeader
BR_HEADER_OFF_TS   = 32               # offset of last_write_ns
BR_PAYLOAD_OFF     = BR_HEADER_SIZE   # payload starts right after header
BR_SAMPLE_HEALTHY  = 1 << 0

DEFAULT_ACTUATOR_URI_BASE = "boardrunner://vehicle/0/actuator/pwm"
NUM_PWM_CHANNELS = 16

CODE_READ = 0
CODE_WRITE = 1
CODE_SUCCESS = 0

PAGE_CONFIG = 0
PAGE_STATUS = 1
PAGE_ACTUATORS = 2
PAGE_SERVOS = 3
PAGE_RAW_RC_INPUT = 4
PAGE_RC_INPUT = 5
PAGE_RAW_ADC_INPUT = 6
PAGE_PWM_INFO = 7
PAGE_SETUP = 50
PAGE_DIRECT_PWM = 54

BOOTSTRAP_SEQUENCE = (
    (PAGE_CONFIG, 0, 10),
    (PAGE_STATUS, 0, 5),
    (PAGE_SETUP, 0, 4),
    (PAGE_RAW_RC_INPUT, 0, 8),
    (PAGE_RC_INPUT, 0, 8),
    (PAGE_PWM_INFO, 0, 8),
)
BOOTSTRAP_INITIAL_REPEATS = 0
BOOTSTRAP_INTERVAL_S = 0.005
SELECT_TIMEOUT_S = 0.005
MAX_PENDING_TX = 4096
MAX_RC_CHANNELS = 18
MAX_SERVO_CHANNELS = 16

RAW_RC_COUNT = 0
RAW_RC_FLAGS = 1
RAW_RC_NRSSI = 2
RAW_RC_DATA = 3
RAW_RC_FRAME_COUNT = 4
RAW_RC_LOST_FRAME_COUNT = 5
RAW_RC_CHANNEL_BASE = 6

RAW_RC_FLAG_FAILSAFE = 1 << 1
RAW_RC_FLAG_RC_OK = 1 << 2

crc8_table = [
    0x00, 0x07, 0x0e, 0x09, 0x1c, 0x1b, 0x12, 0x15, 0x38, 0x3f, 0x36, 0x31,
    0x24, 0x23, 0x2a, 0x2d, 0x70, 0x77, 0x7e, 0x79, 0x6c, 0x6b, 0x62, 0x65,
    0x48, 0x4f, 0x46, 0x41, 0x54, 0x53, 0x5a, 0x5d, 0xe0, 0xe7, 0xee, 0xe9,
    0xfc, 0xfb, 0xf2, 0xf5, 0xd8, 0xdf, 0xd6, 0xd1, 0xc4, 0xc3, 0xca, 0xcd,
    0x90, 0x97, 0x9e, 0x99, 0x8c, 0x8b, 0x82, 0x85, 0xa8, 0xaf, 0xa6, 0xa1,
    0xb4, 0xb3, 0xba, 0xbd, 0xc7, 0xc0, 0xc9, 0xce, 0xdb, 0xdc, 0xd5, 0xd2,
    0xff, 0xf8, 0xf1, 0xf6, 0xe3, 0xe4, 0xed, 0xea, 0xb7, 0xb0, 0xb9, 0xbe,
    0xab, 0xac, 0xa5, 0xa2, 0x8f, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9d, 0x9a,
    0x27, 0x20, 0x29, 0x2e, 0x3b, 0x3c, 0x35, 0x32, 0x1f, 0x18, 0x11, 0x16,
    0x03, 0x04, 0x0d, 0x0a, 0x57, 0x50, 0x59, 0x5e, 0x4b, 0x4c, 0x45, 0x42,
    0x6f, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7d, 0x7a, 0x89, 0x8e, 0x87, 0x80,
    0x95, 0x92, 0x9b, 0x9c, 0xb1, 0xb6, 0xbf, 0xb8, 0xad, 0xaa, 0xa3, 0xa4,
    0xf9, 0xfe, 0xf7, 0xf0, 0xe5, 0xe2, 0xeb, 0xec, 0xc1, 0xc6, 0xcf, 0xc8,
    0xdd, 0xda, 0xd3, 0xd4, 0x69, 0x6e, 0x67, 0x60, 0x75, 0x72, 0x7b, 0x7c,
    0x51, 0x56, 0x5f, 0x58, 0x4d, 0x4a, 0x43, 0x44, 0x19, 0x1e, 0x17, 0x10,
    0x05, 0x02, 0x0b, 0x0c, 0x21, 0x26, 0x2f, 0x28, 0x3d, 0x3a, 0x33, 0x34,
    0x4e, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5c, 0x5b, 0x76, 0x71, 0x78, 0x7f,
    0x6a, 0x6d, 0x64, 0x63, 0x3e, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2c, 0x2b,
    0x06, 0x01, 0x08, 0x0f, 0x1a, 0x1d, 0x14, 0x13, 0xae, 0xa9, 0xa0, 0xa7,
    0xb2, 0xb5, 0xbc, 0xbb, 0x96, 0x91, 0x98, 0x9f, 0x8a, 0x8d, 0x84, 0x83,
    0xde, 0xd9, 0xd0, 0xd7, 0xc2, 0xc5, 0xcc, 0xcb, 0xe6, 0xe1, 0xe8, 0xef,
    0xfa, 0xfd, 0xf4, 0xf3
]


def resolve_pty_env():
    for key in ENV_CANDIDATES:
        val = os.environ.get(key)
        if val:
            return val
    return None


def _uri_to_shm_path(uri):
    """URI -> /dev/shm/... path. Mirrors br_uri_to_shm_name in transport/shm.c."""
    if not uri.startswith(BR_URI_SCHEME):
        raise ValueError("URI must start with %r: %r" % (BR_URI_SCHEME, uri))
    tail = uri[len(BR_URI_SCHEME):]
    if not tail:
        raise ValueError("URI has no body: %r" % (uri,))
    name = "/br_" + tail.replace("/", "_")
    return "/dev/shm" + name


class ActuatorShmPublisher:
    """Publishes IOMCU PWM values to per-channel Boardrunner VIO shm segments.

    One segment per PWM channel at boardrunner://<base>/<idx> for idx in
    0..NUM_PWM_CHANNELS-1. Uses the same SPSC seqlock protocol as the C
    transport in boardrunner_vio/src/transport/shm.c so future C readers
    (Gazebo endpoints, probes) can safely observe values.

    Fail-soft: if a segment cannot be opened, the publisher logs and skips
    that channel — the IOMCU endpoint must keep working even when shm is
    unavailable.
    """

    def __init__(self, uri_base, num_channels=NUM_PWM_CHANNELS):
        self.uri_base = uri_base.rstrip("/")
        self.num_channels = num_channels
        self.channel_seq = [0] * num_channels
        self.mmaps = [None] * num_channels
        self.paths = [None] * num_channels
        self._pid = os.getpid()

    def open_all(self):
        """Create and initialize all channel segments. Idempotent."""
        for idx in range(self.num_channels):
            uri = "%s/%d" % (self.uri_base, idx)
            path = _uri_to_shm_path(uri)
            self.paths[idx] = path
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    os.ftruncate(fd, BR_SHM_PAGE_SIZE)
                    mm = mmap.mmap(fd, BR_SHM_PAGE_SIZE)
                finally:
                    os.close(fd)
            except OSError as e:
                sys.stderr.write(
                    "ap_iomcu_uart_endpoint: shm open failed for %s: %s\n"
                    % (path, e))
                continue

            # Write header. seq=0 signals "no sample published yet" to
            # readers; the first note_output_write will bump to 2 (even).
            header = struct.pack(
                BR_HEADER_FMT,
                BR_SHM_MAGIC,      # magic
                BR_SHM_VERSION,    # version
                BR_ACTUATOR_SIZE,  # payload_size
                1,                 # slot_count
                0,                 # flags
                self._pid,         # writer_pid
                0,                 # seq (fresh)
                0,                 # last_write_ns
                0, 0, 0, 0, 0, 0,  # reserved[6]
            )
            mm[0:BR_HEADER_SIZE] = header
            # Zero the payload slot.
            mm[BR_PAYLOAD_OFF:BR_PAYLOAD_OFF + BR_ACTUATOR_SIZE] = b"\x00" * BR_ACTUATOR_SIZE
            self.mmaps[idx] = mm
            self.channel_seq[idx] = 0

    def publish(self, idx, pulse_us):
        """Seqlock-commit one PWM value on channel idx. Fail-soft on error."""
        if idx < 0 or idx >= self.num_channels:
            return
        mm = self.mmaps[idx]
        if mm is None:
            return

        new_seq = self.channel_seq[idx] + 2  # skip odd (mid-write) to next even
        seq_odd = new_seq - 1
        try:
            struct.pack_into("<Q", mm, BR_HEADER_OFF_SEQ, seq_odd)
            ts_ns = time.monotonic_ns()
            payload = struct.pack(
                BR_ACTUATOR_FMT,
                new_seq,              # payload seq matches header seq
                ts_ns,                # timestamp_ns
                idx,                  # channel
                pulse_us & 0xFFFFFFFF,# pulse_us
                BR_SAMPLE_HEALTHY,    # flags
                0,                    # _pad
            )
            mm[BR_PAYLOAD_OFF:BR_PAYLOAD_OFF + BR_ACTUATOR_SIZE] = payload
            struct.pack_into("<Q", mm, BR_HEADER_OFF_TS, ts_ns)
            struct.pack_into("<Q", mm, BR_HEADER_OFF_SEQ, new_seq)
            self.channel_seq[idx] = new_seq
        except (ValueError, OSError) as e:
            sys.stderr.write(
                "ap_iomcu_uart_endpoint: shm publish failed for channel %d: %s\n"
                % (idx, e))

    def close(self):
        for i, mm in enumerate(self.mmaps):
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
                self.mmaps[i] = None


def crc_crc8(data):
    crc = 0x00
    for byte in data:
        i = (crc ^ byte) & 0xFF
        crc = (crc8_table[i] ^ (crc << 8)) & 0xFF
    return crc & 0xFF


def set_raw_mode(fd):
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass


def make_count_code(code, count):
    return ((code & 0x03) << 6) | (count & 0x3F)


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def monotonic_us():
    return int(time.monotonic() * 1000000)


def clamp_u16(value, low=0, high=0xFFFF):
    return max(low, min(high, int(value))) & 0xFFFF


def clamp_pwm(value):
    return clamp_u16(value, 800, 2200)


def parse_pwm_list(value, max_count):
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(clamp_pwm(part))
        if len(out) >= max_count:
            break
    return out


def packet_crc_ok(frame):
    tmp = bytearray(frame)
    recv_crc = tmp[1]
    tmp[1] = 0
    calc_crc = crc_crc8(tmp)
    if calc_crc != recv_crc:
        return False
    return True


def build_read_reply(page, offset, regs):
    count = len(regs)
    reply = bytearray(4 + count * 2)
    reply[0] = make_count_code(CODE_SUCCESS, count)
    reply[1] = 0
    reply[2] = page & 0xFF
    reply[3] = offset & 0xFF
    if count:
        struct.pack_into("<%dH" % count, reply, 4, *regs)
    reply[1] = crc_crc8(reply)
    return reply


def build_write_ack(page, offset):
    reply = bytearray(4)
    reply[0] = make_count_code(CODE_SUCCESS, 0)
    reply[1] = 0
    reply[2] = page & 0xFF
    reply[3] = offset & 0xFF
    reply[1] = crc_crc8(reply)
    return reply


class MockIOMCU:
    def __init__(self, actuator_publisher=None):
        self.pages = {}
        self.seq = 0
        self.status_flags = 0x0001
        self.setup_flags = 0x0000
        self.bootstrap_index = 0
        self.actuator_publisher = actuator_publisher
        self.rc_channels = [1500] * MAX_RC_CHANNELS
        rc_defaults = parse_pwm_list(os.environ.get("AP_IOMCU_RC_DEFAULTS"), MAX_RC_CHANNELS)
        for i, value in enumerate(rc_defaults):
            self.rc_channels[i] = value
        if rc_defaults:
            self.rc_count = len(rc_defaults)
        else:
            self.rc_count = int(os.environ.get("AP_IOMCU_RC_CHANNELS", "8"))
            self.rc_count = max(1, min(MAX_RC_CHANNELS, self.rc_count))
        self.rc_valid = env_flag("AP_IOMCU_RC_VALID", True)
        self.rc_rssi = clamp_u16(os.environ.get("AP_IOMCU_RC_RSSI", "100"), 0, 255)
        self.rc_frame_count = 0
        self.rc_lost_frame_count = 0
        self.rc_updated_us = monotonic_us()
        self.output_channels = [1500] * MAX_SERVO_CHANNELS
        output_defaults = parse_pwm_list(os.environ.get("AP_IOMCU_SERVO_DEFAULTS"), MAX_SERVO_CHANNELS)
        for i, value in enumerate(output_defaults):
            self.output_channels[i] = value
        self.output_seq = 0
        self.output_updated_us = 0
        self._init_defaults()

    def _page(self, page):
        if page not in self.pages:
            self.pages[page] = [0] * 256
        return self.pages[page]

    def _set_u32(self, regs, idx, value):
        regs[idx] = value & 0xFFFF
        regs[idx + 1] = (value >> 16) & 0xFFFF

    def _init_defaults(self):
        cfg = self._page(PAGE_CONFIG)
        cfg[0] = 4
        cfg[1] = 10
        self._set_u32(cfg, 2, 0x12345678)
        self._set_u32(cfg, 4, 0x9ABCDEF0)
        cfg[6] = 32
        cfg[7] = 8
        cfg[8] = 8
        cfg[9] = 4

        status = self._page(PAGE_STATUS)
        status[0] = self.status_flags
        status[1] = 0
        status[2] = 0
        status[3] = 5000
        status[4] = 100

        setup = self._page(PAGE_SETUP)
        setup[0] = self.setup_flags
        setup[1] = 0
        setup[2] = 0
        setup[3] = 0
        try:
            crc_val = int(os.environ.get("IOMCU_CRC", "0xBB9E65BD"), 0)
        except ValueError:
            crc_val = 0xBB9E65BD
        self._set_u32(setup, 11, crc_val)

        pwm = self._page(PAGE_PWM_INFO)
        servos = self._page(PAGE_SERVOS)
        actuators = self._page(PAGE_ACTUATORS)
        direct_pwm = self._page(PAGE_DIRECT_PWM)
        for i in range(MAX_SERVO_CHANNELS):
            pwm[i] = self.output_channels[i]
            servos[i] = self.output_channels[i]
            actuators[i] = self.output_channels[i]
            direct_pwm[i] = self.output_channels[i]

        raw_adc = self._page(PAGE_RAW_ADC_INPUT)
        raw_adc[0] = 5000
        raw_adc[1] = 5000

        self._sync_rc_pages()
        self._sync_output_pages()

    def _sync_rc_pages(self):
        raw_rc = self._page(PAGE_RAW_RC_INPUT)
        rc = self._page(PAGE_RC_INPUT)

        flags = RAW_RC_FLAG_RC_OK if self.rc_valid else RAW_RC_FLAG_FAILSAFE
        raw_rc[RAW_RC_COUNT] = self.rc_count
        raw_rc[RAW_RC_FLAGS] = flags
        raw_rc[RAW_RC_NRSSI] = self.rc_rssi
        raw_rc[RAW_RC_DATA] = 0
        raw_rc[RAW_RC_FRAME_COUNT] = self.rc_frame_count & 0xFFFF
        raw_rc[RAW_RC_LOST_FRAME_COUNT] = self.rc_lost_frame_count & 0xFFFF

        for i in range(MAX_RC_CHANNELS):
            value = self.rc_channels[i] if i < self.rc_count else 0
            rc[i] = value
            idx = RAW_RC_CHANNEL_BASE + i
            if idx < len(raw_rc):
                raw_rc[idx] = value

    def _sync_output_pages(self):
        pwm = self._page(PAGE_PWM_INFO)
        servos = self._page(PAGE_SERVOS)
        actuators = self._page(PAGE_ACTUATORS)
        direct_pwm = self._page(PAGE_DIRECT_PWM)
        for i, value in enumerate(self.output_channels):
            pwm[i] = value
            servos[i] = value
            actuators[i] = value
            direct_pwm[i] = value

    def set_rc_input(self, channels=None, valid=None, rssi=None, timestamp_us=None):
        if channels is not None:
            if len(channels) > MAX_RC_CHANNELS:
                raise ValueError(f"channels may contain at most {MAX_RC_CHANNELS} values")
            for i, value in enumerate(channels):
                self.rc_channels[i] = clamp_pwm(value)
            self.rc_count = len(channels)
        if valid is not None:
            self.rc_valid = bool(valid)
        if rssi is not None:
            self.rc_rssi = clamp_u16(rssi, 0, 255)

        self.rc_updated_us = int(timestamp_us) if timestamp_us is not None else monotonic_us()
        self.rc_frame_count = (self.rc_frame_count + 1) & 0xFFFF
        if not self.rc_valid:
            self.rc_lost_frame_count = (self.rc_lost_frame_count + 1) & 0xFFFF
        self._sync_rc_pages()

    def note_output_write(self, offset, values):
        for i, value in enumerate(values):
            idx = offset + i
            if 0 <= idx < len(self.output_channels):
                self.output_channels[idx] = clamp_u16(value)
                if self.actuator_publisher is not None:
                    self.actuator_publisher.publish(idx, self.output_channels[idx])
        self.output_seq = (self.output_seq + 1) & 0xFFFFFFFF
        self.output_updated_us = monotonic_us()
        self._sync_output_pages()

    def _refresh_dynamic_state(self):
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        status = self._page(PAGE_STATUS)
        status[0] = self.status_flags
        status[1] = self.seq & 0xFFFF
        status[2] = (self.seq >> 16) & 0xFFFF
        status[3] = 5000
        status[4] = 100

        setup = self._page(PAGE_SETUP)
        setup[0] = self.setup_flags
        if self.rc_valid:
            self.rc_frame_count = (self.rc_frame_count + 1) & 0xFFFF
        else:
            self.rc_lost_frame_count = (self.rc_lost_frame_count + 1) & 0xFFFF
        self._sync_rc_pages()

    def read_regs(self, page, offset, count):
        self._refresh_dynamic_state()
        regs = self._page(page)
        end = min(offset + count, len(regs))
        out = list(regs[offset:end])
        if len(out) < count:
            out.extend([0] * (count - len(out)))
        return out

    def write_regs(self, page, offset, values):
        regs = self._page(page)
        for i, value in enumerate(values):
            idx = offset + i
            if 0 <= idx < len(regs):
                regs[idx] = value & 0xFFFF

        if page in (PAGE_ACTUATORS, PAGE_DIRECT_PWM, PAGE_SERVOS):
            self.note_output_write(offset, values)

        if page == PAGE_SETUP and offset == 0 and values:
            self.setup_flags = values[0] & 0xFFFF

        if page == PAGE_SETUP:
            status = self._page(PAGE_STATUS)
            status[0] = self.status_flags

    def bootstrap_frame(self):
        page, offset, count = BOOTSTRAP_SEQUENCE[
            self.bootstrap_index % len(BOOTSTRAP_SEQUENCE)
        ]
        self.bootstrap_index += 1
        print(f"[IOMCU Mock] Received Read Request for PAGE {page} (Offset {offset}, Count {count})", flush=True)
        if page == PAGE_SETUP:
            print("[IOMCU Mock] SUCCESS: The firmware advanced past the initialization timeout and requested PAGE_SETUP!", flush=True)
        return build_read_reply(page, offset, self.read_regs(page, offset, count))

    def bootstrap_burst(self, repeats=1):
        burst = bytearray()
        repeats = int(repeats)
        if repeats <= 0:
            return burst
        total = repeats * len(BOOTSTRAP_SEQUENCE)
        for _ in range(total):
            burst.extend(self.bootstrap_frame())
        return burst


def open_endpoint(path):
    use_existing = os.environ.get("AP_IOMCU_UART_USE_EXISTING_PTY", "").lower() in (
        "1",
        "true",
        "yes",
    )

    if path and os.path.exists(path) and use_existing:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        set_raw_mode(fd)
        resolved = path
        try:
            resolved = os.path.realpath(path)
        except Exception:
            pass
        sys.stderr.write(
            "ap_iomcu_uart_endpoint: Mock started on existing %s%s\n"
            % (
                path,
                "" if resolved == path else " -> %s" % resolved,
            )
        )
        return fd

    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass

    master, slave = os.openpty()
    tty_name = os.ttyname(slave)
    set_raw_mode(slave)
    link_path = path if path else "/tmp/usart6_pty"
    try:
        if os.path.lexists(link_path):
            os.remove(link_path)
        os.symlink(tty_name, link_path)
    except Exception as e:
        with open("/tmp/endpoint.log", "a") as f: f.write("Failed to symlink %s: %s\n" % (link_path, e))
    os.close(slave)
    with open("/tmp/endpoint.log", "a") as f: f.write(
        "ap_iomcu_uart_endpoint: Mock created PTY at %s (symlinked to %s)\n"
        % (tty_name, link_path)
    )
    return master


def handle_frame(iomcu, frame):
    with open("/tmp/endpoint.log", "a") as f: f.write(f"RX Frame: code={frame[0]>>6} count={frame[0]&0x3F} page={frame[2]} offset={frame[3]}\n")
    count_code = frame[0]
    count = count_code & 0x3F
    code = count_code >> 6
    page = frame[2]
    offset = frame[3]

    if code == CODE_READ:
        return build_read_reply(page, offset, iomcu.read_regs(page, offset, count))

    if code == CODE_WRITE:
        values = []
        if count:
            values = list(struct.unpack("<%dH" % count, frame[4:4 + count * 2]))
        iomcu.write_regs(page, offset, values)
        return build_write_ack(page, offset)

    return build_read_reply(page, offset, [0] * count)


def flush_pending(fd, pending):
    if not pending:
        return
    try:
        n = os.write(fd, pending)
        with open("/tmp/endpoint_write.log", "a") as f:
            f.write(f"Wrote {n} bytes to pty\n")
        del pending[:n]
    except BlockingIOError:
        pass
    except Exception as e:
        print(f"Failed to write to pty: {e}")


def update_selector(sel, fd, pending_tx):
    mask = selectors.EVENT_READ
    if pending_tx:
        mask |= selectors.EVENT_WRITE
    sel.modify(fd, mask, "pty")


def parse_cli():
    parser = argparse.ArgumentParser(
        description="ArduPilot IOMCU UART mock endpoint (Boardrunner VIO)")
    parser.add_argument(
        "path", nargs="?", default=None,
        help="PTY path or symlink to bind. Falls back to env vars if omitted.")
    parser.add_argument(
        "--actuator-uri-base", default=None,
        help=("Boardrunner URI base for PWM publishing. Per-channel segments "
              "are created as <base>/0 .. <base>/%d. "
              "Default: %s. Pass empty string to disable shm publishing."
              % (NUM_PWM_CHANNELS - 1, DEFAULT_ACTUATOR_URI_BASE)))
    return parser.parse_args()


def main():
    args = parse_cli()
    path = args.path or resolve_pty_env()

    if args.actuator_uri_base is None:
        uri_base = os.environ.get("AP_IOMCU_ACTUATOR_URI_BASE",
                                  DEFAULT_ACTUATOR_URI_BASE)
    else:
        uri_base = args.actuator_uri_base

    actuator_publisher = None
    if uri_base:
        actuator_publisher = ActuatorShmPublisher(uri_base)
        actuator_publisher.open_all()
        sys.stderr.write(
            "ap_iomcu_uart_endpoint: publishing PWM to %s/{0..%d}\n"
            % (uri_base, NUM_PWM_CHANNELS - 1))
    else:
        sys.stderr.write(
            "ap_iomcu_uart_endpoint: actuator shm publishing disabled\n")

    fd = open_endpoint(path)

    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ, "pty")
    sys.stderr.flush()

    iomcu = MockIOMCU(actuator_publisher=actuator_publisher)
    buf = bytearray()
    pending_tx = bytearray()
    saw_valid_request = False
    next_bootstrap_at = time.monotonic()

    # Do not send any unsolicited bootstrap frames. Sit in silence
    # and strictly wait for the firmware to initiate communication.
    pending_tx.clear()
    update_selector(sel, fd, pending_tx)
    next_bootstrap_at = time.monotonic() + BOOTSTRAP_INTERVAL_S

    try:
        while True:
            if saw_valid_request:
                timeout = SELECT_TIMEOUT_S
            else:
                timeout = max(
                    0.0,
                    min(SELECT_TIMEOUT_S, next_bootstrap_at - time.monotonic()),
                )

            events = sel.select(timeout=timeout)
            for key, event_mask in events:
                if key.data != "pty":
                    continue

                if event_mask & selectors.EVENT_READ:
                    try:
                        data = os.read(fd, 1024)
                    except BlockingIOError:
                        data = b""
                    except OSError as e:
                        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EIO):
                            data = b""
                        else:
                            raise
                    if data:
                        buf.extend(data)

                if event_mask & selectors.EVENT_WRITE:
                    flush_pending(fd, pending_tx)

            while len(buf) >= 4:
                count_code = buf[0]
                count = count_code & 0x3F
                code = count_code >> 6
                page = buf[2]
                offset = buf[3]

                if count == 0:
                    del buf[0]
                    continue

                if code == CODE_READ:
                    expected_len_1 = 4
                    expected_len_2 = 4 + count * 2

                    # Print the raw buffer here so we can see what the firmware sent!
                    print(f"[IOMCU Mock] Raw RX buf for CODE_READ: {buf[:expected_len_2].hex()} (count={count})", flush=True)

                    if len(buf) >= expected_len_1 and packet_crc_ok(bytes(buf[:expected_len_1])):
                        expected_len = expected_len_1
                    elif len(buf) >= expected_len_2 and packet_crc_ok(bytes(buf[:expected_len_2])):
                        expected_len = expected_len_2
                    else:
                        if len(buf) < expected_len_2:
                            break # Wait for more bytes just in case it's legacy format
                        del buf[0]
                        continue
                else:
                    expected_len = 4 + count * 2
                    if len(buf) < expected_len:
                        break
                    if not packet_crc_ok(bytes(buf[:expected_len])):
                        del buf[0]
                        continue

                frame = bytes(buf[:expected_len])

                del buf[:expected_len]
                saw_valid_request = True
                if code == CODE_READ:
                    print(f"[IOMCU Mock] Received Read Request: PAGE {page}, OFFSET {offset}", flush=True)
                    if page == PAGE_SETUP:
                        print("[IOMCU Mock] SUCCESS: Firmware requested PAGE_SETUP! Initialization succeeded!", flush=True)

                # Once the guest starts actively talking to us, prioritize
                # strict request/response traffic over any unsent bootstrap
                # packets.
                pending_tx.clear()

                reply = handle_frame(iomcu, frame)
                pending_tx.extend(reply)
                flush_pending(fd, pending_tx)

            if (
                not saw_valid_request
                and len(pending_tx) < MAX_PENDING_TX
                and time.monotonic() >= next_bootstrap_at
            ):
                pass # pending_tx.extend(iomcu.bootstrap_frame())
                # flush_pending(fd, pending_tx)
                next_bootstrap_at = time.monotonic() + BOOTSTRAP_INTERVAL_S

            update_selector(sel, fd, pending_tx)
    except KeyboardInterrupt:
        return 0
    finally:
        os.close(fd)
        if actuator_publisher is not None:
            actuator_publisher.close()

    return 0


if __name__ == '__main__':
    sys.exit(main())
