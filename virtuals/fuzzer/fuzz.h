#ifndef FUZZ_H
#define FUZZ_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <config.h>

/* The hooks device models may call (fuzz_get_data, fuzz_request_sync, ...). */
#include <fastdyn/fuzz_hooks.h>

#define MAP_SIZE 65536 // should match AFL

typedef struct {
    const uint8_t *data;
    size_t len;
    bool restore;
} fuzz_backend_msg_t;

// initializes whatever backend is used
bool fuzz_backend_init(void);
bool fuzz_backend_next(fuzz_backend_msg_t *msg);
void fuzz_backend_report_assert(bool fatal);
void fuzz_backend_set_data(const uint8_t *buf, size_t len);
void fuzz_backend_restore_complete(void);

#endif
