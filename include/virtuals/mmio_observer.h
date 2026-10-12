/* Internal: delivering MMIO accesses to the observers registered with
 * virtual_register_mmio_observer() (<fastdyn_runtime.h>). */
#ifndef FASTDYN_VIRTUALS_MMIO_OBSERVER_H
#define FASTDYN_VIRTUALS_MMIO_OBSERVER_H

#include <stdbool.h>
#include <stdint.h>

#include <fastdyn_runtime.h>

extern int virtual_mmio_observer_count;
void virtual_dispatch_mmio(const VirtualMmioAccess *access);

/* Costs one load and branch per access while nobody observes. */
static inline void virtual_notify_mmio(uint64_t pc, uint64_t address, uint64_t value,
                                       unsigned size, bool is_write, bool handled,
                                       bool in_mmio_window) {
    if (virtual_mmio_observer_count) {
        VirtualMmioAccess access = {
            .pc = pc, .address = address, .value = value, .size = size,
            .is_write = is_write, .handled = handled, .in_mmio_window = in_mmio_window,
        };
        virtual_dispatch_mmio(&access);
    }
}

#endif /* FASTDYN_VIRTUALS_MMIO_OBSERVER_H */
