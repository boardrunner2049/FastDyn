set pagination off
set confirm off
set print thread-events off
set style enabled off
set breakpoint pending on
set breakpoint auto-hw on

# Does our qemu_plugin_raise_irq(54,false) from usart2.c actually cause the
# USART2 ISR to execute? Two candidate entry points per the map:
#   Vector54:0x8005303                (vector-table trampoline)
#   sd_lld_serve_interrupt:0x814c481  (ChibiOS ISR body)
#
# Also watches NVIC ISER1 (0xE000E104) to confirm the firmware set bit 6
# (which corresponds to external IRQ 38 = USART2). Bit 6 in ISER1 corresponds
# to IRQ (32+6)=38, i.e. USART2 enable.
#
# Prereqs: socat PTY bridge running, fastdyn probe-run --run-gdb started.
#   script -q -f fastdyn_work/gdb_usart2_isr_probe.log \
#     -c 'gdb-multiarch -q -x gdbscripts/ardurover_usart2_isr_probe.gdb'

file virtuals/physics/flight_controllers/courbet/bin/ardurover_v462
target remote localhost:1234

printf "\n[isr-probe] USART2 ISR dispatch diagnostic\n"

python
import gdb, threading, time

state = {
    "iser1_writes": 0, "iser1_last": 0, "usart2_enabled_seen": 0,
    "vector54_hits": 0,
    "sd_lld_hits":   0,
    "tx_finalize":   0,
}

def _hex(v): return "0x%08x" % (v & 0xFFFFFFFF)

def _read_u32(addr):
    try:
        return int(gdb.parse_and_eval("*(uint32_t*)0x%08x" % addr)) & 0xFFFFFFFF
    except Exception:
        return 0

class Iser1Watch(gdb.Breakpoint):
    def __init__(self):
        super().__init__("*(uint32_t*)0xE000E104", type=gdb.BP_WATCHPOINT, internal=True)
    def stop(self):
        state["iser1_writes"] += 1
        v = _read_u32(0xE000E104)
        state["iser1_last"] = v
        if v & (1 << 6):
            state["usart2_enabled_seen"] += 1
        n = state["iser1_writes"]
        if n <= 5 or n % 100 == 0:
            gdb.write("[iser1] write #%d = %s (bit6=USART2=%d)\n" %
                      (n, _hex(v), (v >> 6) & 1))
        return False

class Vector54Probe(gdb.Breakpoint):
    def __init__(self):
        super().__init__("*0x08005303", type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        state["vector54_hits"] += 1
        n = state["vector54_hits"]
        if n <= 5 or n % 500 == 0:
            gdb.write("[vector54] USART2 ISR entry hit #%d\n" % n)
        return False

class SdLldProbe(gdb.Breakpoint):
    def __init__(self):
        super().__init__("*0x0814c481", type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        state["sd_lld_hits"] += 1
        n = state["sd_lld_hits"]
        if n <= 5 or n % 500 == 0:
            gdb.write("[sd_lld_serve_interrupt] hit #%d\n" % n)
        return False

class TxProbe(gdb.Breakpoint):
    def __init__(self):
        super().__init__("_mav_finalize_message_chan_send",
                         type=gdb.BP_BREAKPOINT, internal=True)
    def stop(self):
        state["tx_finalize"] += 1
        return False

Iser1Watch()
Vector54Probe()
SdLldProbe()
TxProbe()

def _report():
    gdb.write("\n[summary]\n")
    gdb.write("  tx_finalize (MAVLink packets)     = %d\n" % state["tx_finalize"])
    gdb.write("  ISER1 writes                      = %d  last=%s  USART2-enable-seen=%d\n" %
              (state["iser1_writes"], _hex(state["iser1_last"]), state["usart2_enabled_seen"]))
    gdb.write("  Vector54 (USART2 ISR entry)       = %d\n" % state["vector54_hits"])
    gdb.write("  sd_lld_serve_interrupt            = %d\n" % state["sd_lld_hits"])

    if state["usart2_enabled_seen"] == 0:
        gdb.write("[verdict] Firmware never enabled USART2 IRQ (bit 6 of ISER1 never set). Something upstream blocking.\n")
    elif state["vector54_hits"] == 0:
        gdb.write("[verdict] NVIC ISER1 was written with bit 6, but Vector54 never entered. IRQ dispatch broken — NVIC stub is silently eating ISER writes so QEMU's core sees the IRQ pending but disabled. Fix: implement ISER/ICER in nvic.c.\n")
    elif state["sd_lld_hits"] == 0:
        gdb.write("[verdict] Vector54 entered but sd_lld_serve_interrupt not called. Trampoline routing issue.\n")
    elif state["tx_finalize"] > 0 and state["vector54_hits"] > 0:
        gdb.write("[verdict] ISR IS running. Bytes should be reaching DR — check /tmp/usart2_host, and if empty the PTY-forward path or state check in usart2_transmit_byte is off.\n")

class _Ticker(threading.Thread):
    def run(self):
        while True:
            time.sleep(10)
            try: gdb.post_event(_report)
            except: return

_Ticker(daemon=True).start()
end

printf "[isr-probe] probes installed; continuing\n"
continue
