"""Guest-call expansion boundaries for FreeRTOS task regions."""

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
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
_KERNEL_API = re.compile(r"^(?:[xvpuels]+(?:Queue|Semaphore|Task|Timer|EventGroup|"
                         r"StreamBuffer|MessageBuffer|CoRoutine|List|Port)[A-Z]"
                         r"|port(?:YIELD|ENTER|EXIT)|pvPortMalloc|vPortFree|FreeRTOS_|MPU_)")
_SCHEDULER = {"vTaskSwitchContext", "xTaskIncrementTick", "vTaskStartScheduler",
              "xPortStartScheduler", "prvPortStartFirstTask", "prvIdleTask", "prvTimerTask",
              "prvSelectHighestPriorityTask", "prvAddCurrentTaskToDelayedList"}
_ISR = re.compile(r"(?:_Handler|IRQHandler|ISR|FromISR)$|^(?:xPortPendSVHandler|"
                  r"xPortSysTickHandler|vPortSVCHandler)$")


class TaskBoundaries:
    """Classify code using ELF symbols, direct veneers and visible exception reachability.

    Ordinary calls into SHARED targets use QEMU's existing public transfer. A
    public descriptor is initialized lazily and cannot be assumed ready for
    an offline rel32 jump. PUBLIC also preserves scheduler and exception entry.
    """

    def __init__(self, image: GuestImage):
        self.image = image
        self.names = defaultdict(set)
        for func in image.funcs:
            self.names[func.start].add(func.name.split(".", 1)[0])
        self._ranges = image.code_ranges()
        self._resolved = {}
        self._classes = {}
        self._exception_visible = set()
        pending = list(image.exception_entries)
        pending += [pc for pc, names in self.names.items() if any(_ISR.search(n) for n in names)]
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
        elif any(_KERNEL_API.match(n) for n in names):
            result = ExpansionAction.SHARED
        else:
            result = ExpansionAction.INLINE
        self._classes[pc] = result
        return result
