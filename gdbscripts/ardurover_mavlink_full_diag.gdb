set pagination off
set confirm off
set print thread-events off
set style enabled off
set breakpoint pending on
set breakpoint auto-hw on

# ArduRover 4.6.2 CubeBlack — LEAN MAVLink-out-of-USART2 diagnostic.
#
# Design goal:
#   Prior debug scripts installed hardware watchpoints on hot MMIO addresses
#   (USART2 CR1/CR3/DR, NVIC ISER1). In QEMU, hardware watchpoints force
#   single-stepping around the watched address; combined with `icount = 5`
#   in rover462.toml that dilated virtual time so much that MAVLink never
#   reached its first heartbeat inside a normal debug session.
#
#   This script uses ONLY breakpoints — one per interesting symbol — and
#   samples the interesting MMIO state on demand when those breakpoints
#   fire. QEMU keeps running at full TCG speed between them.
#
# What we watch (one breakpoint each, cheap):
#   * _mav_finalize_message_chan_send  -> MAVLink packet leaves GCS. On hit,
#     read USART2 SR/CR1/CR3 and NVIC ISER1 and print. Reports channel too.
#   * Vector54                         -> USART2 IRQ vector entry.
#   * sd_lld_serve_interrupt           -> ChibiOS serial ISR body.
#   * _ZN3GCS11setup_uartsEv           -> GCS bound its channels to UARTs.
#   * _ZN11GCS_MAVLINK11send_bannerEv  -> heartbeat/banner send fires soon
#     after MAVLink is up on the channel.
#   * _ZN7ChibiOS10UARTDriver6_beginEmtt (first hit) -> UART opened.
#
# Custom commands (interactive isolation):
#   (gdb) probe-summary
#   (gdb) probe-force-pending    -> set NVIC ISER1|=bit6 and ISPR1=bit6
#   (gdb) probe-inject-dr [BYTE] -> write BYTE to USART2 DR (0x40004404)
#
# Prereqs (start BEFORE launching FastDyn; otherwise api_pty_fd_gen('usart2')
# returns -1 and no bytes ever leave):
#   socat -d -d PTY,link=/tmp/usart2_pty,raw,echo=0 \
#                PTY,link=/tmp/usart2_host,raw,echo=0
#   minicom -D /tmp/usart2_host -b 57600 --displayhex
#   fastdyn probe-run -c configs/rover462.toml -o fastdyn_recent_run \
#     --rtos-introspect --rtos-introspect-mode events \
#     --rtos-introspection-max-events 16384 --run-gdb
#   script -q -f fastdyn_work/gdb_mavlink_full_diag.log \
#     -c 'gdb-multiarch -q -x gdbscripts/ardurover_mavlink_full_diag.gdb'

file virtuals/physics/flight_controllers/courbet/bin/ardurover_v462

python
import os, gdb

def _add_directory(path, label):
    if not path or "\n" in path or "\r" in path:
        return
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(path):
        return
    escaped = path.replace("\\", "\\\\").replace(" ", "\\ ")
    gdb.execute("directory %s" % escaped, to_string=True)

_add_directory(os.environ.get("ARDUPILOT_SOURCE_ROOT"), "ArduPilot source")
_add_directory(os.environ.get("ARDUPILOT_BUILD_ROOT"), "ArduPilot build root")
_build_root = os.environ.get("ARDUPILOT_BUILD_ROOT")
if _build_root:
    _add_directory(os.path.join(_build_root, "build", "CubeBlack"), "CubeBlack build")
end

target remote localhost:1234

printf "\n[full-diag] LEAN MAVLink-on-USART2 diagnostic loaded\n"
printf "[full-diag] Channel: SERIAL1 = USART2 (TELEM1) = mavlink_channel_t 1\n"
printf "[full-diag] No hot-address watchpoints — QEMU runs at full speed between events.\n"

python
import gdb, threading, time
from collections import Counter

# USART2 register addresses (STM32F427):
U2_SR      = 0x40004400
U2_DR      = 0x40004404
U2_BRR     = 0x40004408
U2_CR1     = 0x4000440C
U2_CR3     = 0x40004414
NVIC_ISER1 = 0xE000E104
NVIC_ISPR1 = 0xE000E204

state = {
    "tx_finalize":       0,
    "chan_hist":         Counter(),
    "chan1_snapshots":   0,       # SR/CR1/CR3/NVIC captured on chan=1 packet
    "vector54_hits":     0,
    "sd_lld_hits":       0,
    "uart_begin_hit":    False,
    "setup_uarts_hit":   False,
    "banner_hit":        False,
    "forced_pending":    0,
    "forced_dr_inject":  0,
}

def _reg(name):
    try:
        return int(gdb.parse_and_eval("$" + name)) & 0xFFFFFFFF
    except Exception:
        return 0

def _hex(v):
    if v is None:
        return "none"
    return "0x%08x" % (v & 0xFFFFFFFF)

def _read_u32(addr):
    try:
        return int(gdb.parse_and_eval("*(uint32_t*)0x%08x" % addr)) & 0xFFFFFFFF
    except Exception:
        return 0

def _write_u32(addr, value):
    try:
        gdb.execute("set *(uint32_t*)0x%08x = 0x%08x" %
                    (addr, value & 0xFFFFFFFF), to_string=True)
        return True
    except Exception as exc:
        gdb.write("[force] write to %s failed: %s\n" % (_hex(addr), exc))
        return False

def _decode_cr1(v):
    return ("UE=%d TE=%d RE=%d RXNEIE=%d TXEIE=%d TCIE=%d" %
            ((v >> 13) & 1, (v >> 3) & 1, (v >> 2) & 1,
             (v >> 5) & 1, (v >> 7) & 1, (v >> 6) & 1))

def _decode_cr3(v):
    return "DMAT=%d DMAR=%d" % ((v >> 7) & 1, (v >> 6) & 1)

def _snapshot_usart2(label):
    sr = _read_u32(U2_SR); cr1 = _read_u32(U2_CR1); cr3 = _read_u32(U2_CR3)
    iser1 = _read_u32(NVIC_ISER1); ispr1 = _read_u32(NVIC_ISPR1)
    gdb.write("[%s] USART2 SR=%s  CR1=%s [%s]\n" %
              (label, _hex(sr), _hex(cr1), _decode_cr1(cr1)))
    gdb.write("[%s] USART2 CR3=%s [%s]\n" %
              (label, _hex(cr3), _decode_cr3(cr3)))
    gdb.write("[%s] NVIC   ISER1=%s (bit6/USART2=%d)  ISPR1=%s (bit6=%d)\n" %
              (label, _hex(iser1), (iser1 >> 6) & 1,
               _hex(ispr1), (ispr1 >> 6) & 1))

# ---------------------------------------------------------------------------
# Breakpoints — each is a rare event, cheap to instrument
# ---------------------------------------------------------------------------

class ChanProbe(gdb.Breakpoint):
    """Every MAVLink packet leaving GCS. On chan==1, snapshot USART2+NVIC."""
    def __init__(self):
        super(ChanProbe, self).__init__(
            "_mav_finalize_message_chan_send",
            type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        chan = _reg("r0") & 0xFFFF
        state["tx_finalize"] += 1
        state["chan_hist"][chan] += 1
        n = state["tx_finalize"]
        if n <= 12 or n % 50 == 0:
            gdb.write("[mav] finalize #%d chan=%d pc=%s lr=%s\n" %
                      (n, chan, _hex(_reg("pc")), _hex(_reg("lr"))))
            if chan == 1:
                state["chan1_snapshots"] += 1
                _snapshot_usart2("mav-chan1")
        return False

class Vector54Probe(gdb.Breakpoint):
    """USART2 IRQ vector entry."""
    def __init__(self):
        super(Vector54Probe, self).__init__(
            "*0x08005302", type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        state["vector54_hits"] += 1
        n = state["vector54_hits"]
        if n <= 5 or n % 200 == 0:
            gdb.write("[isr] Vector54 (USART2) entry #%d pc=%s lr=%s\n" %
                      (n, _hex(_reg("pc")), _hex(_reg("lr"))))
        return False

class SdLldProbe(gdb.Breakpoint):
    """ChibiOS sd_lld_serve_interrupt — real serial ISR body."""
    def __init__(self):
        super(SdLldProbe, self).__init__(
            "*0x0814c480", type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        state["sd_lld_hits"] += 1
        n = state["sd_lld_hits"]
        if n <= 5 or n % 200 == 0:
            gdb.write("[isr] sd_lld_serve_interrupt #%d\n" % n)
        return False

class OnceProbe(gdb.Breakpoint):
    """Print first hit only; then keep counting cheaply."""
    def __init__(self, spec, label, key):
        super(OnceProbe, self).__init__(spec, type=gdb.BP_BREAKPOINT,
                                        internal=True)
        self.label = label
        self.key = key
    def stop(self):
        if not state[self.key]:
            state[self.key] = True
            gdb.write("[first] %s | pc=%s lr=%s\n" %
                      (self.label, _hex(_reg("pc")), _hex(_reg("lr"))))
        return False

ChanProbe()
Vector54Probe()
SdLldProbe()
OnceProbe("_ZN7ChibiOS10UARTDriver6_beginEmtt",
          "ChibiOS::UARTDriver::_begin (UART opened)", "uart_begin_hit")
OnceProbe("_ZN3GCS11setup_uartsEv",
          "GCS::setup_uarts (channels bound to UARTs)", "setup_uarts_hit")
OnceProbe("_ZN11GCS_MAVLINK11send_bannerEv",
          "GCS_MAVLINK::send_banner (first heartbeat imminent)", "banner_hit")

# ---------------------------------------------------------------------------
# User-callable commands
# ---------------------------------------------------------------------------

class ProbeForcePending(gdb.Command):
    """Set NVIC ISER1|=bit6 and ISPR1=bit6 from GDB, bypassing any stub.

    If Vector54 fires after this, the plugin's IRQ dispatch path is fine and
    the only blocker is 'firmware never asks the USART2 model to raise IRQ'
    (i.e. TXEIE stays 0).
    """
    def __init__(self):
        super(ProbeForcePending, self).__init__("probe-force-pending",
                                                gdb.COMMAND_USER)
    def invoke(self, arg, from_tty):
        before = state["vector54_hits"]
        iser = _read_u32(NVIC_ISER1); ispr = _read_u32(NVIC_ISPR1)
        gdb.write("\n[force] BEFORE Vector54=%d  ISER1=%s  ISPR1=%s\n" %
                  (before, _hex(iser), _hex(ispr)))
        _write_u32(NVIC_ISER1, iser | (1 << 6))
        _write_u32(NVIC_ISPR1, 1 << 6)
        state["forced_pending"] += 1
        gdb.write("[force] AFTER  ISER1=%s  ISPR1=%s -- continue and watch Vector54\n" %
                  (_hex(_read_u32(NVIC_ISER1)), _hex(_read_u32(NVIC_ISPR1))))

class ProbeInjectDr(gdb.Command):
    """probe-inject-dr [BYTE]  — write BYTE (default 0x55) to USART2 DR.

    If BYTE lands on /tmp/usart2_host in minicom, usart2.c PTY forwarding
    works end-to-end and the only missing link is firmware pushing bytes.
    """
    def __init__(self):
        super(ProbeInjectDr, self).__init__("probe-inject-dr",
                                            gdb.COMMAND_USER)
    def invoke(self, arg, from_tty):
        byte = 0x55
        arg = (arg or "").strip()
        if arg:
            try:
                byte = int(arg, 0) & 0xFF
            except ValueError:
                gdb.write("[inject] bad byte '%s' — using 0x55\n" % arg)
        _write_u32(U2_DR, byte)
        state["forced_dr_inject"] += 1
        gdb.write("[inject] wrote 0x%02x to USART2 DR — check /tmp/usart2_host\n" %
                  byte)

class ProbeSummary(gdb.Command):
    def __init__(self):
        super(ProbeSummary, self).__init__("probe-summary", gdb.COMMAND_USER)
    def invoke(self, arg, from_tty):
        _report()

class ProbeSnapshot(gdb.Command):
    """probe-snapshot  — dump USART2 SR/CR1/CR3 and NVIC ISER1/ISPR1 now."""
    def __init__(self):
        super(ProbeSnapshot, self).__init__("probe-snapshot", gdb.COMMAND_USER)
    def invoke(self, arg, from_tty):
        _snapshot_usart2("snapshot")

ProbeForcePending()
ProbeInjectDr()
ProbeSummary()
ProbeSnapshot()

def _report():
    gdb.write("\n[summary] ====== lean full-diag =======================================\n")
    gdb.write("  MAVLink packets finalized     = %d\n" % state["tx_finalize"])
    if state["chan_hist"]:
        gdb.write("  chan distribution             = %s\n" %
                  ", ".join("chan%d=%d" % (c, n)
                            for c, n in sorted(state["chan_hist"].items())))
    gdb.write("  chan1 (USART2) snapshots taken = %d\n" % state["chan1_snapshots"])
    gdb.write("  ISR:\n")
    gdb.write("    Vector54 (USART2 entry)     = %d\n" % state["vector54_hits"])
    gdb.write("    sd_lld_serve_interrupt      = %d\n" % state["sd_lld_hits"])
    gdb.write("  Firmware progress markers:\n")
    gdb.write("    UARTDriver::_begin seen     = %s\n" % state["uart_begin_hit"])
    gdb.write("    GCS::setup_uarts    seen    = %s\n" % state["setup_uarts_hit"])
    gdb.write("    GCS_MAVLINK::send_banner    = %s\n" % state["banner_hit"])
    gdb.write("  Forced trials from GDB:\n")
    gdb.write("    probe-force-pending calls   = %d\n" % state["forced_pending"])
    gdb.write("    probe-inject-dr calls       = %d\n" % state["forced_dr_inject"])
    # Live snapshot on every summary — useful when nothing is happening
    _snapshot_usart2("summary")

    # Verdicts
    verdicts = []
    if not state["uart_begin_hit"]:
        verdicts.append("UART not opened yet — firmware is still in early setup. Wait longer.")
    elif not state["setup_uarts_hit"]:
        verdicts.append("UART opened but GCS::setup_uarts hasn't run — MAVLink layer not yet initialised.")
    elif state["tx_finalize"] == 0:
        verdicts.append("GCS setup ran but no MAVLink packet has been finalised. "
                        "Waiting for AP_Vehicle::setup to reach the scheduler.")
    elif state["chan_hist"].get(1, 0) == 0:
        verdicts.append("MAVLink is finalising packets but none went to chan1 (USART2) yet — "
                        "primary channel is likely chan0 (USB).")
    elif state["chan_hist"].get(1, 0) > 0 and state["vector54_hits"] == 0:
        verdicts.append("chan1 packets ARE being sent but Vector54 has NEVER fired. "
                        "Either the USART2 model isn't calling qemu_plugin_raise_irq, "
                        "or the plugin's IRQ dispatch is being masked (PRIMASK / disabled ISER). "
                        "Run `probe-force-pending` next to isolate.")
    elif state["vector54_hits"] > 0:
        verdicts.append("ISR IS firing. If /tmp/usart2_host is empty, run `probe-inject-dr` "
                        "to test the PTY path independently.")
    for v in verdicts:
        gdb.write("[verdict] %s\n" % v)

class _Ticker(threading.Thread):
    def run(self):
        while True:
            time.sleep(15)
            try:
                gdb.post_event(_report)
            except Exception:
                return

_Ticker(daemon=True).start()
end

printf "[full-diag] probes installed. Custom commands:\n"
printf "[full-diag]   (gdb) probe-summary          -> print summary now\n"
printf "[full-diag]   (gdb) probe-snapshot         -> dump USART2+NVIC state\n"
printf "[full-diag]   (gdb) probe-force-pending    -> force NVIC ISPR1 bit6 = pending IRQ38\n"
printf "[full-diag]   (gdb) probe-inject-dr [BYTE] -> write BYTE (default 0x55) to USART2 DR\n"
printf "[full-diag] continuing target — summary + register snapshot every 15 s\n"
continue
