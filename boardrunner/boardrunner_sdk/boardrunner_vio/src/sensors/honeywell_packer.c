/*
 * honeywell_packer.c — SI Tesla → HMC58xx big-endian int16 register bytes.
 *
 * Pure function, no globals. See packers/honeywell.h for wire format.
 */

#include <boardrunner/packers/honeywell.h>

#include <math.h>
#include <stdint.h>
#include <string.h>

static int16_t sat_int16(float v) {
    long r = lroundf(v);
    if (r > 32767l)  return (int16_t)32767;
    if (r < -32768l) return (int16_t)-32768;
    return (int16_t)r;
}

static inline void store_be16(uint8_t* dst, int16_t v) {
    uint16_t u = (uint16_t)v;
    dst[0] = (uint8_t)((u >> 8) & 0xFFu);
    dst[1] = (uint8_t)(u & 0xFFu);
}

void honeywell_pack_be16(const BoardrunnerMagSample* si,
                         const HoneywellChipScale* scale,
                         uint8_t out[6])
{
    if (si == NULL || scale == NULL || out == NULL) {
        return;
    }

    const int16_t mx = sat_int16(si->mx_tesla * scale->lsb_per_tesla);
    const int16_t my = sat_int16(si->my_tesla * scale->lsb_per_tesla);
    const int16_t mz = sat_int16(si->mz_tesla * scale->lsb_per_tesla);

    switch (scale->layout) {
    case HMC_LAYOUT_XYZ:
        /* HMC5843: X, Y, Z order */
        store_be16(&out[0], mx);
        store_be16(&out[2], my);
        store_be16(&out[4], mz);
        break;

    case HMC_LAYOUT_XZY:
        /* HMC5883L: X, Z, Y order */
        store_be16(&out[0], mx);
        store_be16(&out[2], mz);
        store_be16(&out[4], my);
        break;

    default:
        memset(out, 0, 6);
        break;
    }
}
