/*
 * invensense_packer.c — SI → Invensense register-frame conversion
 *
 * See packers/invensense.h for the surface. Pure function: no globals, no
 * allocations, no I/O. Safe from any thread.
 *
 * The layout enum picks one of two orderings inside the 14-byte contiguous
 * register block that starts at each chip's ACCEL_XOUT_H-equivalent
 * address. See the header comment for the two byte layouts.
 */

#include <boardrunner/packers/invensense.h>

#include <math.h>
#include <stdint.h>
#include <string.h>

/* Saturate a float to the int16 range. int16 min/max are -32768 and 32767.
 * Values that round outside get clamped so the caller sees the same
 * saturation behaviour a real chip would produce on over-range. */
static int16_t sat_int16(float v)
{
    /* lroundf rounds half-away-from-zero, matching what ArduPilot expects
     * (real chips also round rather than truncate at the LSB boundary). */
    long r = lroundf(v);
    if (r > 32767l) {
        return (int16_t)32767;
    }
    if (r < -32768l) {
        return (int16_t)-32768;
    }
    return (int16_t)r;
}

/* Big-endian int16 store (MSB first, matching STM datasheet notation). */
static inline void store_be16(uint8_t* dst, int16_t v)
{
    uint16_t u = (uint16_t)v;
    dst[0] = (uint8_t)((u >> 8) & 0xFFu);
    dst[1] = (uint8_t)(u & 0xFFu);
}

void invensense_pack_be16_with_raw(const BoardrunnerImuSample* si,
                                   const InvensenseChipScale* scale,
                                   uint8_t out[14],
                                   InvensenseRawFrame* out_raw)
{
    if (si == NULL || scale == NULL || out == NULL) {
        return;
    }

    /* SI → int16 LSB with per-axis scaling and saturation. */
    const int16_t ax = sat_int16(si->ax_mps2  * scale->accel_lsb_per_mps2);
    const int16_t ay = sat_int16(si->ay_mps2  * scale->accel_lsb_per_mps2);
    const int16_t az = sat_int16(si->az_mps2  * scale->accel_lsb_per_mps2);
    const int16_t gx = sat_int16(si->gx_radps * scale->gyro_lsb_per_radps);
    const int16_t gy = sat_int16(si->gy_radps * scale->gyro_lsb_per_radps);
    const int16_t gz = sat_int16(si->gz_radps * scale->gyro_lsb_per_radps);

    /* temp_c → raw: raw = (temp_c - room) * sensitivity */
    const int16_t tp = sat_int16((si->temp_c - scale->temp_room_c) *
                                 scale->temp_lsb_per_c);

    if (out_raw != NULL) {
        out_raw->ax   = ax;
        out_raw->ay   = ay;
        out_raw->az   = az;
        out_raw->gx   = gx;
        out_raw->gy   = gy;
        out_raw->gz   = gz;
        out_raw->temp = tp;
    }

    switch (scale->layout) {
    case INVENSENSE_LAYOUT_ACCEL_TEMP_GYRO:
        /* MPU9250, ICM20602 — accel, temp, gyro contiguous. */
        store_be16(&out[0],  ax);
        store_be16(&out[2],  ay);
        store_be16(&out[4],  az);
        store_be16(&out[6],  tp);
        store_be16(&out[8],  gx);
        store_be16(&out[10], gy);
        store_be16(&out[12], gz);
        break;

    case INVENSENSE_LAYOUT_ACCEL_GYRO_TEMP:
        /* ICM20948 bank0 — gyro right after accel, temp last. */
        store_be16(&out[0],  ax);
        store_be16(&out[2],  ay);
        store_be16(&out[4],  az);
        store_be16(&out[6],  gx);
        store_be16(&out[8],  gy);
        store_be16(&out[10], gz);
        store_be16(&out[12], tp);
        break;

    default:
        /* Unknown layout — zero the frame rather than emit garbage. */
        memset(out, 0, 14);
        break;
    }
}

void invensense_pack_be16(const BoardrunnerImuSample* si,
                          const InvensenseChipScale* scale,
                          uint8_t out[14])
{
    invensense_pack_be16_with_raw(si, scale, out, NULL);
}
