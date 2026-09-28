#pragma once

/*
 * packers/invensense.h — SI → Invensense register-frame conversion
 *
 * Covers the three Invensense parts wired to the CubeBlack:
 *   ICM20602   (SPI4, cs=4)   — layout ACCEL_TEMP_GYRO, 14-byte contiguous
 *   ICM20948   (SPI4, cs=1)   — layout ACCEL_GYRO_TEMP, 14-byte contiguous
 *   MPU9250    (SPI1, cs=4)   — layout ACCEL_TEMP_GYRO, 14-byte contiguous
 *
 * The register maps are near-identical (ACCEL_XOUT_H onwards is 14 bytes of
 * big-endian int16 samples in every case), but the ordering of accel / temp /
 * gyro within those 14 bytes differs — see InvensenseRegLayout below.
 *
 * Full-scale settings, temperature calibration constants, and register offsets
 * are all encoded in InvensenseChipScale — the caller (a slave model) fills it
 * once at init from its current CR settings, then this header's pack function
 * uses it every sample.
 *
 * Nothing here touches shm or channels. The caller reads a
 * BoardrunnerImuSample from wherever (BR_IMU_READ or the static-default
 * getter), passes it to invensense_pack_be16(), and memcpy's the 14-byte
 * output into its own regs[] backing store.
 */

#include <stdint.h>
#include <boardrunner/canonical.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Ordering of accel / temp / gyro inside the 14-byte contiguous register
 * frame that starts at each chip's "ACCEL_XOUT_H"-like address. */
typedef enum {
    /* MPU9250, ICM20602 — TEMP_OUT_H sits between accel and gyro. */
    INVENSENSE_LAYOUT_ACCEL_TEMP_GYRO = 0,
    /* ICM20948 bank0 — GYRO_XOUT_H comes right after ACCEL_ZOUT_L, temp last. */
    INVENSENSE_LAYOUT_ACCEL_GYRO_TEMP = 1,
} InvensenseRegLayout;

/*
 * Chip-specific scaling that maps SI units to the int16 LSBs that will land
 * in the register frame. Set once per chip after the firmware writes the
 * relevant CR (GYRO_CONFIG / ACCEL_CONFIG on MPU-line parts, GYRO_CONFIG_1 /
 * ACCEL_CONFIG on ICM20948).
 *
 * Defaults for CubeBlack init state (used before ArduPilot programs FS):
 *   Accel: ±16 g full scale → 2048 LSB / g → 208.86 LSB per m/s²
 *   Gyro:  ±2000 dps        → 16.4 LSB / dps → 939.65 LSB per rad/s
 *   Temp:  MPU9250   temp_c = (raw - roomOffset)/sensitivity + 21 = raw/333.87 + 21
 *          ICM20602  temp_c = raw/326.8 + 25
 *          ICM20948  temp_c = (raw - roomOffset)/333.87 + 21   (like MPU9250)
 *
 * All fields required; no zeros allowed for the *_lsb_per_* fields (they are
 * denominators; zero produces INF and silently kills the AHRS).
 */
typedef struct {
    float accel_lsb_per_mps2;
    float gyro_lsb_per_radps;
    float temp_room_c;              /* per-chip datasheet room-temp calibration */
    float temp_lsb_per_c;           /* per-chip sensitivity */
    InvensenseRegLayout layout;
} InvensenseChipScale;

/* -------------------------------------------------------------------------- */
/* Ready-made scale presets for CubeBlack init state (±16 g, ±2000 dps)       */
/* -------------------------------------------------------------------------- */

/*
 * These match the reset-state full-scale range for each chip. Slave models
 * may start from a preset then adjust when firmware writes GYRO_CONFIG /
 * ACCEL_CONFIG registers. Presets are intentionally functions (not const
 * struct literals) so callers can copy-and-mutate without touching globals.
 */
static inline InvensenseChipScale invensense_scale_mpu9250_default(void) {
    return (InvensenseChipScale){
        .accel_lsb_per_mps2 = 208.86f,          /* 2048 LSB/g ÷ 9.80665 */
        .gyro_lsb_per_radps = 939.65f,          /* 16.4 LSB/(°/s) × 57.2958 */
        .temp_room_c        = 21.0f,
        .temp_lsb_per_c     = 333.87f,
        .layout             = INVENSENSE_LAYOUT_ACCEL_TEMP_GYRO,
    };
}

static inline InvensenseChipScale invensense_scale_icm20602_default(void) {
    return (InvensenseChipScale){
        .accel_lsb_per_mps2 = 208.86f,
        .gyro_lsb_per_radps = 939.65f,
        .temp_room_c        = 25.0f,
        .temp_lsb_per_c     = 326.8f,
        .layout             = INVENSENSE_LAYOUT_ACCEL_TEMP_GYRO,
    };
}

static inline InvensenseChipScale invensense_scale_icm20948_default(void) {
    return (InvensenseChipScale){
        .accel_lsb_per_mps2 = 208.86f,
        .gyro_lsb_per_radps = 939.65f,
        .temp_room_c        = 21.0f,
        .temp_lsb_per_c     = 333.87f,
        .layout             = INVENSENSE_LAYOUT_ACCEL_GYRO_TEMP,
    };
}

/* -------------------------------------------------------------------------- */
/* Packing                                                                    */
/* -------------------------------------------------------------------------- */

/*
 * Convert a SI-unit sample to a 14-byte big-endian register frame ready to
 * be memcpy'd into the slave's regs[] backing store at the ACCEL_XOUT_H-like
 * offset.
 *
 * Output layout when scale->layout == ACCEL_TEMP_GYRO (MPU9250, ICM20602):
 *   [ 0..1]  ax_be16
 *   [ 2..3]  ay_be16
 *   [ 4..5]  az_be16
 *   [ 6..7]  temp_be16
 *   [ 8..9]  gx_be16
 *   [10..11] gy_be16
 *   [12..13] gz_be16
 *
 * Output layout when scale->layout == ACCEL_GYRO_TEMP (ICM20948):
 *   [ 0..1]  ax_be16
 *   [ 2..3]  ay_be16
 *   [ 4..5]  az_be16
 *   [ 6..7]  gx_be16
 *   [ 8..9]  gy_be16
 *   [10..11] gz_be16
 *   [12..13] temp_be16
 *
 * SI values that overflow int16 after scaling are saturated (not wrapped) —
 * matches real-chip saturation behaviour so ArduPilot's over-range detector
 * sees the same edge case.
 *
 * Pure function: no globals, no allocation, safe to call from any thread.
 */
void invensense_pack_be16(const BoardrunnerImuSample *si,
                          const InvensenseChipScale *scale,
                          uint8_t out[14]);

/*
 * Same as invensense_pack_be16() but returns the intermediate int16s too,
 * which some FIFO paths want to store separately from the register frame.
 * If `out_raw` is NULL only the byte frame is produced.
 */
typedef struct {
    int16_t ax, ay, az;
    int16_t gx, gy, gz;
    int16_t temp;
} InvensenseRawFrame;

void invensense_pack_be16_with_raw(const BoardrunnerImuSample *si,
                                   const InvensenseChipScale *scale,
                                   uint8_t out[14],
                                   InvensenseRawFrame *out_raw);

#ifdef __cplusplus
} /* extern "C" */
#endif
