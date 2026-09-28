set pagination off
set confirm off
set print thread-events off
set style enabled off
set breakpoint pending on
set breakpoint auto-hw on

# ArduRover 4.6.2 CubeBlack — MAVLink routing diagnostic.
#
# Answers: which mavlink_channel_t does _mav_finalize_message_chan_send get
# called with, and which peripheral DR / DMA stream carries the bytes.
#
# CubeBlack SERIAL_ORDER (fmuv3 hwdef.dat:132):
#   SERIAL0 = OTG1   (USB)     -> mavlink_channel_t 0
#   SERIAL1 = USART2 (TELEM1)  -> mavlink_channel_t 1
#   SERIAL2 = USART3 (TELEM2)  -> mavlink_channel_t 2
#
# STM32F427 DMA map (STM32F427xx.py:116-119):
#   USART2_TX = DMA1 Stream 6 Ch 4   (S6 CR = 0x400260A0, PAR = 0x400260A8)
#   USART3_TX = DMA1 Stream 3 Ch 4   (S3 CR = 0x40026058, PAR = 0x40026060)
#             or DMA1 Stream 4 Ch 7  (S4 CR = 0x40026070, PAR = 0x40026078)
#
# Prerequisites: PTY bridge running (socat), fastdyn probe-run --run-gdb.
#   script -q -f fastdyn_work/gdb_mavlink_route_diag.log \
#     -c 'gdb-multiarch -q -x gdbscripts/ardurover_mavlink_route_diag.gdb'

file virtuals/physics/flight_controllers/courbet/bin/ardurover_v462

python
import os, gdb

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

printf "\n[mav-route] MAVLink routing diagnostic loaded\n"
printf "[mav-route] SERIAL0=USB(chan0)  SERIAL1=USART2(chan1)  SERIAL2=USART3(chan2)\n"

python
import gdb, threading, time
from collections import Counter

state = {
    "tx_finalize":     0,
    "chan_hist":       Counter(),
    "chan_first_pcs":  [],       # list of (chan, pc, lr) for first N packets
    "usart2_dr":       0,
    "usart3_dr":       0,
    "otg_fifo0":       0,        # OTG_FS device FIFO0 access (USB TX)
    "s3_par_last":     None,
    "s3_par_writes":   0,
    "s3_cr_writes":    0,
    "s4_par_last":     None,
    "s4_par_writes":   0,
    "s6_par_last":     None,
    "s6_par_writes":   0,
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

class ChanProbe(gdb.Breakpoint):
    """Capture the mavlink_channel_t chan argument (r0) on packet finalize."""
    def __init__(self):
        super(ChanProbe, self).__init__(
            "_mav_finalize_message_chan_send",
            type=gdb.BP_BREAKPOINT,
            internal=True,
        )
    def stop(self):
        chan = _reg("r0") & 0xFFFF
        state["tx_finalize"] += 1
        state["chan_hist"][chan] += 1
        if len(state["chan_first_pcs"]) < 12:
            state["chan_first_pcs"].append((chan, _reg("pc"), _reg("lr")))
            gdb.write("[tx] finalize #%d chan=%d pc=%s lr=%s\n" %
                      (state["tx_finalize"], chan,
                       _hex(_reg("pc")), _hex(_reg("lr"))))
        return False

def _mk_dr_watch(addr, key, label):
    class _W(gdb.Breakpoint):
        def __init__(self):
            super(_W, self).__init__(
                "*(uint32_t*)0x%08x" % addr,
                type=gdb.BP_WATCHPOINT,
                internal=True,
            )
        def stop(self):
            state[key] += 1
            n = state[key]
            if n <= 3 or n % 400 == 0:
                gdb.write("[%s] direct write #%d pc=%s lr=%s\n" %
                          (label, n, _hex(_reg("pc")), _hex(_reg("lr"))))
            return False
    return _W()

def _mk_par_watch(addr, key_writes, key_last, label):
    class _W(gdb.Breakpoint):
        def __init__(self):
            super(_W, self).__init__(
                "*(uint32_t*)0x%08x" % addr,
                type=gdb.BP_WATCHPOINT,
                internal=True,
            )
        def stop(self):
            state[key_writes] += 1
            v = _read_u32(addr)
            state[key_last] = v
            n = state[key_writes]
            if n <= 3 or n % 400 == 0:
                gdb.write("[%s] PAR write #%d = %s pc=%s\n" %
                          (label, n, _hex(v), _hex(_reg("pc"))))
            return False
    return _W()

def _mk_cr_watch(addr, key, label):
    class _W(gdb.Breakpoint):
        def __init__(self):
            super(_W, self).__init__(
                "*(uint32_t*)0x%08x" % addr,
                type=gdb.BP_WATCHPOINT,
                internal=True,
            )
        def stop(self):
            state[key] += 1
            n = state[key]
            if n <= 3 or n % 400 == 0:
                cr = _read_u32(addr)
                gdb.write("[%s] CR write #%d = %s (CHSEL=%d EN=%d) pc=%s\n" %
                          (label, n, _hex(cr), (cr >> 25) & 0x7, cr & 0x1,
                           _hex(_reg("pc"))))
            return False
    return _W()

ChanProbe()

# Direct DR watches
_mk_dr_watch(0x40004404, "usart2_dr", "usart2-dr")
_mk_dr_watch(0x40004804, "usart3_dr", "usart3-dr")
# OTG_FS device FIFO0 (USB TX endpoint 0 FIFO)
_mk_dr_watch(0x50001000, "otg_fifo0", "otg-fifo0")

# DMA1 Stream 3 (USART3_TX candidate)
_mk_par_watch(0x40026060, "s3_par_writes", "s3_par_last", "dma1-s3")
_mk_cr_watch (0x40026058, "s3_cr_writes",                 "dma1-s3")

# DMA1 Stream 4 (USART3_TX alt / SPI2_TX)
_mk_par_watch(0x40026078, "s4_par_writes", "s4_par_last", "dma1-s4")

# DMA1 Stream 6 (USART2_TX per STM32F427xx.py; but we saw it grabbed by I2C1)
_mk_par_watch(0x400260A8, "s6_par_writes", "s6_par_last", "dma1-s6")

def _report():
    gdb.write("\n[summary]\n")
    gdb.write("  tx_finalize (MAVLink packets)  = %d\n" % state["tx_finalize"])
    if state["chan_hist"]:
        gdb.write("  chan distribution              = %s\n" %
                  ", ".join("chan%d=%d" % (c, n) for c, n in sorted(state["chan_hist"].items())))
    gdb.write("  direct DR writes:\n")
    gdb.write("    USART2 DR (0x40004404)       = %d\n" % state["usart2_dr"])
    gdb.write("    USART3 DR (0x40004804)       = %d\n" % state["usart3_dr"])
    gdb.write("    OTG_FS FIFO0 (0x50001000)    = %d\n" % state["otg_fifo0"])
    gdb.write("  DMA1 stream PAR programming:\n")
    gdb.write("    S3 (USART3_TX candidate) writes=%d last=%s\n" %
              (state["s3_par_writes"], _hex(state["s3_par_last"])))
    gdb.write("    S4 (USART3_TX/SPI2_TX)   writes=%d last=%s\n" %
              (state["s4_par_writes"], _hex(state["s4_par_last"])))
    gdb.write("    S6 (USART2_TX/I2C1_TX)   writes=%d last=%s\n" %
              (state["s6_par_writes"], _hex(state["s6_par_last"])))

    # Interpretation
    hist = state["chan_hist"]
    if not hist:
        gdb.write("[verdict] no MAVLink packets seen yet\n")
        return
    top_chan, top_count = hist.most_common(1)[0]
    if top_chan == 0:
        gdb.write("[verdict] MAVLink is targeting SERIAL0 = USB (OTG_FS). Bytes disappear into stubbed OTG models.\n")
        gdb.write("[next]    Either bring up an OTG_FS model with PTY forwarding, or ensure USART2/USART3 also send.\n")
    elif top_chan == 1:
        if state["usart2_dr"] > 0:
            gdb.write("[verdict] chan1=USART2 in use, CPU direct-DR path. Fix usart2.c PTY plumbing.\n")
        elif state["s6_par_last"] == 0x40004404:
            gdb.write("[verdict] chan1=USART2 in use, DMA1 S6 -> USART2_DR. Fix dma1.c stream generalisation.\n")
        else:
            gdb.write("[verdict] chan1=USART2 in use but bytes vanish before any UART/DMA endpoint. Deeper look needed.\n")
    elif top_chan == 2:
        if state["usart3_dr"] > 0:
            gdb.write("[verdict] chan2=USART3 in use, direct-DR path. Model usart3.c same as usart2.c fix.\n")
        elif state["s3_par_last"] == 0x40004804 or state["s4_par_last"] == 0x40004804:
            gdb.write("[verdict] chan2=USART3 in use, DMA path. Generalise dma1.c AND add usart3.c DMA cb.\n")
        else:
            gdb.write("[verdict] chan2=USART3 in use but bytes vanish. Deeper look needed.\n")
    else:
        gdb.write("[verdict] MAVLink targets chan=%d — unexpected on this board.\n" % top_chan)

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

printf "[mav-route] probes installed; continuing target\n"
continue
