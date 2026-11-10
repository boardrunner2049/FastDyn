"""Schema-and-event introspectors for RTOSes with no bespoke C walker yet.

These implementations deliberately use only stable scheduler entry points and
DWARF-derived layouts.  Native callbacks report scheduler/task lifecycle
events and, where a standard current-task global exists, its address.  They do
not hard-code a particular RTOS release's private task-list layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastdyn.binary.schema_gen import SchemaGenerator
from ..introspector_base import RTOSIntrospector


@dataclass(frozen=True)
class RTOSSchemaSpec:
    hooks: tuple[str, ...]
    resource_hooks: tuple[str, ...]
    structs: tuple[str, ...]
    symbols: tuple[str, ...]
    epilogue_hooks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ZephyrSwitchHook:
    """A verified Zephyr architecture context-switch callback contract."""

    symbols: tuple[str, ...]


# Zephyr has no scheduler-level function that is emitted by every architecture.
# This is intentionally an introspection-plugin table, not frontend knowledge.
# Empty tuples mean the architecture has no stable hook that we can promise.
ZEPHYR_ARCH_SWITCH_HOOKS: dict[str, ZephyrSwitchHook] = {
    "arm-cortex-m": ZephyrSwitchHook(("z_arm_pendsv",)),
    "riscv": ZephyrSwitchHook(("z_riscv_switch",)),
    "arm-cortex-ar": ZephyrSwitchHook(("z_arm_context_switch",)),
    "aarch64": ZephyrSwitchHook(("z_arm64_context_switch",)),
    "openrisc": ZephyrSwitchHook(("z_openrisc_switch",)),
    "sparc": ZephyrSwitchHook(("z_sparc_context_switch",)),
    "renesas-rx": ZephyrSwitchHook(("_z_rx_arch_switch",)),
    "x86-ia32": ZephyrSwitchHook(("arch_swap",)),
    "arc": ZephyrSwitchHook(("z_arc_switch", "_rirq_newthread_switch", "_firq_exit")),
    # MIPS, Xtensa, x86-64, and POSIX/native_sim deliberately have no entry:
    # their relevant transitions are internal, exception-return based, or not
    # a hardware CPU switch. The preprocessor reports this instead of guessing.
}


def zephyr_switch_hook(architecture: str, machine: str, cpu: str) -> ZephyrSwitchHook | None:
    """Select Zephyr's architecture-owned switch routine from public context."""
    arch = architecture.lower().replace("_", "-")
    target = f"{machine} {cpu}".lower().replace("_", "-")
    if arch in {"arm", "arm32"}:
        if "cortex-m" in target or machine.lower() in {"cortexm", "cortexm7"}:
            return ZEPHYR_ARCH_SWITCH_HOOKS["arm-cortex-m"]
        if "cortex-a" in target or "cortex-r" in target:
            return ZEPHYR_ARCH_SWITCH_HOOKS["arm-cortex-ar"]
        return None
    aliases = {
        "riscv32": "riscv", "riscv64": "riscv",
        "arm64": "aarch64",
        "or1k": "openrisc", "or1knd": "openrisc",
        "rx": "renesas-rx",
        "i386": "x86-ia32", "x86": "x86-ia32",
    }
    return ZEPHYR_ARCH_SWITCH_HOOKS.get(aliases.get(arch, arch))


class _SchemaEventIntrospector(RTOSIntrospector):
    spec: RTOSSchemaSpec

    def setup_hooks(self):
        for hook in self.spec.hooks:
            self.register_prologue_hook(hook)
        for hook in self.spec.resource_hooks:
            self.register_prologue_hook(hook)
        epilogue_hooks = self.spec.epilogue_hooks
        selector: Callable[[str, str, str], ZephyrSwitchHook | None] | None = getattr(
            type(self), "switch_hook_selector", None
        )
        if selector is not None:
            selected = selector(self.architecture, self.machine, self.cpu_model)
            epilogue_hooks = selected.symbols if selected is not None else ()
            if not epilogue_hooks:
                # Resource introspection remains useful, but task switching is
                # not fabricated for architectures without a stable hook.
                print(
                    "[hook] Zephyr has no verified architecture context-switch "
                    f"hook for arch={self.architecture!r}, machine={self.machine!r}, "
                    f"cpu={self.cpu_model!r}"
                )
        for hook in epilogue_hooks:
            self.register_epilogue_hook(hook)

        generator = (SchemaGenerator(self.binary, analysis=self.analysis)
                     if self.analysis is not None else SchemaGenerator(self.binary))
        exported_symbols = {
            name: self.symbols[name].address
            for name in self.spec.symbols
            if name in self.symbols
        }
        return generator.generate_schema(list(self.spec.structs), exported_symbols)


def _register(name: str, spec: RTOSSchemaSpec) -> type[_SchemaEventIntrospector]:
    """Create a named plugin class so the normal RTOS registry remains public."""
    class_name = name.replace("-", "").replace("/", "").replace(" ", "") + "Introspector"
    return type(class_name, (_SchemaEventIntrospector,), {"spec": spec}, rtos_name=name)


ZephyrIntrospector = _register(
    "Zephyr",
    RTOSSchemaSpec(
        hooks=(),
        resource_hooks=(
            "k_sem_init", "k_mutex_init", "k_timer_init",
            "k_sem_take", "k_sem_give", "k_mutex_lock", "k_mutex_unlock",
            "k_timer_start", "k_timer_stop",
            "k_msgq_init", "k_msgq_put", "k_msgq_get",
            "k_event_init", "k_event_post", "k_event_clear",
            # Zephyr applications normally enter these implementation symbols
            # through generated syscall wrappers. The public k_* functions
            # are consequently absent from many release builds. Treat both
            # entry points as equivalent operations; only linked symbols become
            # virtual instructions.
            "z_impl_k_sem_init", "z_impl_k_mutex_init", "z_impl_k_timer_init",
            "z_impl_k_sem_take", "z_impl_k_sem_give",
            "z_impl_k_mutex_lock", "z_impl_k_mutex_unlock",
            "z_impl_k_timer_start", "z_impl_k_timer_stop",
            "z_impl_k_msgq_init", "z_impl_k_msgq_put", "z_impl_k_msgq_get",
            "z_impl_k_event_init", "z_impl_k_event_post", "z_impl_k_event_clear",
        ),
        # _kernel begins with the per-CPU array on current uniprocessor
        # Zephyr.  Its _cpu.current field is the runtime current thread.
        structs=("z_kernel", "_cpu", "k_thread", "k_sem", "k_mutex", "k_timer", "k_msgq", "k_event"),
        symbols=("_kernel",),
    ),
)
ZephyrIntrospector.switch_hook_selector = zephyr_switch_hook
_register(
    "ThreadX",
    RTOSSchemaSpec(
        hooks=("_tx_thread_schedule", "_tx_thread_system_return", "_tx_thread_create", "tx_thread_create"),
        resource_hooks=(
            "_tx_semaphore_create", "_tx_mutex_create", "_tx_timer_create",
            "_tx_semaphore_get", "_tx_semaphore_put", "_tx_mutex_get", "_tx_mutex_put",
            "_tx_timer_activate", "_tx_timer_deactivate",
            "_tx_queue_create", "_tx_queue_send", "_tx_queue_receive",
            "_tx_event_flags_create", "_tx_event_flags_set", "_tx_event_flags_get",
        ),
        structs=("TX_THREAD", "TX_THREAD_STRUCT"),
        symbols=("_tx_thread_current_ptr",),
    ),
)
_register(
    "RT-Thread",
    RTOSSchemaSpec(
        hooks=("rt_schedule", "rt_thread_create", "rt_thread_self"),
        resource_hooks=(
            "rt_sem_init", "rt_mutex_init", "rt_timer_init",
            "rt_sem_take", "rt_sem_release", "rt_mutex_take", "rt_mutex_release",
            "rt_timer_start", "rt_timer_stop",
            "rt_mq_init", "rt_mq_send", "rt_mq_recv",
            "rt_event_init", "rt_event_send", "rt_event_recv",
        ),
        # Current RT-Thread releases intentionally expose rt_current_thread
        # as a macro for rt_thread_self().  On uniprocessor targets the
        # implementation stores that value in the static _cpu object, so
        # exporting _cpu and its DWARF layout is the stable way for the
        # native callback to obtain the current thread without inventing a
        # nonexistent global symbol.
        structs=("rt_cpu", "rt_thread", "rt_thread_information"),
        symbols=("_cpu",),
    ),
)
_register(
    "NuttX",
    RTOSSchemaSpec(
        # This is NuttX's scheduler-to-architecture context-switch boundary.
        # Its AAPCS arguments are ``from`` (R0) and ``to`` (R1), so a
        # prologue hook records every actual selected-task transition.  Queue
        # mutation helpers are deliberately not scheduler events: they may
        # run without switching the processor to a different task.
        hooks=("nxsched_switch_context",),
        resource_hooks=(
            "nxsem_init", "nxmutex_init", "wd_create",
            "nxsem_wait", "nxsem_post", "nxmutex_lock", "nxmutex_unlock",
            "wd_start", "wd_cancel",
        ),
        structs=("tcb_s", "dq_queue_s"),
        symbols=("g_readytorun", "g_idletcb"),
    ),
)
