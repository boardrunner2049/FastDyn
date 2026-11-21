#!/usr/bin/env python3
"""stock.py ELF INPUT [--icount] [--wall]: run a compute-firmware image on stock QEMU (mps2-an385).

No plugin and no patched QEMU.  The start of the guest window is one gdbstub
breakpoint (vComputeWindowStart), removed when it is hit; the end is found by
polling the PC until it is inside vComputeDone.  Prints one JSON object with a
calibration run and a timed run.  Environment: STOCK_QEMU, ARM_PREFIX.
"""
import json, os, socket, subprocess, sys, tempfile, time

NM = os.environ.get("ARM_PREFIX", "arm-none-eabi-") + "nm"

def symbols(elf):
    out = {}
    for line in subprocess.run([NM, elf], capture_output=True, text=True, check=True).stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            out[p[2]] = int(p[0], 16) & ~1
    return out

class Gdb:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX); self.s.connect(path); self.buf = b""
    def send(self, data):
        pkt = b"$" + data.encode() + b"#" + b"%02x" % (sum(data.encode()) & 0xff)
        self.s.sendall(pkt)
    def recv(self):
        while True:
            a = self.buf.find(b"$"); b = self.buf.find(b"#", a + 1) if a >= 0 else -1
            if a >= 0 and b >= 0 and len(self.buf) >= b + 3:
                body = self.buf[a + 1:b]; self.buf = self.buf[b + 3:]
                self.s.sendall(b"+"); return body.decode()
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
    def cmd(self, data):
        self.send(data); return self.recv()

def run(elf, inp, icount=False, qemu=None, timeout=120, expect_ns=None, window=True):
    """One run.  window=True: a breakpoint at the window start (removed when hit), then the end is
    found by polling the PC; the result is the guest window.  window=False: no breakpoint at all;
    the result is the time from process start to the end of the work.

    A breakpoint that stays inserted makes QEMU single-step every block on its guest page, so the
    end is never a breakpoint.  A poll stops the vCPU for a moment; polls are sparse until the
    expected end (expect_ns, from an earlier run) and 1 ms apart near it.
    """
    s = symbols(elf); qemu = qemu or os.environ.get("STOCK_QEMU", "/usr/bin/qemu-system-arm")
    tmp = tempfile.mkdtemp(prefix="stock-", dir="/dev/shm"); sock = os.path.join(tmp, "gdb.sock")
    args = [qemu, "-M", "mps2-an385", "-cpu", "cortex-m3", "-kernel", elf, "-display", "none", "-monitor", "none",
            "-serial", "none", "-device", f"loader,file={inp},addr=0x21000000,force-raw=on",
            "-S", "-gdb", f"unix:{sock},server=on,wait=off"]
    if icount:
        args += ["-icount", "shift=0,sleep=off,align=off"]
    done = s["vComputeDone"]; polls = 0
    t0 = time.monotonic_ns()
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while not os.path.exists(sock):
            if time.monotonic_ns() - t0 > 10e9:
                raise RuntimeError("gdb socket did not appear")
            time.sleep(0.0005)
        g = Gdb(sock); g.s.settimeout(timeout)
        def pc_now():
            r = g.cmd("g"); return int.from_bytes(bytes.fromhex(r[120:128]), "little") & ~1
        t_start = t0
        if window:
            ws = s["vComputeWindowStart"]
            assert g.cmd(f"Z1,{ws:x},2") == "OK"
            g.cmd("c"); t_start = time.monotonic_ns()
            assert pc_now() == ws, "did not stop at the window start"
            assert g.cmd(f"z1,{ws:x},2") == "OK"
        g.send("c")
        last = t_start
        while True:
            now = time.monotonic_ns()
            if now - t0 > timeout * 1e9:
                raise RuntimeError("timeout")
            # Sparse until 3 % before the expected end, then every 2 ms.
            left = (t_start + 0.97 * expect_ns - now) if expect_ns else 0
            time.sleep(max(0.002, min(0.25, left / 3e9)) if expect_ns else 0.005)
            g.s.sendall(b"\x03"); t_poll = time.monotonic_ns(); g.recv(); polls += 1
            if done <= pc_now() < done + 8:
                t_done, t_prev = t_poll, last
                break
            last = time.monotonic_ns(); g.send("c")
        a = s["xStats"]
        mem = bytes.fromhex(g.cmd(f"m{a:x},50"))
        stats = [int.from_bytes(mem[i:i + 4], "little") for i in range(0, len(mem), 4)]
        g.send("k")
    finally:
        p.kill(); p.wait()
        t_end = time.monotonic_ns()
        try:
            os.unlink(sock)
        except OSError:
            pass
        os.rmdir(tmp)
    # The work ended between the last two polls: report the midpoint.
    return {"ns": (t_done + t_prev) // 2 - t_start, "uncertainty_ns": (t_done - t_prev) // 2,
            "wall_ns": t_end - t0, "polls": polls,
            "stats": [f"0x{x:08x}" for x in stats]}

if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    r = run(a[0], a[1], "--icount" in sys.argv, window="--wall" not in sys.argv)
    r2 = run(a[0], a[1], "--icount" in sys.argv, window="--wall" not in sys.argv, expect_ns=r["ns"])
    print(json.dumps({"calibration": r, "timed": r2}))
