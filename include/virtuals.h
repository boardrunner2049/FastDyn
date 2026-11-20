#ifndef VIRTUALS_H
#define VIRTUALS_H

#include <qemu/qemu-plugin.h>
#include <stdint.h>
#include "common.h"
#include "config.h"

extern _Atomic int64_t * last_sim_time_ns;

/**
 * @brief Print the value of a CPU register.
 *
 * This virtual instruction prints the value of a specified CPU register
 * to the standard output. The register number is provided as a hexadecimal
 * string in the `udata` parameter.
 *
 * Example usage in virtuals.txt:
 *
 * - `0xDEADBEEF printreg 0x0      # Prints the value of register 0 (R0)`
 *
 * - `0xDEADBEEF printreg 0xF      # Prints the value of register 15 (PC)`
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to a string containing the register number in hex.
 */
void printreg(unsigned int cpu_index, void *udata);

/**
 * @brief Raise IRQ function
 *
 * This virtual instruction raises an interrupt request (IRQ) of a specific number.
 * The number is 0 indexed, so raising IRQ 0 will trigger the first interrupt in the vector table.
 * For example, interrupt number 28 should be raised using the number 44 (28 + 16 for ARM Cortex-M).
 *
 * Example usage in virtuals.txt:
 *
 * - `0xDEADBEEF raiseirq 44`
 */
void raiseirq(unsigned int cpu_index, void *udata);

/**
 * @brief Pulse IRQ function
 *
 * This virtual instruction pulses an interrupt request (IRQ) of a specific number.
 * The number is 0 indexed, so pulsing IRQ 0 will trigger the first interrupt in the vector table.
 * For example, interrupt number 28 should be pulsed using the number 44 (28 + 16 for ARM Cortex-M).
 *
 * Example usage in virtuals.txt:
 *
 * -  `0xDEADBEEF pulseirq 44`
 */
void pulseirq(unsigned int cpu_index, void *udata);

/**
 * @brief Raise a periodic IRQ at configurable intervals.
 *
 * This virtual instruction sets up a periodic timer to raise an interrupt request (IRQ)
 * of a specific number. The default period is 1 millisecond and can be overridden with
 * the plugin argument timer_irq_period_ns or with a second virtual-instruction argument.
 * The IRQ number is 0 indexed, so raising IRQ 0 will
 * trigger the first interrupt in the vector table. For example, interrupt number 28 should be raised using the number 44 (28 + 16 for ARM Cortex
 *
 * Example usage in virtuals.txt:
 *
 * - `0xDEADBEEF raise_periodic_irq 44`
 */
void raise_periodic_irq(unsigned int cpu_index, void *udata);

/**
 * @brief Update or read emulated memory based on a string command.
 *
 * This function parses a memory access command string provided in @p udata
 * and performs either a memory read or write through the QEMU plugin
 * interface.
 *
 * The command string must follow the format:
 * @code
 * <address>:<mode>:<length>:<byte0>,<byte1>,...,<byteN>
 * @endcode
 *
 * - <address> : Memory address in decimal or hex (e.g., 1024, 0x20000000).
 *
 * - <mode>    : Either 'r' (read) or 'w' (write).
 *
 * - <length>  : Number of bytes to read/write.
 *
 * - <byteX>   : Comma-separated list of byte values (decimal or hex).
 *                  The number of values must match <length>.
 *
 * Example usages:
 *
 * - Write 4 bytes at address 0x20001000:
 *   @code
 *   0x20001000:w:4:0x12,0x34,0x56,0x78
 *   @endcode
 *
 * - Read 3 bytes from address 0x20001000:
 *   @code
 *   0x20001000:r:3:0,0,0,0
 *   @endcode
 *   (dummy byte values are still required for read mode)
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to a string containing the memory access command.
 */
void updatemem(unsigned int cpu_index, void *udata);

/**
 * @brief Randomize the state of registers and memory addresses.
 *
 * This function takes a comma-separated list of addresses from the input string
 * provided in `udata`. It randomizes the values at these addresses, either in
 * registers (if the address is less than 100) or in memory (if the address is 100 or greater).
 * The program counter (PC) register (address 15) is excluded from randomization.
 *
 * Example usage:
 *
 * - Randomize registers R0, R1, and memory address 0x20001000:
 *  `0xDEADBEEF randstate 0,1,0x20001000`
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to a string containing comma-separated addresses.
 */
void randstate(unsigned int cpu_index, void *udata);

/**
 * @brief Dump the log buffer of a specified address list to a file.
 *
 * This function takes an input string in the format `<index>:<filename>`,
 * where `<index>` is the index of the address list to dump, and `<filename>`
 * is the name of the file to which the log buffer will be written.
 *
 * Example usage:
 *
 * - Dump the log buffer of address list 0 to "log_output.bin":
 *   `0xDEADBEEF dumplogger 0:log_output.bin`
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to a string containing the index and filename.
 */
void dumplogger(unsigned int cpu_index, void *udata);

/**
 * @brief Write a file's contents into emulated memory at a specified address.
 *
 * This function takes an input string in the format `<address>:<filename>`,
 * reads the contents of the specified file, and writes it into the emulated
 * memory at the given address.
 *
 * Example usage:
 *
 * - Write the contents of "input.bin" into memory starting at address 0x20001000:
 *   `0xDEADBEEF dyninst 0x20001000:input.bin`
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to a string containing the address and filename.
 */
void dyninst(unsigned int cpu_index, void *udata);

/**
 * @brief Load an ELF file into the emulated environment.
 *
 * This function uses the QEMU plugin interface to load an ELF file
 * specified by the `udata` parameter into the emulated environment.
 *
 * Example usage:
 *
 * - Load the ELF file "module.elf":
 *
 *   `0xDEADBEEF dyninst_lib module.elf`
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to a string containing the ELF filename.
 */
void dyninst_lib(unsigned int cpu_index, void *udata);

/**
 * @brief TODO: document usage
 */
void fastdyn_callback(unsigned int cpu_index, void *udata);

/**
 * @brief Example use of QEMU timer to call a function periodically.
 *
 * This function sets up a periodic timer that calls `my_timer_callback`
 * every 1 millisecond (1e6 nanoseconds). The callback function prints the
 * current value of the virtual timer.
 *
 * Example usage:
 *
 * - `0xDEADBEEF timer_start`
 *
 * You should not modify the PC in your callback function. Undefined behavior may occur.
 *
 * @param cpu_index Index of the CPU invoking the plugin (unused here).
 * @param udata     Pointer to user data (unused here).
 */
void timer_start(unsigned int cpu_index, void *udata);

/**
 * @brief TODO
 */
void start_budgeting(unsigned int cpu_index, void *udata);

/**
 * @brief Log a debug message with a timestamp.
 *
 * This function logs a debug message along with the CPU index and a timestamp
 * in seconds with microsecond precision. The message is provided in the
 * `udata` parameter.
 *
 * Example usage:
 *
 * - `0xDEADBEEF debug_log "This is a debug message"`
 *
 * @param cpu_index Index of the CPU invoking the plugin.
 * @param udata     Pointer to a string containing the debug message.
 */
void debug_log(unsigned int cpu_index, void *udata);

/**
 * @brief Capture a monotonic wall-clock timestamp with no I/O.
 *
 * Pairs with @ref benchmark_end. Records CLOCK_MONOTONIC into a plugin-static
 * variable; performs no printf/write to keep the timed region free of
 * measurement noise. Intended for microbenchmarking hot-path plugin features
 * (modifiers, virtuals, coverage) bracketed by two sentinel PCs in the guest.
 *
 * Example usage in virtuals.txt:
 *
 * - `0x080001ba benchmark_start`
 */
void benchmark_start(unsigned int cpu_index, void *udata);

/**
 * @brief Print elapsed wall-clock nanoseconds since @ref benchmark_start.
 *
 * Grabs a second CLOCK_MONOTONIC sample, computes the delta, and prints
 * `[BENCH][<tag>] elapsed_ns=<ns> elapsed_us=<us>` where `<tag>` is the
 * @p udata string (empty if omitted). All I/O happens after the second
 * sample, so it never contaminates the measured region.
 *
 * Example usage in virtuals.txt:
 *
 * - `0x080001c8 benchmark_end slice_modifier`
 */
void benchmark_end(unsigned int cpu_index, void *udata);

/**
 * @brief Minimal-work callback for microbenchmarking virtuals.
 *
 * Increments a plugin-static `volatile uint64_t` counter — one guaranteed
 * memory store per dispatch, no elision. Intended as the "cheapest useful
 * virtual" reference point (counterpart to a minimal modifier like
 * `patch="r0 0"`). The counter is reset by @ref benchmark_start and not
 * otherwise observed.
 *
 * Example usage in virtuals.txt:
 *
 * - `0x080001ac bench_tick`
 */
void bench_tick(unsigned int cpu_index, void *udata);

// Helper functions
unsigned char get_random_byte(void);
uint32_t get_random_word(void);
unsigned long long* parse_addresses(const char *input, size_t *count);
int parse_update_mem_arg(const char *input, MemAccess *out);
AddrFilePair parse_addr_file(const char *input);
void* read_file(const char *filename, size_t *length);
bool parse_file_entry(const char* line, FileEntry* entry);
void dump_log_buffer_to_file(const AddressList* list, const char *filename);

// Callback lookup function (moved from core.c)
cb_func_t lookup_callback(const char *name);
bool virtual_callback_is_observer(cb_func_t callback);

// External dependencies from core.c
extern AddressList addressLists[];
extern size_t listCount;
extern uint32_t qemu_get_register(int reg);
extern void qemu_set_register(uint32_t value, int reg);

int virtual_register(const char *name, cb_func_t func);
// Initialization function
int virtuals_init(int argc, char **argv);
bool virtual_fragment_registration_failed(void);
int virtual_run_post_device_init(void);

#endif // VIRTUALS_H
