#ifndef CORE_H
#define CORE_H

#include <qemu/qemu-plugin.h>
#include <qemu/fastdyn-fragments.h>
#include <stddef.h>
#include <stdint.h>
#include "common.h"

/**
 * @brief Get the current program counter (PC) value.
 *
 * @return uint64_t The current PC.
 */
uint64_t core_get_pc(void);
uint64_t core_get_sp(void);
uint64_t core_get_icount(void);

/**
 * @brief Wait for the tracer thread to catch up to the inline logger
 */
void core_wait_for_trace_drain(void);

/**
 * @brief Read from RAM at the specified address.
 *
 * @param address The memory address to read from.
 * @param size The number of bytes to read.
 * @param buffer The buffer to store the read data.
 * @return int 0 on success, -1 on failure.
 */
int core_read_ram(uintptr_t address, size_t size, void* buffer);

/**
 * @brief Write to RAM at the specified address.
 *
 * @param address The memory address to write to.
 * @param size The number of bytes to write.
 * @param buffer The buffer containing the data to write.
 * @return int 0 on success, -1 on failure.
 */
int core_write_ram(uintptr_t address, size_t size, const void* buffer);

/**
 * Resolve an artifact owned by the active FastDyn run.
 *
 * The frontend owns the run directory and writes artifacts below
 * ``run-artifacts``.  Native modules address one by a logical relative name
 * (for example ``"introspection/schema.txt"``), never by adding a QEMU
 * plugin argument.  Returns 0 on success and -1 when no safe run artifact
 * path is available.
 */
int core_get_run_artifact_path(const char *relative, char *out, size_t out_size);

// Wrapper for qemu's irq registration, allows multiple hooks
void core_register_irq_hook(void (*cb)(int), void (*cb_end)(int));

typedef void (*core_tb_trans_hook_t)(qemu_plugin_id_t id, struct qemu_plugin_tb *tb);

// Allow users to inspect/instrument translated basic blocks.
void core_register_tb_trans_hook(core_tb_trans_hook_t cb);

// Allow users to register a hook for when fastdyn exits
void core_register_exit_hook(void (*cb)(void));

/* Rules and updates constructed programmatically by virtual subsystems. */
bool core_register_virtual_rule(uint64_t address, cb_func_t func,
                                const char *args);
bool core_register_register_update(uint64_t address, int reg,
                                   uint64_t value);
bool core_register_instruction_fragment(uint64_t guest_pc,
                                        const uint8_t *code,
                                        size_t code_size,
                                        const FastDynCodeSpec *code_spec,
                                        uint64_t *out_hook_id);
void core_set_guest_target(const char *target_name);

/* Register a normal modifier whose generated update runs only while
 * ``enabled`` is non-zero. The gate is read at guest runtime, so it can be
 * changed after the target basic block has been translated. */
bool core_register_gated_modifier(uint64_t address, const char *patch,
                                  const volatile uint8_t *enabled);
#endif /* CORE_H */
