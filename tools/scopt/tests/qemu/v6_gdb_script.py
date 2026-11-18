"""gdb-side half of ``v6_gdb_diag.py`` (runs inside ``gdb -nx -batch -x``).

Parameters come from ``WP22_GDB_PARAMS`` (JSON); the result is written as JSON
to ``params["result"]``. Logging breakpoints never stop the inferior
(``stop()`` returns False) unless a scenario needs control at that point.
"""

import json
import os
import socket
import threading
import time

import gdb

P = json.loads(os.environ["WP22_GDB_PARAMS"])
SC = P["scenario"]
EVENTS = []
STATE = {"triggered": False, "flushed": False, "seen_variant": False, "tb_flush_called": False,
         "tlb_flush_called": False, "armed_at_flush": None, "armed_variant_ids_at_flush": [],
         "vcpu_thread_name": None, "hmp_sent": None, "errors": []}
MAX_EVENTS = 400

for c in ("set pagination off", "set confirm off", "set breakpoint pending on",
          "handle SIGUSR1 nostop noprint pass", "handle SIGUSR2 nostop noprint pass",
          "handle SIGPIPE nostop noprint pass", "handle SIGALRM nostop noprint pass"):
    gdb.execute(c)


def tname():
    t = gdb.selected_thread()
    return (t.name or "") if t else ""


def ev(bp, **kw):
    if len(EVENTS) < MAX_EVENTS:
        EVENTS.append({"bp": bp, "thread": tname(), "t": round(time.monotonic(), 4), **kw})


def links_state():
    n = int(gdb.parse_and_eval("sc6.n_links"))
    out = []
    for i in range(n):
        lk = gdb.parse_and_eval(f"sc6.links[{i}]")
        out.append({"armed": bool(lk["armed"]), "variant": int(lk["variant"])})
    return out


class VariantBP(gdb.Breakpoint):
    def __init__(self, v):
        super().__init__(f"*{v['tc']}", type=gdb.BP_HARDWARE_BREAKPOINT, internal=True)
        self.v = v
        self.total = 0
        self.after = 0

    def stop(self):
        self.total += 1
        if STATE["flushed"]:
            self.after += 1
        if not STATE["seen_variant"]:
            STATE["seen_variant"] = True
            ev("variant_entry", variant=self.v["id"], vaddr=self.v["vaddr"])
            STATE["vcpu_thread_name"] = tname()
            if SC in ("tlb", "hmp", "tbflush") and not STATE["triggered"]:
                STATE["triggered"] = True
                return True
        return False


class LogBP(gdb.Breakpoint):
    def __init__(self, spec, stop_once=False, extra=None):
        super().__init__(spec, internal=True)
        self.name = spec
        self.stop_once = stop_once
        self.extra = extra
        self.hits = 0

    def stop(self):
        self.hits += 1
        kw = {}
        if self.extra:
            try:
                kw = self.extra()
            except gdb.error as e:
                kw = {"error": str(e)}
        ev(self.name, **kw)
        if self.stop_once and self.hits == 1:
            return True
        return False


class CpuExecBP(gdb.Breakpoint):
    """tbflush: stop at the first cpu_exec entry after a variant ran."""

    def stop(self):
        return SC == "tbflush" and STATE.get("cpu_exit_called") and not STATE["tb_flush_called"]


variants = []
LogBP("tb_unlink_jump")
LogBP("sc6_disarm_all_work",
      extra=lambda: {"exclusive": int(gdb.parse_and_eval("cpu->exclusive_context_count != 0"))})
setter = LogBP("tcg_supercache_v6_one_insn_per_tb", stop_once=(SC == "hmp"))
LogBP("tcg_supercache_v6_forbid")
cpu_exec_bp = CpuExecBP("cpu_exec", internal=True)


def send_hmp():
    time.sleep(0.3)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(P["monitor"])
        s.settimeout(5)
        try:
            s.recv(4096)          # banner
        except OSError:
            pass
        s.sendall(b"one-insn-per-tb on\n")
        time.sleep(0.5)
        s.close()
        STATE["hmp_sent"] = time.monotonic()
    except OSError as e:
        STATE["errors"].append(f"hmp: {e}")


def main():
    # hardware breakpoints need a live target: stop once after the code load
    gdb.execute("tbreak tcg_supercache_after_prologue")
    gdb.execute("run")
    variants.extend(VariantBP(v) for v in P["variants"])
    gdb.execute("continue")
    while gdb.selected_inferior().pid:
        if SC == "tlb" and STATE["triggered"] and not STATE["tlb_flush_called"]:
            ls = links_state()
            STATE["armed_at_flush"] = sum(1 for l in ls if l["armed"])
            STATE["armed_variant_ids_at_flush"] = [str(l["variant"]) for l in ls if l["armed"]]
            gdb.execute("call (void)tlb_flush(cpus_queue.tqh_first)")
            STATE["tlb_flush_called"] = True
            STATE["flushed"] = True
            ev("tlb_flush_called", armed=STATE["armed_at_flush"])
        elif SC == "tbflush" and STATE["triggered"] and not STATE.get("cpu_exit_called"):
            # leave the TB loop at C''s entry check (TB_EXIT_REQUESTED), like a kick
            gdb.execute("call (void)cpu_exit(cpus_queue.tqh_first)")
            STATE["cpu_exit_called"] = True
            ev("cpu_exit_called")
        elif SC == "tbflush" and STATE.get("cpu_exit_called") and not STATE["tb_flush_called"]:
            ls = links_state()
            ev("tb_flush_call", armed=sum(1 for l in ls if l["armed"]))
            gdb.execute("call (void)tb_flush(cpu)")
            STATE["tb_flush_called"] = True
        elif SC == "hmp" and STATE["triggered"] and STATE["hmp_sent"] is None \
                and setter.hits == 0:
            ls = links_state()
            ev("hmp_trigger", armed=sum(1 for l in ls if l["armed"]))
            threading.Thread(target=send_hmp, daemon=True).start()
            gdb.execute("thread 1")
            gdb.execute("set scheduler-locking on")
        elif SC == "hmp" and setter.hits == 1 and not STATE.get("finished_setter"):
            gdb.execute("finish")
            STATE["finished_setter"] = True
            gdb.execute("set scheduler-locking off")
        gdb.execute("continue")


try:
    main()
except gdb.error as e:
    STATE["errors"].append(str(e))
try:
    code = int(gdb.parse_and_eval("$_exitcode"))
except (gdb.error, TypeError, ValueError):
    code = None
res = dict(STATE)
res["exit_code"] = code
res["events"] = EVENTS
res["variant_hits"] = {str(v.v["id"]): {"vaddr": v.v["vaddr"], "r": v.v["r"], "total": v.total,
                                        "after_flush": v.after} for v in variants}
with open(P["result"], "w") as f:
    json.dump(res, f, indent=1)
