"""Host-side (QEMU process) half of ``wp22_stress.py`` (runs inside ``gdb -nx -batch -x``).

WP22 integration stage, T-B3-26 (design §5.5-5.6, §9.6). Parameters come from
``WP22_GDB_PARAMS`` (JSON); the result is written as JSON to ``params["result"]``.

What it observes (no breakpoint here stops the inferior unless an action needs
control at that point):

* every variant entry ``C'.tc.ptr`` (hardware breakpoints, at most 4): hit
  count, split by *segment* (the segment number grows at every loader event
  below), and the guest iteration counter r4 (``env->regs[4]``; env is ``%rbp``
  inside generated code) of the first and last hit of each segment;
* the loader events, each with the loader/slot state read **before** the call
  and **after** it returns (``gdb.FinishBreakpoint``):
  ``tcg_supercache_mapping_changed`` (Q7, every vCPU TLB flush),
  ``tcg_supercache_runtime_reset`` (Q6, ``tb_flush``),
  ``tcg_supercache_v6_forbid`` (Q11), ``sc6_disarm_all_work`` (Q9),
  ``tb_invalidate_phys_range`` (debug writes / SMC; recorded only while some
  record of the extension table is live);
* the state is: per guarded slot, the live R struct, ``R->jmp_dest[n]``,
  ``R->jmp_target_addr[n]`` against the reset address ``tc.ptr +
  jmp_reset_offset[n]`` and whether the target is a variant entry; per variant,
  the built C' struct and both of its registered slots; per link,
  ``armed``/``disarmed_epoch``; ``forbidden``.

Actions (``params["actions"]``), each fired once at a variant entry
(``{"variant": id, "hit": k}`` = the k-th entry of that variant, 1-based):
``tlb_flush`` (``call tlb_flush(cpu)`` on the vCPU thread: what an MPU or
watchpoint write does, G12), ``plugin_reset`` (``qemu_plugin_reset(<FastDyn
id>, NULL)`` on the vCPU thread: queued into the exclusive context as
``plugin_flush_destroy``, Q11), ``hmp`` (HMP ``one-insn-per-tb on`` on the
``-monitor`` socket while the vCPU stays stopped, Q9; as in the WP22 unit
diag).
"""

import json
import os
import socket
import threading
import time

import gdb

P = json.loads(os.environ["WP22_GDB_PARAMS"])
CF_INVALID = 0x4000
EVENTS = []
FIRST_ENTRY = {}
MAX_EVENTS = 20000
STATE = {"segment": 0, "errors": [], "actions_done": [], "vcpu_thread": None,
         "hmp_sent": None, "pending": None, "state_events": 0}
MAX_STATE_EVENTS = 60

for c in ("set pagination off", "set confirm off", "set breakpoint pending on",
          "set print thread-events off",
          "handle SIGUSR1 nostop noprint pass", "handle SIGUSR2 nostop noprint pass",
          "handle SIGPIPE nostop noprint pass", "handle SIGALRM nostop noprint pass",
          "handle SIGINT nostop noprint pass"):
    gdb.execute(c)


def u(expr):
    return int(gdb.parse_and_eval(expr))


def tname():
    t = gdb.selected_thread()
    return (t.name or "") if t else ""


def ev(kind, **kw):
    if len(EVENTS) < MAX_EVENTS:
        EVENTS.append({"ev": kind, "seg": STATE["segment"], "thread": tname(),
                       "t": round(time.monotonic(), 4), **kw})


def buffer_base():
    return u("(unsigned long)supercache.buffer")


def tb_slots(addr):
    t = f"((TranslationBlock *){addr:#x})"
    tc = u(f"(unsigned long){t}->tc.ptr")
    out = []
    for k in (0, 1):
        ro = u(f"{t}->jmp_reset_offset[{k}]")
        if ro == 0xFFFF:
            out.append(None)
            continue
        tgt = u(f"{t}->jmp_target_addr[{k}]")
        out.append({"dest": u(f"{t}->jmp_dest[{k}]"), "target": tgt, "reset": tc + ro,
                    "at_reset": tgt == tc + ro})
    return {"tb": addr, "tc": tc, "invalid": bool(u(f"{t}->cflags") & CF_INVALID),
            "jmp_list_head": u(f"{t}->jmp_list_head"), "slots": out}


def sc6_state():
    """Loader + slot state (see the module docstring); integers, hex in JSON later."""
    if not u("sc6.loaded"):
        return {"loaded": False}
    base = buffer_base()
    nv, ns, nl = u("sc6.n_variants"), u("sc6.n_slots"), u("sc6.n_links")
    vtc = {}
    variants = []
    for j in range(nv):
        e = f"sc6.variants[{j}]"
        tc = base + u(f"{e}.e.tc_offset")
        vtc[tc] = j
        built = u(f"(unsigned long){e}.built")
        gen = u(f"(unsigned long)sc6.live[{e}.general]")
        d = {"id": j, "tc": tc, "built": built, "general_live": gen}
        if built:
            d["state"] = tb_slots(built)
        if gen:
            g = tb_slots(gen)
            d["general_invalid"] = g["invalid"]
            d["general_jmp_list_head"] = g["jmp_list_head"]
        variants.append(d)
    slots = []
    for i in range(ns):
        e = f"sc6.slots[{i}]"
        n = u(f"{e}.e.slot_n")
        R = u(f"(unsigned long)sc6.live[{e}.src]")
        d = {"i": i, "n": n, "kind": u(f"{e}.e.kind"), "vc": u(f"{e}.e.target_vaddr"), "R": R}
        if R:
            st = tb_slots(R)
            s = st["slots"][n]
            d.update(R_invalid=st["invalid"], dest=s["dest"] if s else None,
                     target=s["target"] if s else None, reset=s["reset"] if s else None,
                     at_reset=s["at_reset"] if s else None,
                     target_variant=vtc.get(s["target"]) if s else None)
        slots.append(d)
    links = [{"armed": bool(u(f"sc6.links[{i}].armed")),
              "disarmed_epoch": bool(u(f"sc6.links[{i}].disarmed_epoch")),
              "slot": u(f"sc6.links[{i}].slot"), "variant": u(f"sc6.links[{i}].variant")}
             for i in range(nl)]
    return {"loaded": True, "forbidden": bool(u("sc6.forbidden")),
            "forbid_reason": u("sc6.forbid_reason"), "armed_total": u("sc6.st.armed"),
            "variants": variants, "slots": slots, "links": links}


def any_live(st):
    # the loader's sc6_any_live(): a live slot source R or a built variant
    return st.get("loaded") and (any(s["R"] for s in st["slots"]) or
                                 any(v["built"] for v in st["variants"]))


def safe_state():
    try:
        return sc6_state()
    except gdb.error as e:
        STATE["errors"].append(f"state: {e}")
        return {"error": str(e)}


OUTSTANDING = set()   # (thread num, frame sp) of event calls whose AfterBP has not fired


def frame_key():
    t = gdb.selected_thread()
    return (t.num if t else None, u("(unsigned long)$sp"))


def summary(st):
    """Counts the driver derives the loader's stats from (the loader's definitions)."""
    if not st.get("loaded"):
        return {"loaded": bool(st.get("loaded")), "error": st.get("error")}
    vs = [v for v in st["variants"] if v.get("state") and not v["state"]["invalid"]]
    return {"loaded": True, "forbidden": st["forbidden"],
            "armed": sum(1 for lk in st["links"] if lk["armed"]),
            "any_live": bool(any(s["R"] for s in st["slots"]) or
                             any(v["built"] for v in st["variants"])),
            "live_slots": sum(1 for s in st["slots"] if s["R"] and not s.get("R_invalid")),
            "variant_slots": sum(1 for v in vs for sl in v["state"]["slots"] if sl),
            "linked_variant_slots": sum(1 for v in vs for sl in v["state"]["slots"]
                                        if sl and (sl["dest"] & ~1)),
            "slots_to_variant": sum(1 for s in st["slots"] if s.get("target_variant") is not None),
            "anything_live": bool(any(s["R"] for s in st["slots"]) or
                                  any(v["built"] or v["general_live"] for v in st["variants"]))}


def disarm_bad(st):
    """§5.6 after a disarm: every guarded slot of a live valid R reset and unlinked,
    every built valid variant's registered slots unlinked, no link armed."""
    bad = []
    if not st.get("loaded"):
        return ["not loaded"]
    for s in st["slots"]:
        if s["R"] and not s.get("R_invalid") and ((s.get("dest") or 0) & ~1 or
                                                  not s.get("at_reset")):
            bad.append(f"slot {s['i']} dest={s.get('dest')} at_reset={s.get('at_reset')}")
    for v in st["variants"]:
        vs = v.get("state")
        if vs and not vs["invalid"]:
            for k, sl in enumerate(vs["slots"]):
                if sl and (sl["dest"] & ~1):
                    bad.append(f"variant {v['id']} slot {k} linked dest={sl['dest']:#x}")
    n = sum(1 for lk in st["links"] if lk["armed"])
    if n:
        bad.append(f"{n} links armed")
    return bad


class AfterBP(gdb.FinishBreakpoint):
    def __init__(self, frame, rec, key, full):
        super().__init__(frame, internal=True)
        self.rec = rec
        self.key = key
        self.full = full

    def stop(self):
        OUTSTANDING.discard(self.key)
        st = safe_state()
        self.rec["a"] = summary(st)
        self.rec["a"]["disarm_bad"] = disarm_bad(st)[:4]
        if self.full:
            self.rec["after"] = st
        return False

    def out_of_scope(self):
        OUTSTANDING.discard(self.key)
        self.rec["after_error"] = "out_of_scope"


class EventBP(gdb.Breakpoint):
    """Before/after state of one loader event; bumps the segment counter. Every
    event gets compact summaries; the first MAX_STATE_EVENTS also full states."""

    def __init__(self, spec, only_live=False, extra=None):
        # "*func": exactly the entry address (gcc may split a function into
        # several breakpoint locations, e.g. sc6_disarm_all_work has two)
        super().__init__("*" + spec, internal=True)
        self.spec = spec
        self.only_live = only_live
        self.extra = extra
        self.hits = 0

    def stop(self):
        key = frame_key()
        if key in OUTSTANDING:
            # gdb re-reported the same call (same thread, same frame) before it
            # returned; one call is one event
            STATE["rereports"] = STATE.get("rereports", 0) + 1
            return False
        self.hits += 1
        st = safe_state()
        if self.only_live and not any_live(st):
            return False
        STATE["segment"] += 1
        kw = {}
        if self.extra:
            try:
                kw = self.extra()
            except gdb.error as e:
                kw = {"extra_error": str(e)}
        rec = {"ev": self.spec, "seg": STATE["segment"], "thread": tname(),
               "t": round(time.monotonic(), 4), "hit": self.hits, "b": summary(st), **kw}
        full = STATE["state_events"] < MAX_STATE_EVENTS
        if full:
            STATE["state_events"] += 1
            rec["before"] = st
        try:
            AfterBP(gdb.newest_frame(), rec, key, full)
            OUTSTANDING.add(key)
        except (gdb.error, ValueError) as e:
            rec["after_error"] = str(e)
        if len(EVENTS) < MAX_EVENTS:
            EVENTS.append(rec)
        else:
            STATE["events_dropped"] = STATE.get("events_dropped", 0) + 1
        return False


class VariantBP(gdb.Breakpoint):
    def __init__(self, v):
        super().__init__(f"*{v['tc']:#x}", type=gdb.BP_HARDWARE_BREAKPOINT, internal=True)
        self.v = v
        self.total = 0
        self.seg = {}

    def stop(self):
        self.total += 1
        try:
            r4 = u("*(unsigned int *)($rbp + 0x10)")
        except gdb.error:
            r4 = None
        s = self.seg.setdefault(str(STATE["segment"]), {"hits": 0, "first_r4": r4, "last_r4": r4})
        s["hits"] += 1
        s["last_r4"] = r4
        if STATE["vcpu_thread"] is None:
            STATE["vcpu_thread"] = tname()
        if self.total == 1:
            # the slot state at the first entry into this C' (who jumped here)
            FIRST_ENTRY[str(self.v["id"])] = safe_state()
        for a in P.get("actions", []):
            if a.get("done") or a.get("variant") != self.v["id"]:
                continue
            if a.get("hit") == self.total or (a.get("r4") is not None and a.get("r4") == r4):
                a["done"] = True
                STATE["pending"] = dict(a, at_r4=r4, at_hit=self.total)
                return True
        return False


EventBP("tcg_supercache_mapping_changed", only_live=True)
EventBP("tcg_supercache_runtime_reset")
EventBP("tcg_supercache_v6_forbid")
EventBP("sc6_disarm_all_work",
        extra=lambda: {"exclusive": u("cpu->exclusive_context_count != 0")})
EventBP("tb_invalidate_phys_range", only_live=True,
        extra=lambda: {"start": u("start"), "last": u("last")})
setter_bp = None


class SetterBP(gdb.Breakpoint):
    def __init__(self):
        super().__init__("tcg_supercache_v6_one_insn_per_tb", internal=True)
        self.hits = 0

    def stop(self):
        self.hits += 1
        ev("one_insn_per_tb_setter")
        return self.hits == 1 and STATE.get("hmp_wait", False)


setter_bp = SetterBP()


def send_hmp(cmd):
    time.sleep(0.3)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(P["monitor"])
        s.settimeout(5)
        try:
            s.recv(4096)
        except OSError:
            pass
        s.sendall(cmd.encode() + b"\n")
        time.sleep(0.5)
        s.close()
        STATE["hmp_sent"] = time.monotonic()
    except OSError as e:
        STATE["errors"].append(f"hmp: {e}")


def do_action(a):
    rec = {"action": a["do"], "variant": a.get("variant"), "at_hit": a.get("at_hit"),
           "at_r4": a.get("at_r4"), "thread": tname()}
    rec["before"] = safe_state()
    STATE["segment"] += 1
    if a["do"] == "tlb_flush":
        gdb.execute("call (void)tlb_flush(cpus_queue.tqh_first)")
        rec["after"] = safe_state()
    elif a["do"] == "plugin_reset":
        rec["plugin_id"] = u("plugin.ctxs.tqh_first->id")
        gdb.execute("call (void)qemu_plugin_reset(plugin.ctxs.tqh_first->id, 0)")
        rec["after_call"] = safe_state()
    elif a["do"] == "hmp":
        STATE["hmp_wait"] = True
        threading.Thread(target=send_hmp, args=("one-insn-per-tb on",), daemon=True).start()
        gdb.execute("thread 1")
        gdb.execute("set scheduler-locking on")
        gdb.execute("continue")            # main thread only, until the setter stops
        rec["setter_thread"] = tname()
        gdb.execute("finish")
        rec["after_setter"] = safe_state()
        gdb.execute("set scheduler-locking off")
        STATE["hmp_wait"] = False
    STATE["actions_done"].append(rec)


def main():
    gdb.execute("tbreak tcg_supercache_after_prologue")
    gdb.execute("run")
    STATE["at_load"] = safe_state()
    variants = [VariantBP(v) for v in P.get("variants", [])[:4]]
    STATE["variant_bps"] = variants
    gdb.execute("continue")
    while gdb.selected_inferior().pid:
        a = STATE["pending"]
        if a is not None:
            STATE["pending"] = None
            do_action(a)
        gdb.execute("continue")


try:
    main()
except gdb.error as e:
    STATE["errors"].append(str(e))
try:
    code = int(gdb.parse_and_eval("$_exitcode"))
except (gdb.error, TypeError, ValueError):
    code = None
res = {k: v for k, v in STATE.items() if k not in ("variant_bps", "pending")}
res["exit_code"] = code
res["events"] = EVENTS
res["variant_hits"] = {str(b.v["id"]): {"tc": b.v["tc"], "total": b.total, "segments": b.seg}
                       for b in STATE.get("variant_bps", [])}
res["setter_hits"] = setter_bp.hits
res["first_entry"] = FIRST_ENTRY
with open(P["result"], "w") as f:
    json.dump(res, f, indent=1)
