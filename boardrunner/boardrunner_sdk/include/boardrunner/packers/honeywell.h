#pragma once

/*
 * packers/honeywell.h — SI (Tesla) → Honeywell HMC58xx raw int16 counts
 *
 * Covers the HMC5843, HMC5883L, and HMC5983 family — all share the same
 * register-level protocol and gain table, differing only in output data
 * order:
 *
 *   HMC5843     : data at 0x03..0x08 in X, Y, Z order (MSB then LSB per axis)
 *   HMC5883L    : data at 0x03..0x08 in X, Z, Y order (Honeywell errata quirk)
 *   HMC5983     : same as 5883L
 *
 * ArduPilot's AP_Compass_HMC5843 driver reads six bytes starting at 0x03
 * and decodes them per the layout. The layout choice is passed to
 * hmc_pack_be16() so the packer emits bytes in the right order.
 *
 * Scale: register LSB → tesla via user-selected gain setting (config
 * register B bits GN2..GN0). Gain values are in LSB per gauss; convert to
 * LSB per tesla by multiplying by 1e4.
 */

#include <stdint.h>
#include <boardrunner/canonical.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    HMC_LAYOUT_XYZ = 0,      /* HMC5843: X, Y, Z (default in old datasheets) */
    HMC_LAYOUT_XZY = 1,      /* HMC5883L / HMC5983 */
} HoneywellRegLayout;

/*
 * Per-chip gain / layout selection. Set once at init from CRB register.
 *
 *   HMC5843 default gain (CRB bits 5:7 = 001) = 1300 LSB/gauss
 *   HMC5883L default gain (CRB bits 5:7 = 001) = 1090 LSB/gauss
 *   HMC5983 same as 5883L
 *
 * Full-scale ±0.9 gauss (default) → int16 range ±1170 counts approx —
 * plenty of headroom for Earth's ~0.5 gauss field.
 */
typedef struct {
    float lsb_per_tesla;      /* e.g. 1090 * 1e4 = 1.09e7 for HMC5883L */
    HoneywellRegLayout layout;
} HoneywellChipScale;

/* -------------------------------------------------------------------------- */
/* Ready-made scale presets                                                   */
/* -------------------------------------------------------------------------- */

static inline HoneywellChipScale honeywell_scale_hmc5843_default(void) {
    /* 1300 LSB/gauss at CRB gain 001; multiply by 1e4 for LSB/tesla. */
    return (HoneywellChipScale){
        .lsb_per_tesla = 1.30e7f,
        .layout        = HMC_LAYOUT_XYZ,
    };
}

static inline HoneywellChipScale honeywell_scale_hmc5883l_default(void) {
    /* 1090 LSB/gauss at CRB gain 001. */
    return (HoneywellChipScale){
        .lsb_per_tesla = 1.09e7f,
        .layout        = HMC_LAYOUT_XZY,
    };
}

/* -------------------------------------------------------------------------- */
/* Packing                                                                    */
/* -------------------------------------------------------------------------- */

/*
 * Convert an SI mag sample (Tesla, chip frame) to a 6-byte big-endian
 * int16 register frame ready to be memcpy'd into the slave's regs[] at
 * offset 0x03 (DATA_OUTPUT_X_MSB).
 *
 * Output for HMC_LAYOUT_XYZ (bytes 0..5):
 *   [0..1] mx_be16   (X MSB, X LSB)
 *   [2..3] my_be16   (Y MSB, Y LSB)
 *   [4..5] mz_be16   (Z MSB, Z LSB)
 *
 * Output for HMC_LAYOUT_XZY:
 *   [0..1] mx_be16
 *   [2..3] mz_be16   (Z BEFORE Y — Honeywell quirk)
 *   [4..5] my_be16
 *
 * Values overflowing int16 are saturated to ±32767.
 *
 * Pure function: no globals, no allocation, thread-safe.
 */
void honeywell_pack_be16(const BoardrunnerMagSample* si,
                         const HoneywellChipScale* scale,
                         uint8_t out[6]);

#ifdef __cplusplus
} /* extern "C" */
#endif
