#pragma once

/*
 * imu_sample.h — LEGACY chip-LSB IMU sample struct.
 *
 * This header predates the canonical SI-unit schema in canonical.h. It is
 * retained for the boot-safety fallback path in each SPI slave model: when
 * no VIO channel is attached (no `data_source` in TOML, or the writer has
 * never published), the model calls boardrunner_imu_get_sample() and stores
 * the returned LSB values directly into its register frame. This guarantees
 * bit-exact identical boot behaviour compared to the pre-VIO model code.
 *
 * The new SI-based path is:
 *   #include <boardrunner/channel.h>          -- api_channel_* + BR_IMU_READ
 *   #include <boardrunner/packers/invensense.h> -- SI → BE16 packer
 *
 * New model code should prefer the SI path and use this header only for
 * the fallback branch.
 */

#include <stdint.h>

typedef struct {
    int16_t ax_lsb;
    int16_t ay_lsb;
    int16_t az_lsb;
    int16_t gx_lsb;
    int16_t gy_lsb;
    int16_t gz_lsb;
    int16_t temp_lsb;
} BoardrunnerImuSampleLSB;

/*
 * Returns a raw sensor-frame sample in chip LSB units. Device models are
 * responsible for packing this into their register and FIFO layouts.
 *
 * Runtime overrides (retained for existing tooling):
 *   BOARDRUNNER_IMU_AX, BOARDRUNNER_IMU_AY, ...
 *   BOARDRUNNER_IMU_<DEVICE>_AX, BOARDRUNNER_IMU_<DEVICE>_AY, ...
 *
 * Example:
 *   BOARDRUNNER_IMU_ICM20602_AZ=-2048
 */
void boardrunner_imu_get_sample(const char *device_name, BoardrunnerImuSampleLSB *sample);
