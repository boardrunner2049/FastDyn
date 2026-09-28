set pagination off
set confirm off
set print thread-events off
set style enabled off
set breakpoint pending on
set breakpoint auto-hw on

# ArduRover 4.6.2 CubeBlack MAVLink-on-USART2 diagnostic probe.
#
# Prerequisite: set up a PTY bridge BEFORE starting FastDyn, so the model's
# open("/tmp/usart2_pty") in api_pty_fd_gen() succeeds:
#
#   socat -d -d PTY,link=/tmp/usart2_pty,raw,echo=0 \
#                PTY,link=/tmp/usart2_host,raw,echo=0
#
# Then run QEMU/FastDyn with:
#   fastdyn probe-run -c configs/rover462.toml -o fastdyn_recent_run \
#     --rtos-introspect --rtos-introspect-mode events \
#     --rtos-introspection-max-events 16384 --run-gdb
#
# Then run this script from the FastDyn repository root:
#   script -q -f fastdyn_work/gdb_mavlink_uart2_probe.log \
#     -c 'gdb-multiarch -q -x gdbscripts/ardurover_mavlink_uart2_probe.gdb'
#
# Attach a MAVLink parser to the host side of the bridge in yet another shell:
#   mavproxy.py --master=/tmp/usart2_host --baudrate 57600 --console
# or for raw byte inspection:
#   minicom -D /tmp/usart2_host -b 57600 --displayhex

file virtuals/physics/flight_controllers/courbet/bin/ardurover_v462

python
import os
import gdb

def _add_directory(path, label):
    if not path:
        return
    if "\n" in path or "\r" in path:
        gdb.write("[input] %s ignored: value contains a newline\n" % label)
        return
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(path):
        gdb.write("[input] %s unavailable: %s\n" % (label, path))
        return
    escaped = path.replace("\\", "\\\\").replace(" ", "\\ ")
    gdb.execute("directory %s" % escaped, to_string=True)
    gdb.write("[input] %s: %s\n" % (label, path))

source_root = os.environ.get("ARDUPILOT_SOURCE_ROOT")
build_root = os.environ.get("ARDUPILOT_BUILD_ROOT")

_add_directory(source_root, "ArduPilot source")
_add_directory(build_root, "ArduPilot build root")
if build_root:
    _add_directory(os.path.join(build_root, "build", "CubeBlack"), "CubeBlack build")
end

target remote localhost:1234

printf "\n[uart-test] MAVLink-over-USART2 diagnostic probe loaded\n"
printf "[uart-test] target: ArduRover v4.6.2 CubeBlack on STM32F427\n"
printf "[uart-test] USART2 MMIO: 0x40004400 (DR at +0x04)\n"
printf "[uart-test] firmware-side PTY: /tmp/usart2_pty (created by socat)\n"
printf "[uart-test] host-side PTY:     /tmp/usart2_host\n"

python
import gdb

state = {
    "setup_uarts": 0,
    "backend_created": 0,
    "uart_begin": 0,
    "banner": 0,
    "tx_finalize": 0,
}

def _reg(name):
    try:
        return int(gdb.parse_and_eval("$" + name))
    except Exception:
        return 0

def _hex_reg(name):
    return "0x%08x" % (_reg(name) & 0xFFFFFFFF)

class OnceProbe(gdb.Breakpoint):
    """First hit prints once and then stays enabled (no perf cost — cheap symbols)."""
    def __init__(self, spec, label, key):
        super(OnceProbe, self).__init__(
            spec,
            type=gdb.BP_BREAKPOINT,
            internal=True,
        )
        self.label = label
        self.key = key

    def stop(self):
        state[self.key] += 1
        n = state[self.key]
        if n == 1:
            gdb.write("[first-hit] %s | pc=%s lr=%s\n" % (
                self.label, _hex_reg("pc"), _hex_reg("lr")))
        return False

class TxProbe(gdb.Breakpoint):
    """Counting probe on the MAVLink finalize path. Prints first 3 hits and every 200th."""
    def __init__(self, spec, label, key):
        super(TxProbe, self).__init__(
            spec,
            type=gdb.BP_BREAKPOINT,
            internal=True,
        )
        self.label = label
        self.key = key

    def stop(self):
        state[self.key] += 1
        n = state[self.key]
        if n <= 3 or n % 200 == 0:
            gdb.write("[tx] %s count=%d | pc=%s lr=%s\n" % (
                self.label, n, _hex_reg("pc"), _hex_reg("lr")))
        return False

# --- Probes ---
OnceProbe("_ZN3GCS11setup_uartsEv",
          "GCS::setup_uarts (which UARTs GCS binds to)", "setup_uarts")

OnceProbe("_ZN3GCS26create_gcs_mavlink_backendER22GCS_MAVLINK_ParametersRN6AP_HAL10UARTDriverE",
          "GCS::create_gcs_mavlink_backend (backend bound to a UART)", "backend_created")

OnceProbe("_ZN6AP_HAL10UARTDriver5beginEm",
          "AP_HAL::UARTDriver::begin (UART opened with baud)", "uart_begin")

OnceProbe("_ZN11GCS_MAVLINK11send_bannerEv",
          "GCS_MAVLINK::send_banner (GCS greeting)", "banner")

TxProbe("_mav_finalize_message_chan_send",
        "_mav_finalize_message_chan_send (packet leaving GCS)", "tx_finalize")

end

# Periodic summary — print state every 5 seconds so you can watch progress.
python
import threading, gdb

def _report():
    gdb.write("\n[summary] setup_uarts=%d  backend_created=%d  uart_begin=%d  banner=%d  tx_finalize=%d\n" % (
        state["setup_uarts"], state["backend_created"], state["uart_begin"],
        state["banner"], state["tx_finalize"]))
    if state["tx_finalize"] == 0 and state["banner"] > 0:
        gdb.write("[warn] send_banner hit but no _mav_finalize_message_chan_send — packets not leaving GCS\n")
    if state["tx_finalize"] > 0 and state["setup_uarts"] > 0:
        gdb.write("[ok] MAVLink send path is executing; check /tmp/usart2_host for bytes\n")

class _Ticker(threading.Thread):
    def run(self):
        import time
        while True:
            time.sleep(5)
            try:
                gdb.post_event(_report)
            except Exception:
                return

_Ticker(daemon=True).start()
end

printf "[uart-test] probes installed; continuing target\n"
continue
