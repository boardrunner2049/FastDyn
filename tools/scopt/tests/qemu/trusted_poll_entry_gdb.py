"""QEMU-process half of ``trusted_poll_entry.py`` (runs inside ``gdb -nx -batch -x``).

Forces the IRQ/exit-request poll of a trusted region entry to fire after a
trusted->trusted chain entered it at ``+0`` (counters revision 4), and
observes the exit path.  Parameters come from ``TRUSTED_POLL_ENTRY_GDB_PARAMS``
(JSON, ``result``: where to write the result JSON).

1. ``chain_target``: a breakpoint on ``tcg_supercache_chain_target`` and a
   finish breakpoint on each call record ``from``, ``to`` and the returned
   address R.  A return that is neither 0 nor ``to->tc.ptr`` is a
   trusted->trusted chain: R must be ``to->tc.ptr - 15`` and the bytes at R
   the poll ``8b 5d f8 85 db 0f 8c``.  Up to four such entries A (with their
   destination T) get a hardware breakpoint, because the first chain
   installed may never be traversed again.
2. ``poll_entry``: the first hit of any A (only a +0 entry executes it):
   ``$pc == A``, then ``(int16)-1`` is written to ``icount_decr.u16.high`` at
   ``$rbp + (int8)A[2] + 2`` (env is ``%rbp`` in generated code), so the
   ``jl`` takes the poll exit with TB_EXIT_REQUESTED.  The other breakpoints
   are removed; a one-shot breakpoint on ``arm_cpu_synchronize_from_tb`` for
   ``tb == T`` and a one-shot hardware breakpoint at ``T->tc.ptr`` are armed.
3. ``synchronize``: when that ``synchronize_from_tb(T)`` returns,
   ``env->regs[15] == T->pc``.
4. ``reentry``: the next entry of T at ``tc.ptr`` (+15, through the
   dispatcher) sees ``*(uint32 *)($rbp + 0x3c) == T->pc``.

The hardware and ordinary breakpoints are added and deleted, and memory is
written, only from the main loop while the inferior is stopped.  The one
exception is the ``gdb.FinishBreakpoint`` that observes a function's return
(steps 1 and 3): it is created inside the ``stop()`` callback of the
breakpoint on that function's entry, the pattern ``wp22_stress_gdb.py`` also
uses.  The GDB manual advises against changing breakpoints from ``stop()``;
this works on the installed GDB 15.1, so recheck it after a GDB upgrade.
"""

import json
import os

import gdb

P = json.loads(os.environ["TRUSTED_POLL_ENTRY_GDB_PARAMS"])
POLL = bytes.fromhex("8b5df885db0f8c")
BODY = 15                    # SC7_TRUSTED_BODY_OFFSET
REGS15 = 0x3C                # offsetof(CPUARMState, regs[15])
MAX_ENTRIES = 4              # x86 debug registers
STATE = {"events": {}, "errors": [], "candidates": [], "chain_calls": 0,
         "order": [], "pending": [], "fired": None}

for command in ("set pagination off", "set confirm off", "set breakpoint pending on",
                "set print thread-events off", "set startup-with-shell off",
                "set disable-randomization on", "set debuginfod enabled off",
                "handle SIGUSR1 nostop noprint pass", "handle SIGUSR2 nostop noprint pass",
                "handle SIGPIPE nostop noprint pass", "handle SIGALRM nostop noprint pass",
                "handle SIGINT nostop noprint pass"):
    try:
        gdb.execute(command)
    except gdb.error:
        pass


def u(expr):
    return int(gdb.parse_and_eval(expr))


def mem(addr, size):
    return bytes(gdb.selected_inferior().read_memory(addr, size))


def tb_field(tb, field):
    return u(f"(unsigned long)((TranslationBlock *){tb:#x})->{field}")


def event(name, ok, **detail):
    if name in STATE["events"]:
        return
    STATE["events"][name] = {"ok": bool(ok), **detail}
    STATE["order"].append(name)
    if not ok:
        STATE["errors"].append(f"{name}: {detail}")


class ChainReturn(gdb.FinishBreakpoint):
    def __init__(self, frame, source, dest):
        super().__init__(frame, internal=True)
        self.source, self.dest = source, dest

    def stop(self):
        if STATE["fired"] is not None or len(STATE["candidates"]) >= MAX_ENTRIES:
            return False
        ret = u("$rax")
        tc = tb_field(self.dest, "tc.ptr")
        if ret in (0, tc) or any(c["entry"] == ret for c in STATE["candidates"]):
            return False
        prefix = mem(ret, len(POLL))
        row = {"from": self.source, "to": self.dest, "entry": ret, "to_tc": tc,
               "to_pc": tb_field(self.dest, "pc"), "prefix": prefix.hex()}
        ok = ret == tc - BODY and prefix == POLL
        if not STATE["candidates"]:
            event("chain_target", ok, **row)
        if ok:
            STATE["candidates"].append(row)
            STATE["pending"].append(("arm_entry", row))
            return True
        STATE["errors"].append(f"chain_target: unexpected address {row}")
        return False

    def out_of_scope(self):
        pass


class ChainTarget(gdb.Breakpoint):
    def __init__(self):
        super().__init__("*tcg_supercache_chain_target", internal=True)

    def stop(self):
        STATE["chain_calls"] += 1
        if STATE["fired"] is None and len(STATE["candidates"]) < MAX_ENTRIES:
            ChainReturn(gdb.newest_frame(), u("$rdi"), u("$rsi"))
        return False


class PollEntry(gdb.Breakpoint):
    def __init__(self, row):
        super().__init__(f"*{row['entry']:#x}", type=gdb.BP_HARDWARE_BREAKPOINT, internal=True)
        self.row = row

    def stop(self):
        if STATE["fired"] is not None:
            return False
        STATE["fired"] = self.row
        STATE["pending"].append(("fire", self.row))
        return True


class SyncDone(gdb.FinishBreakpoint):
    def __init__(self, frame, row, env):
        super().__init__(frame, internal=True)
        self.row, self.env = row, env

    def stop(self):
        pc = int.from_bytes(mem(self.env + REGS15, 4), "little")
        event("synchronize", pc == self.row["to_pc"] & 0xFFFFFFFF, regs15=pc,
              tb_pc=self.row["to_pc"], after=list(STATE["order"]))
        return False

    def out_of_scope(self):
        STATE["errors"].append("synchronize: out of scope")


class Sync(gdb.Breakpoint):
    def __init__(self, row, env):
        super().__init__("*arm_cpu_synchronize_from_tb", internal=True)
        self.row, self.env, self.done = row, env, False

    def stop(self):
        if self.done or u("$rsi") != self.row["to"]:
            return False
        self.done = True
        SyncDone(gdb.newest_frame(), self.row, self.env)
        return False


class Reentry(gdb.Breakpoint):
    def __init__(self, row):
        super().__init__(f"*{row['to_tc']:#x}", type=gdb.BP_HARDWARE_BREAKPOINT, internal=True)
        self.row, self.done = row, False

    def stop(self):
        if self.done:
            return False
        self.done = True
        pc = int.from_bytes(mem(u("$rbp") + REGS15, 4), "little")
        event("reentry", pc == self.row["to_pc"] & 0xFFFFFFFF and u("$pc") == self.row["to_tc"],
              regs15=pc, tb_pc=self.row["to_pc"], host_pc=u("$pc"),
              after=list(STATE["order"]))
        STATE["pending"].append(("done", self.row))
        return True


BREAKPOINTS = {"chain": None, "entries": [], "sync": None, "reentry": None}


def act(kind, row):
    if kind == "arm_entry":
        BREAKPOINTS["entries"].append(PollEntry(row))
        if len(STATE["candidates"]) >= MAX_ENTRIES and BREAKPOINTS["chain"] is not None:
            BREAKPOINTS["chain"].delete()
            BREAKPOINTS["chain"] = None
    elif kind == "fire":
        pc, env = u("$pc"), u("$rbp")
        disp = int.from_bytes(mem(row["entry"] + 2, 1), "little", signed=True)
        high = env + disp + 2
        ok = pc == row["entry"] and mem(pc, len(POLL)) == POLL
        if ok:
            gdb.selected_inferior().write_memory(high, (0xFFFF).to_bytes(2, "little"))
        event("poll_entry", ok, host_pc=pc, env=env, icount_decr_high=high,
              to=row["to"], to_pc=row["to_pc"])
        for bp in BREAKPOINTS["entries"]:
            bp.delete()
        BREAKPOINTS["entries"] = []
        if BREAKPOINTS["chain"] is not None:
            BREAKPOINTS["chain"].delete()
            BREAKPOINTS["chain"] = None
        if ok:
            BREAKPOINTS["sync"] = Sync(row, env)
            BREAKPOINTS["reentry"] = Reentry(row)
    elif kind == "done":
        for key in ("sync", "reentry"):
            if BREAKPOINTS[key] is not None:
                BREAKPOINTS[key].delete()
                BREAKPOINTS[key] = None


def main():
    BREAKPOINTS["chain"] = ChainTarget()
    gdb.execute("run")
    while gdb.selected_inferior().pid:
        while STATE["pending"]:
            act(*STATE["pending"].pop(0))
        gdb.execute("continue")


try:
    main()
except gdb.error as e:
    STATE["errors"].append(str(e))
try:
    code = int(gdb.parse_and_eval("$_exitcode"))
except (gdb.error, TypeError, ValueError):
    code = None
result = {"exit_code": code, "events": STATE["events"], "order": STATE["order"],
          "errors": STATE["errors"], "candidates": STATE["candidates"],
          "chain_calls": STATE["chain_calls"]}
with open(P["result"], "w") as f:
    json.dump(result, f, indent=1)
