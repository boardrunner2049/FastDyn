/** Fuzzing hooks a device model may call when FastDyn runs with the fuzzer.
 *
 * They are defined in libfastdyn.so (builds with the fuzzer enabled), so a
 * model that uses them links against it (`fastdyn build-model --pkg
 * fastdyn`, or `pkg-config --libs fastdyn`). Models that only emulate
 * hardware do not need this header.
 */
#ifndef FASTDYN_FUZZ_HOOKS_H
#define FASTDYN_FUZZ_HOOKS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* --- The current fuzz input --- */

/** Copy up to `len` bytes of the current fuzz message into `buf`; returns
 * the number copied. */
size_t fuzz_get_data(char *buf, size_t len);

/** Take the current fuzz message exactly once, keeping its true size. The
 * caller owns *data and must free() it. Returns false if there is none. */
bool fuzz_take_input(uint8_t **data, size_t *len);

/** Send `len` bytes back to the fuzzer as the target's response. */
void fuzz_set_data(char *buf, size_t len);

/** Ask for the next message from a model callback (also from an IRQ-context
 * completion hook). The exchange with the fuzzer happens at thread level,
 * and execution resumes at `resume_pc`. */
void fuzz_request_sync(uint32_t resume_pc);

/* --- Guest state --- */

uint32_t fuzz_get_register(int reg);
void fuzz_set_register(uint32_t value, int reg);
int fuzz_write_memory(unsigned long long addr, uint8_t *mem_buf, int len);
int fuzz_read_memory(unsigned long long addr, uint8_t *mem_buf, int len);

/** Restore the fuzzing snapshot (base registers and its selected ranges). */
bool fuzz_restore_snapshot(void);

/* --- Callbacks (one each; a later call replaces the earlier one) --- */

typedef void (*fuzz_callback_t)(void);

/** After a sync with the fuzzer (see fuzz_request_sync). */
void fuzz_register_callback(fuzz_callback_t cb);

/** After a new fuzz message becomes active. */
void fuzz_register_input_callback(fuzz_callback_t cb);

/** After the snapshot is restored for a new test case. */
void fuzz_register_restore(fuzz_callback_t cb);

#endif /* FASTDYN_FUZZ_HOOKS_H */
