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


def residual_reason(action: ExpansionAction) -> str | None:
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

_KERNEL_SOURCES = {"tasks.c", "queue.c", "port.c", "portasm.c", "list.c", "timers.c",
                   "stream_buffer.c", "event_groups.c", "croutine.c", "mpu_wrappers.c",
                   "mpu_wrappers_v2.c", *(f"heap_{n}.c" for n in range(1, 6))}


class TaskBoundaries:
    """Classify code using ELF symbols, direct veneers and visible exception reachability.

    Ordinary calls into SHARED targets use QEMU's existing public transfer. A
    public descriptor is initialized lazily and cannot be assumed ready for
    an offline rel32 jump. PUBLIC also preserves scheduler and exception entry.
    """

    def __init__(self, image: GuestImage):
        self.image = image
        self._local_sources = {entry: PurePath(path).name.lower()
                               for entry, path in image.local_function_sources}
        self.names = defaultdict(set)
        for func in image.funcs:
            self.names[func.start].add(func.name.split(".", 1)[0])
        self._ranges = image.code_ranges()
        self._resolved = {}
        self._classes = {}
        self._exception_visible = set()
        pending = list(image.exception_entries)
        # A FromISR API may exist without an installed interrupt caller. Keep
        # its entry public, but only propagate IRQ visibility from actual
        # vector entries or named handlers, not from the API's spelling.
        pending += [pc for pc, names in self.names.items()
                    if any(_ISR.search(n) and not n.endswith("FromISR") for n in names)]
        seen = set()
        while pending:
            pc = pending.pop()
            if pc in seen:
                continue
            seen.add(pc)
            func = self._body(pc)
            if func is None or func.start in self._exception_visible:
                continue
            self._exception_visible.add(func.start)
            for lo, hi in self._ranges:
                lo, hi = max(lo, func.start), min(hi, func.end)
                if hi <= lo:
                    continue
                for insn in _md().disasm(image.read(lo, hi - lo) or b"", lo):
                    if insn.id in (A.ARM_INS_B, A.ARM_INS_BL, A.ARM_INS_BLX) and (
                            insn.operands and insn.operands[0].type == A.ARM_OP_IMM):
                        dst = insn.operands[0].imm & 0xfffffffe
                        if not func.start <= dst < func.end:
                            pending.append(dst)

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
                func = None
                break
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
