"""Shared RTOS marker data; callers retain their own detection threshold."""

RTOS_SIGNATURES = {
    "FreeRTOS": ({"pxCurrentTCB", "vTaskSwitchContext"},),
    "Zephyr": ({"_kernel", "z_swap"}, {"_kernel", "z_sched_yield"}),
    "ThreadX": ({"_tx_thread_current_ptr", "_tx_thread_create"},
                {"_tx_thread_current_ptr", "tx_thread_create"}),
    "RT-Thread": ({"rt_thread_self", "rt_thread_create"},
                  {"rt_current_thread", "rt_thread_create"}),
    "NuttX": ({"g_readytorun", "nx_start"},),
    "ChibiOS": ({"ch_system", "__port_switch"}, {"chSchReadyI"}),
}

RTOS_HINT_MARKERS = {
    "FreeRTOS": ("xTaskCreate", "vTaskStartScheduler", "pxCurrentTCB"),
    "Zephyr": ("z_arm_pendsv", "z_riscv_switch", "z_current"),
    "NuttX": ("nx_start", "nxsched_switch_context", "g_running_tasks"),
    "ChibiOS": ("chSysInit", "chThdCreate", "chThdGetSelfX"),
    "ThreadX": ("tx_kernel_enter", "_tx_thread_current_ptr"),
    "RT-Thread": ("rt_thread_startup", "rt_current_thread"),
}


def identify_rtos(symbols: object) -> str:
    """Strong signature policy used for instrumentation decisions."""
    symbol_set = set(symbols.keys()) if hasattr(symbols, "keys") else set(symbols)
    for rtos, alternatives in RTOS_SIGNATURES.items():
        if any(required.issubset(symbol_set) for required in alternatives):
            return rtos
    return "Unknown/Custom Baremetal"
