"""
Scripted replacement for hal_dev_uart. Runs inside the halucinator
container via `docker exec`.

Subscribes to the firmware's UART TX topic, publishes scripted input
on the RX topic, exits as soon as the success marker appears in the
accumulated TX buffer. Wall time printed to stderr.

No halucinator source changes. Uses the same IOServer + topics that
halucinator.external_devices.uart uses, so it's a drop-in for
benchmarking.
"""
import argparse
import re
import sys
import threading
import time

import zmq

# halucinator's src tree is on PYTHONPATH inside both container images,
# but add it defensively in case this runs from /tmp.
sys.path.insert(0, "/root/halucinator/src")

from halucinator.external_devices.ioserver import IOServer  # noqa: E402
import halucinator.hal_log as hal_log  # noqa: E402

# The peer connects to HALucinator's ipc:// sockets before the emulator has
# bound them (it must subscribe first, or the firmware's first bytes are
# dropped by the PUB side). libzmq retries a failed connect only every
# 100 ms by default; a fast backend can bind and print the whole prompt
# inside that window, and the prompt is lost. Retry every 2 ms instead.
# Must be set before connect(), so wrap socket creation.
_orig_socket = zmq.Context.socket


def _socket(self, socket_type, *args, **kwargs):
    sock = _orig_socket(self, socket_type, *args, **kwargs)
    sock.setsockopt(zmq.RECONNECT_IVL, 2)
    sock.setsockopt(zmq.RECONNECT_IVL_MAX, 2)
    return sock


zmq.Context.socket = _socket


class Peer:
    def __init__(self, io, uart_id, steps, success, timeout, newline, label):
        self.io = io
        self.uart_id = uart_id
        self.steps = list(steps)       # queue of (wait_regex, response)
        self.success = success
        self.timeout = timeout
        self.newline = newline
        self.label = label
        self.buf = ""
        self.got_output = threading.Event()
        self.done = threading.Event()
        self.elapsed = None
        io.register_topic("Peripheral.UARTPublisher.write", self._on_tx)

    def _send(self, data):
        if self.newline and not data.endswith("\n"):
            data = data + "\n"
        self.io.send_msg(
            "Peripheral.UARTPublisher.rx_data",
            {"id": self.uart_id, "chars": data},
        )

    def _on_tx(self, _io, msg):
        chars = msg["chars"].decode("latin-1")
        sys.stdout.write(chars)
        sys.stdout.flush()
        self.buf += chars
        if not self.got_output.is_set():
            self.got_output.set()

        # Fire next scripted step if its cue matched.
        if self.steps and self.steps[0][0] and re.search(self.steps[0][0], self.buf):
            _, response = self.steps.pop(0)
            self._send(response)
            self.buf = ""

        if self.success in self.buf:
            self.elapsed = time.perf_counter() - self._t0
            self.done.set()

    def run(self):
        self._t0 = time.perf_counter()

        # Empty-cue steps ("") fire once the firmware has produced any
        # output, or after 5s — whichever comes first.
        if self.steps and self.steps[0][0] == "":
            self.got_output.wait(timeout=5.0)
            while self.steps and self.steps[0][0] == "":
                _, response = self.steps.pop(0)
                self._send(response)
                time.sleep(0.02)  # space out in case of pure-echo firmware

        if self.done.wait(self.timeout):
            sys.stderr.write(
                "\n[BENCH] {} SUCCESS {:.4f}\n".format(self.label, self.elapsed)
            )
            return 0
        sys.stderr.write("\n[BENCH] {} TIMEOUT\n".format(self.label))
        return 2


def parse_steps(raw):
    steps = []
    for s in raw:
        wait, _, send = s.partition("|")
        steps.append((wait, send))
    return steps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rx-port", type=int, default=5556)
    ap.add_argument("--tx-port", type=int, default=5555)
    ap.add_argument("--id", type=int, default=0, dest="uart_id")
    ap.add_argument("--newline", action="store_true")
    ap.add_argument(
        "--step",
        action="append",
        default=[],
        metavar="WAIT|SEND",
        help='Prompt/response pair. Empty WAIT ("") = send after first output.',
    )
    ap.add_argument("--success", required=True)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    hal_log.setLogConfig()
    io = IOServer(args.rx_port, args.tx_port)
    peer = Peer(
        io,
        args.uart_id,
        parse_steps(args.step),
        args.success,
        args.timeout,
        args.newline,
        args.label,
    )
    io.start()
    time.sleep(0.3)  # zmq slow-joiner
    # Tell the runner we're subscribed and ready to receive. The runner
    # only launches the emulator after seeing this line, so the firmware's
    # first TX bytes don't get dropped on the floor.
    sys.stderr.write("[BENCH] READY\n")
    sys.stderr.flush()
    rc = peer.run()
    try:
        io.shutdown()
    except Exception:
        pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
