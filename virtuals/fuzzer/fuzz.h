#ifndef FUZZ_H
#define FUZZ_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <config.h>

#define MAP_SIZE 65536 // should match AFL

typedef struct {
    const uint8_t *data;
    size_t len;
    bool restore;
} fuzz_backend_msg_t;

// gets fuzzed data into buf of max size len, returns copied size
size_t fuzz_get_data(char* buf, size_t len);

/* Take the current fuzz message exactly once. The caller owns *data and must
 * free it. Unlike fuzz_get_data(), this preserves the message's true size. */
bool fuzz_take_input(uint8_t **data, size_t *len);

// sets data that should be returned to fuzzer
void fuzz_set_data(char* buf, size_t len);

// initializes whatever backend is used
bool fuzz_backend_init(void);
bool fuzz_backend_next(fuzz_backend_msg_t *msg);
void fuzz_backend_report_assert(bool fatal);
void fuzz_backend_set_data(const uint8_t *buf, size_t len);
void fuzz_backend_restore_complete(void);

/* Request a message handoff from a model callback.  Models may call this from
 * an IRQ-context completion hook; the registered thread-level virtual below
 * performs the blocking backend exchange and resumes at ``resume_pc``. */
void fuzz_request_sync(uint32_t resume_pc);

uint32_t fuzz_get_register(int reg);
void fuzz_set_register(uint32_t value, int reg);

int fuzz_write_memory(unsigned long long addr, uint8_t *mem_buf, int len);
int fuzz_read_memory(unsigned long long addr, uint8_t *mem_buf, int len);

typedef void (*fuzz_callback_t)(void);

// Registers a model callback that runs after a new fuzz message becomes active.
void fuzz_register_input_callback(fuzz_callback_t cb);

// Restores the fuzzing snapshot policy (base registers and its selected ranges).
bool fuzz_restore_snapshot(void);

#endif
