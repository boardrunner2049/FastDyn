set pagination off
set confirm off
set print thread-events off
set style enabled off
set breakpoint pending on
set breakpoint auto-hw on

# ArduRover 4.6.2 CubeBlack — USART2/USART3 TX-mode diagnostic.
#
# Question: does the ChibiOS UART driver on this build use DMA-driven TX
# (CR3.DMAT=1) or interrupt-driven TX (CR1.TXEIE=1)? Answers what to fix
# in usart2.c / usart3.c to get MAVLink bytes flowing.
#
# STM32F4 USART register layout (offset from base):
#   0x00 SR    status
#   0x04 DR    data
#   0x08 BRR   baud rate
#   0x0C CR1   control 1     bit 7 = TXEIE   bit 5 = RXNEIE  bit 3 = TE  bit 2 = RE
#   0x10 CR2   control 2
#   0x14 CR3   control 3     bit 7 = DMAT    bit 6 = DMAR
#
# USART2 base = 0x40004400  -> CR1=0x4000440C  CR3=0x40004414
# USART3 base = 0x40004800  -> CR1=0x4000480C  CR3=0x40004814
#
# Prereqs: socat PTY bridge running, fastdyn probe-run --run-gdb started.
#   script -q -f fastdyn_work/gdb_usart_mode_diag.log \
#     -c 'gdb-multiarch -q -x gdbscripts/ardurover_usart_mode_diag.gdb'

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

printf "\n[usart-mode] USART2/USART3 TX-mode diagnostic loaded\n"
printf "[usart-mode] watching USART2 CR1, CR3 and USART3 CR1, CR3\n"

python
import gdb, threading, time

state = {
    "u2_cr1_writes": 0, "u2_cr1_last": 0,
    "u2_cr3_writes": 0, "u2_cr3_last": 0,
    "u3_cr1_writes": 0, "u3_cr1_last": 0,
    "u3_cr3_writes": 0, "u3_cr3_last": 0,
    "u2_txeie_seen": 0, "u2_dmat_seen": 0,
    "u3_txeie_seen": 0, "u3_dmat_seen": 0,
    "tx_finalize":   0,
}

def _reg(name):
    try:
        return int(gdb.parse_and_eval("$" + name)) & 0xFFFFFFFF
    except Exception:
        return 0

def _read_u32(addr):
    try:
        return int(gdb.parse_and_eval("*(uint32_t*)0x%08x" % addr)) & 0xFFFFFFFF
    except Exception:
        return 0

def _hex(v):
    return "0x%08x" % (v & 0xFFFFFFFF)

def _decode_cr1(v):
    return "UE=%d RE=%d TE=%d RXNEIE=%d TXEIE=%d" % (
        (v >> 13) & 1, (v >> 2) & 1, (v >> 3) & 1, (v >> 5) & 1, (v >> 7) & 1)

def _decode_cr3(v):
    return "DMAT=%d DMAR=%d" % ((v >> 7) & 1, (v >> 6) & 1)

def _mk_cr1_watch(addr, key_writes, key_last, key_flag, label):
    class _W(gdb.Breakpoint):
        def __init__(self):
            super(_W, self).__init__(
                "*(uint32_t*)0x%08x" % addr,
                type=gdb.BP_WATCHPOINT, internal=True)
        def stop(self):
            state[key_writes] += 1
            v = _read_u32(addr)
            state[key_last] = v
            if (v >> 7) & 1:
                state[key_flag] += 1
            n = state[key_writes]
            if n <= 3 or n % 300 == 0:
                gdb.write("[%s CR1] write #%d = %s [%s] pc=%s\n" %
                          (label, n, _hex(v), _decode_cr1(v), _hex(_reg("pc"))))
            return False
    return _W()

def _mk_cr3_watch(addr, key_writes, key_last, key_flag, label):
    class _W(gdb.Breakpoint):
        def __init__(self):
            super(_W, self).__init__(
                "*(uint32_t*)0x%08x" % addr,
                type=gdb.BP_WATCHPOINT, internal=True)
        def stop(self):
            state[key_writes] += 1
            v = _read_u32(addr)
            state[key_last] = v
            if (v >> 7) & 1:
                state[key_flag] += 1
            n = state[key_writes]
            if n <= 3 or n % 300 == 0:
                gdb.write("[%s CR3] write #%d = %s [%s] pc=%s\n" %
                          (label, n, _hex(v), _decode_cr3(v), _hex(_reg("pc"))))
            return False
    return _W()

class TxProbe(gdb.Breakpoint):
    def __init__(self):
        super(TxProbe, self).__init__(
            "_mav_finalize_message_chan_send",
            type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        state["tx_finalize"] += 1
        return False

_mk_cr1_watch(0x4000440C, "u2_cr1_writes", "u2_cr1_last", "u2_txeie_seen", "USART2")
_mk_cr3_watch(0x40004414, "u2_cr3_writes", "u2_cr3_last", "u2_dmat_seen",  "USART2")
_mk_cr1_watch(0x4000480C, "u3_cr1_writes", "u3_cr1_last", "u3_txeie_seen", "USART3")
_mk_cr3_watch(0x40004814, "u3_cr3_writes", "u3_cr3_last", "u3_dmat_seen",  "USART3")
TxProbe()

def _report():
    gdb.write("\n[summary]\n")
    gdb.write("  MAVLink packets finalized  = %d\n" % state["tx_finalize"])
    gdb.write("  USART2 CR1 writes=%d last=%s [%s]  TXEIE_seen=%d\n" %
              (state["u2_cr1_writes"], _hex(state["u2_cr1_last"]),
               _decode_cr1(state["u2_cr1_last"]), state["u2_txeie_seen"]))
    gdb.write("  USART2 CR3 writes=%d last=%s [%s]  DMAT_seen=%d\n" %
              (state["u2_cr3_writes"], _hex(state["u2_cr3_last"]),
               _decode_cr3(state["u2_cr3_last"]), state["u2_dmat_seen"]))
    gdb.write("  USART3 CR1 writes=%d last=%s [%s]  TXEIE_seen=%d\n" %
              (state["u3_cr1_writes"], _hex(state["u3_cr1_last"]),
               _decode_cr1(state["u3_cr1_last"]), state["u3_txeie_seen"]))
    gdb.write("  USART3 CR3 writes=%d last=%s [%s]  DMAT_seen=%d\n" %
              (state["u3_cr3_writes"], _hex(state["u3_cr3_last"]),
               _decode_cr3(state["u3_cr3_last"]), state["u3_dmat_seen"]))

    u2_dmat = state["u2_dmat_seen"] > 0
    u2_txeie = state["u2_txeie_seen"] > 0
    if u2_dmat and not u2_txeie:
        gdb.write("[verdict] USART2 = DMA-driven TX (CR3.DMAT=1). Fix path: extend dma1.c to route Stream 6 to USART2 when CHSEL=4, add usart2.c DMA callback.\n")
    elif u2_txeie and not u2_dmat:
        gdb.write("[verdict] USART2 = INTERRUPT-driven TX (CR1.TXEIE=1). Fix path: usart2.c must qemu_plugin_raise_irq(54, false) when TXE && TXEIE.\n")
    elif u2_dmat and u2_txeie:
        gdb.write("[verdict] USART2 = MIXED (DMAT + TXEIE both seen). Likely DMA for bulk, ISR for last-byte / TC.\n")
    else:
        gdb.write("[verdict] USART2 TX never enabled — neither DMAT nor TXEIE seen. UART not opened or not writing.\n")

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

printf "[usart-mode] probes installed; continuing target\n"
continue
