"""Guest-call expansion boundaries for FreeRTOS task regions."""

from collections import defaultdict
from collections.abc import Set
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
import re

from capstone import arm as A

from ..decode.guest_symbols import GuestImage
from ..decode.thumb_sem import _md
from .rtos_cfg import patched_entry


class ExpansionAction(str, Enum):
    INLINE = "inline"
    SHARED = "shared"
    PUBLIC = "public"


@dataclass(frozen=True, slots=True)
class ExpansionSite:
    kind: str  # root, nested call or static branch
    source_uid: str
    source_pc: int
    target_pc: int


def residual_reason(action: ExpansionAction | str) -> str | None:
    """The side-exit reason of an expansion decision.  A callback may also
    return a plain nonempty string, used verbatim as the residual reason
    (for example ``nested_not_profitable`` from the firmware profit model)."""
    if not isinstance(action, ExpansionAction) and isinstance(action, str) and action:
        return action
    if action is ExpansionAction.INLINE:
        return None
    if action is ExpansionAction.SHARED:
        return "rtos_shared_public_lookup"
    if action is ExpansionAction.PUBLIC:
        return "rtos_public_boundary"
    raise ValueError(f"unknown expansion action {action!r}")


# FreeRTOS API naming conventions. Internal scheduler routines reached by
# ordinary calls from these entry points execute in the public kernel body.
_KERNEL_API = re.compile(
    r"^(?:(?:MPU_|FreeRTOS_)?(?:x|v|u|e|ux|ul|us|uc|pc|pv|px)"
    r"(?:Queue|Semaphore|Task|Timer|EventGroup|StreamBuffer|MessageBuffer|CoRoutine|List|Port)[A-Z]"
    r"|port(?:YIELD|ENTER|EXIT))")
_SCHEDULER = {"vTaskSwitchContext", "xTaskIncrementTick", "vTaskStartScheduler",
              "xPortStartScheduler", "prvPortStartFirstTask", "prvIdleTask", "prvTimerTask",
              "prvSelectHighestPriorityTask", "prvAddCurrentTaskToDelayedList"}
_ISR = re.compile(r"(?:_Handler|IRQHandler|ISR|FromISR)$|^(?:xPortPendSVHandler|"
                  r"xPortSysTickHandler|vPortSVCHandler)$")

# The reset entry runs once in Thread mode before the scheduler starts. Its
# CMSIS spellings match the handler pattern but name no interrupt.
_RESET_NAMES = {"Reset_Handler", "reset_handler", "ResetISR", "Reset_IRQHandler"}

_KERNEL_SOURCES = {"tasks.c", "queue.c", "port.c", "portasm.c", "list.c", "timers.c",
                   "stream_buffer.c", "event_groups.c", "croutine.c", "mpu_wrappers.c",
                   "mpu_wrappers_v2.c", *(f"heap_{n}.c" for n in range(1, 6))}

#: Longest handler-to-function name path reported by :meth:`TaskBoundaries.explain`.
_VIA_MAX = 16


class TaskBoundaries:
    """Classify code using ELF symbols, direct veneers and visible exception reachability.

    Ordinary calls into SHARED targets use QEMU's existing public transfer. A
    public descriptor is initialized lazily and cannot be assumed ready for
    an offline rel32 jump. PUBLIC also preserves scheduler and exception entry.

    ``revision`` 1 is the original classification. Revision 2 (FirmLooper v3)
    does not seed the reset entry (vector word 1 or the ELF entry, or a reset
    handler name) as an interrupt, and follows BOLT entry patches outside the
    Thumb code ranges to the moved function.
    """

    def __init__(self, image: GuestImage, *, revision: int = 1):
        if revision not in (1, 2):
            raise ValueError(f"unknown RTOS analysis revision {revision!r}")
        self.image = image
        self.revision = revision
        self._local_sources = {entry: PurePath(path).name.lower()
                               for entry, path in image.local_function_sources}
        self.names = defaultdict(set)
        for func in image.funcs:
            self.names[func.start].add(func.name.split(".", 1)[0])
        self._ranges = image.code_ranges()
        self._resolved = {}
        self._patch_of = {}
        self._classes = {}
        self._exception_visible = set()
        # Function start -> start of the visible function whose branch made it
        # visible (None for a seed), for explain().
        self._visible_parent = {}
        #: Entry patches that _body followed (revision 2), for receipts.
        self.patched_entries_resolved = set()
        #: Vector word 1 and the ELF entry, but not a real exception vector.
        self.reset_entries = (frozenset(image.thumb_entries - image.exception_entries)
                              if revision >= 2 else frozenset())
        pending = [(pc, None) for pc in image.exception_entries]
        # A FromISR API may exist without an installed interrupt caller. Keep
        # its entry public, but only propagate IRQ visibility from actual
        # vector entries or named handlers, not from the API's spelling.
        pending += [(pc, None) for pc, names in self.names.items()
                    if pc not in self.reset_entries
                    and any(_ISR.search(n) and not n.endswith("FromISR")
                            and not (revision >= 2 and n in _RESET_NAMES) for n in names)]
        seen = set()
        while pending:
            pc, parent = pending.pop()
            if pc in seen:
                continue
            seen.add(pc)
            func = self._body(pc)
            if func is None or func.start in self._exception_visible:
                continue
            self._exception_visible.add(func.start)
            self._visible_parent[func.start] = parent
            for lo, hi in self._ranges:
                lo, hi = max(lo, func.start), min(hi, func.end)
                if hi <= lo:
                    continue
                for insn in _md().disasm(image.read(lo, hi - lo) or b"", lo):
                    if insn.id in (A.ARM_INS_B, A.ARM_INS_BL, A.ARM_INS_BLX) and (
                            insn.operands and insn.operands[0].type == A.ARM_OP_IMM):
                        dst = insn.operands[0].imm & 0xfffffffe
                        if not func.start <= dst < func.end:
                            pending.append((dst, func.start))

    def _body(self, pc):
        if pc in self._resolved:
            return self._resolved[pc]
        start, seen = pc, set()
        while pc not in seen:
            seen.add(pc)
            func = self.image.func_at(pc)
            if func and func.start != pc:
                break
            if not any(lo <= pc < hi for lo, hi in self._ranges):
                # Revision 2: a BOLT entry patch left outside the $t ranges.
                dst = patched_entry(self.image, pc, _md()) if self.revision >= 2 else None
                if dst is None:
                    func = None
                    break
                self.patched_entries_resolved.add(pc)
                self._patch_of.setdefault(start, pc)
                pc = dst
                continue
            raw = self.image.read(pc, 4) or self.image.read(pc, 2) or b""
            insn = next(_md().disasm(raw, pc, count=1), None)
            if not (insn and insn.id == A.ARM_INS_B and insn.cc in (A.ARM_CC_AL, A.ARM_CC_INVALID)
                    and insn.operands and insn.operands[0].type == A.ARM_OP_IMM):
                break
            dst = insn.operands[0].imm & 0xfffffffe
            if func and func.start <= dst < func.end:
                break
            pc = dst
        else:
            func = None
        self._resolved[start] = func
        return func

    def classify(self, pc: int) -> ExpansionAction:
        if pc in self._classes:
            return self._classes[pc]
        owner = self.image.func_at(pc)
        func = self._body(pc)
        names = self.names.get(owner.start, set()) if owner else set()
        if func:
            names = names | self.names[func.start]
        if (not func or func.start in self._exception_visible or names & _SCHEDULER
                or any(_ISR.search(n) for n in names)):
            result = ExpansionAction.PUBLIC
        elif (any(_KERNEL_API.match(n) for n in names)
              or any(f and self._local_sources.get(f.start) in _KERNEL_SOURCES for f in (owner, func))):
            result = ExpansionAction.SHARED
        else:
            result = ExpansionAction.INLINE
        self._classes[pc] = result
        return result

    def explain(self, pc: int) -> dict:
        """The classification of ``pc`` and the first rule that decided it (receipts).

        Rules, in :meth:`classify`'s order: unknown_owner, exception_visible,
        scheduler_name, isr_name, kernel_api_name, kernel_source, application.
        ``patched_entry`` names the BOLT entry patch that was followed, and
        ``via`` the handler-to-function path of an exception-visible function.
        """
        action = self.classify(pc)
        owner = self.image.func_at(pc)
        func = self._body(pc)
        names = self.names.get(owner.start, set()) if owner else set()
        if func:
            names = names | self.names[func.start]
        if not func:
            rule = "unknown_owner"
        elif func.start in self._exception_visible:
            rule = "exception_visible"
        elif names & _SCHEDULER:
            rule = "scheduler_name"
        elif any(_ISR.search(n) for n in names):
            rule = "isr_name"
        elif any(_KERNEL_API.match(n) for n in names):
            rule = "kernel_api_name"
        elif any(f and self._local_sources.get(f.start) in _KERNEL_SOURCES for f in (owner, func)):
            rule = "kernel_source"
        else:
            rule = "application"
        out = {"action": action.value, "rule": rule,
               "function": func.name if func else owner.name if owner else None}
        if pc in self._patch_of:
            out["patched_entry"] = hex(self._patch_of[pc])
        if rule == "exception_visible":
            out["via"] = self._via(func.start)
        return out

    def _via(self, start):
        path, seen = [], set()
        while start is not None and start not in seen:
            seen.add(start)
            func = self.image.func_at(start)
            path.append(func.name if func else hex(start))
            start = self._visible_parent.get(start)
        path.reverse()
        if len(path) > _VIA_MAX:
            half = _VIA_MAX // 2
            path = path[:half] + ["..."] + path[-(_VIA_MAX - half - 1):]
        return path

    def classify_task_source(self, pc: int, registered_entries: Set[int]) -> ExpansionAction:
        """A registered, file-local application worker can use an API-like name.

        Only decides if the worker may nominate its loop calls. The existing
        classification still applies to every destination and to ISR-visible code.
        """
        action = self.classify(pc)
        if action is not ExpansionAction.SHARED:
            return action
        owner = self.image.func_at(pc)
        if owner is None or owner.start not in registered_entries or self._body(owner.start) != owner:
            return action
        source = self._local_sources.get(owner.start, "")
        if source.endswith(".c") and source not in _KERNEL_SOURCES:
            return ExpansionAction.INLINE
        return action
