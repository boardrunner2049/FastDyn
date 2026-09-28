set pagination off
set confirm off
set print thread-events off
set style enabled off
set breakpoint pending on
set breakpoint auto-hw on

# ArduRover 4.6.2 CubeBlack — USART2 DMA vs direct-DR diagnostic.
#
# Goal: decide whether the current usart2.c model is missing bytes because
#   (a) firmware bypasses DR and drives DMA1 Stream 6 -> USART2_DR, or
#   (b) firmware writes DR directly but the PTY plumbing loses the byte.
#
# Prerequisite (must be running BEFORE fastdyn starts):
#   socat -d -d PTY,link=/tmp/usart2_pty,raw,echo=0 \
#                PTY,link=/tmp/usart2_host,raw,echo=0
#
# Then:
#   fastdyn probe-run -c configs/rover462.toml -o fastdyn_recent_run \
#     --rtos-introspect --rtos-introspect-mode events \
#     --rtos-introspection-max-events 16384 --run-gdb
#
#   script -q -f fastdyn_work/gdb_usart2_dma_diag.log \
#     -c 'gdb-multiarch -q -x gdbscripts/ardurover_usart2_dma_diag.gdb'
#
# STM32F427 CubeBlack USART2 TX = DMA1 Stream 6 Channel 4.
#   DMA1 base   = 0x40026000
#   Stream 6 CR = 0x400260A0
#   Stream 6 PAR= 0x400260A8
#   USART2 DR   = 0x40004404
#
# Watchpoints on NDTR/M0AR are intentionally omitted — they change on every
# DMA byte and would slow the run dramatically. CR + PAR is enough to prove
# the firmware is arming the stream toward USART2.

file virtuals/physics/flight_controllers/courbet/bin/ardurover_v462

python
import os
import gdb

def _add_directory(path, label):
    if not path:
        return
    if "\n" in path or "\r" in path:
        return
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(path):
        return
    escaped = path.replace("\\", "\\\\").replace(" ", "\\ ")
    gdb.execute("directory %s" % escaped, to_string=True)

_add_directory(os.environ.get("ARDUPILOT_SOURCE_ROOT"), "ArduPilot source")
_add_directory(os.environ.get("ARDUPILOT_BUILD_ROOT"), "ArduPilot build root")
build_root = os.environ.get("ARDUPILOT_BUILD_ROOT")
if build_root:
    _add_directory(os.path.join(build_root, "build", "CubeBlack"), "CubeBlack build")
end

target remote localhost:1234

printf "\n[dma-diag] USART2 DMA-vs-DR diagnostic loaded\n"
printf "[dma-diag] watching:\n"
printf "[dma-diag]   0x40004404  USART2 DR       (direct CPU writes)\n"
printf "[dma-diag]   0x400260A0  DMA1 S6 CR      (arming the stream)\n"
printf "[dma-diag]   0x400260A8  DMA1 S6 PAR     (target should be 0x40004404)\n"
printf "[dma-diag]   symbol      _mav_finalize_message_chan_send (packet count)\n"

python
import gdb, threading, time

state = {
    "dr_writes":       0,   # CPU writes to 0x40004404
    "s6_cr_writes":    0,   # firmware writing DMA1 Stream 6 CR
    "s6_par_writes":   0,   # firmware writing DMA1 Stream 6 PAR
    "s6_par_last":     None,
    "s6_cr_en_seen":   0,   # times CR was written with EN=1
    "tx_finalize":     0,   # baseline: MAVLink packets leaving GCS
}

def _reg(name):
    try:
        return int(gdb.parse_and_eval("$" + name)) & 0xFFFFFFFF
    except Exception:
        return 0

def _hex(v):
    return "0x%08x" % (v & 0xFFFFFFFF)

def _read_u32(addr):
    try:
        return int(gdb.parse_and_eval("*(uint32_t*)0x%08x" % addr)) & 0xFFFFFFFF
    except Exception:
        return 0

class DrWriteWatch(gdb.Breakpoint):
    def __init__(self):
        super(DrWriteWatch, self).__init__(
            "*(uint32_t*)0x40004404",
            type=gdb.BP_WATCHPOINT,
            internal=True,
        )
    def stop(self):
        state["dr_writes"] += 1
        n = state["dr_writes"]
        if n <= 5 or n % 200 == 0:
            gdb.write("[dr-direct] USART2 DR write #%d pc=%s lr=%s\n" %
                      (n, _hex(_reg("pc")), _hex(_reg("lr"))))
        return False

class S6CrWatch(gdb.Breakpoint):
    def __init__(self):
        super(S6CrWatch, self).__init__(
            "*(uint32_t*)0x400260A0",
            type=gdb.BP_WATCHPOINT,
            internal=True,
        )
    def stop(self):
        state["s6_cr_writes"] += 1
        # Sample the CR value AFTER the write — bit 0 (EN) tells us if this armed a transfer.
        cr = _read_u32(0x400260A0)
        if (cr & 0x1) != 0:
            state["s6_cr_en_seen"] += 1
        n = state["s6_cr_writes"]
        if n <= 5 or n % 200 == 0:
            gdb.write("[dma-cfg] DMA1 S6 CR write #%d value=%s (EN=%d) pc=%s\n" %
                      (n, _hex(cr), cr & 0x1, _hex(_reg("pc"))))
        return False

class S6ParWatch(gdb.Breakpoint):
    def __init__(self):
        super(S6ParWatch, self).__init__(
            "*(uint32_t*)0x400260A8",
            type=gdb.BP_WATCHPOINT,
            internal=True,
        )
    def stop(self):
        state["s6_par_writes"] += 1
        par = _read_u32(0x400260A8)
        state["s6_par_last"] = par
        n = state["s6_par_writes"]
        if n <= 5 or n % 200 == 0:
            marker = "  (matches USART2 DR)" if par == 0x40004404 else "  (target=%s)" % _hex(par)
            gdb.write("[dma-cfg] DMA1 S6 PAR write #%d = %s%s pc=%s\n" %
                      (n, _hex(par), marker, _hex(_reg("pc"))))
        return False

class TxProbe(gdb.Breakpoint):
    def __init__(self):
        super(TxProbe, self).__init__(
            "_mav_finalize_message_chan_send",
            type=gdb.BP_BREAKPOINT,
            internal=True,
        )
    def stop(self):
        state["tx_finalize"] += 1
        return False

DrWriteWatch()
S6CrWatch()
S6ParWatch()
TxProbe()

def _report():
    gdb.write("\n[summary]\n")
    gdb.write("  MAVLink packets finalized (tx_finalize)   = %d\n" % state["tx_finalize"])
    gdb.write("  CPU writes to USART2 DR   (dr_writes)     = %d\n" % state["dr_writes"])
    gdb.write("  DMA1 S6 CR   writes                       = %d  (EN=1 seen: %d)\n" %
              (state["s6_cr_writes"], state["s6_cr_en_seen"]))
    gdb.write("  DMA1 S6 PAR  writes                       = %d  (last=%s)\n" %
              (state["s6_par_writes"],
               _hex(state["s6_par_last"]) if state["s6_par_last"] is not None else "none"))

    dr = state["dr_writes"]
    cr = state["s6_cr_writes"]
    par_ok = (state["s6_par_last"] == 0x40004404) if state["s6_par_last"] is not None else False

    if state["tx_finalize"] == 0:
        gdb.write("[verdict] Inconclusive: no MAVLink packets yet. Give it more time.\n")
    elif dr == 0 and cr > 0 and par_ok:
        gdb.write("[verdict] DMA path CONFIRMED. Firmware never wrote DR; it armed DMA1 Stream 6 with PAR=0x40004404.\n")
        gdb.write("[next]    Fix dma1.c to kick generic streams, add usart2.c DMA callback.\n")
    elif dr > 0 and cr == 0:
        gdb.write("[verdict] DIRECT-DR path. DMA1 Stream 6 was never armed; CPU wrote DR directly.\n")
        gdb.write("[next]    Fix is in usart2.c PTY plumbing (api_pty_fd_gen path, timing, or forwarding).\n")
    elif dr > 0 and cr > 0:
        gdb.write("[verdict] MIXED. Firmware uses direct DR early (banner/setup) and DMA for steady-state.\n")
        gdb.write("[next]    Both paths need to reach the PTY. DMA fix unblocks steady-state MAVLink.\n")
    elif cr > 0 and not par_ok:
        gdb.write("[verdict] AMBIGUOUS. Stream 6 is being programmed but not toward USART2 DR.\n")
        gdb.write("[next]    Recheck hwdef — MAVLink telemetry may be on a different USART.\n")
    else:
        gdb.write("[verdict] Inconclusive combination — capture more time and rerun.\n")

class _Ticker(threading.Thread):
    def run(self):
        while True:
            time.sleep(10)
            try:
                gdb.post_event(_report)
            except Exception:
                return

_Ticker(daemon=True).start()
end

printf "[dma-diag] probes installed; continuing target\n"
continue
