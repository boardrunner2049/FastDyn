#ifndef FASTDYN_MMIO_OPT_H
#define FASTDYN_MMIO_OPT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <fastdyn/device_model.h>

typedef struct FdResolvedMmioOperation {
    const char *device_name;
    const void *state;
    uint32_t state_size;
    const FdMmioOperation *operation;
} FdResolvedMmioOperation;

bool elder_resolve_mmio_operation(uint64_t address,
                                  FdResolvedMmioOperation *resolved);

#endif
