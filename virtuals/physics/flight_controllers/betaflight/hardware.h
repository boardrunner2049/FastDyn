#ifndef FASTDYN_BETAFLIGHT_HARDWARE_H
#define FASTDYN_BETAFLIGHT_HARDWARE_H
#include <stddef.h>
#include <stdint.h>
int betaflight_hardware_init_virtuals(void);
size_t betaflight_hardware_gps_bytes(uint8_t *out, size_t capacity);
#endif
