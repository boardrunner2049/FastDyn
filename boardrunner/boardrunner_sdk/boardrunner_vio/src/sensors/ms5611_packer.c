/*
 * ms5611_packer.c — SI → MS5611 D1/D2 conversion
 *
 * Inverse of the pressure/temperature decode formulas in the MS5611
 * datasheet §7 (DA5611-01BA03). Given desired pressure (Pa) and
 * temperature (°C), computes the D1/D2 raw ADC counts that ArduPilot's
 * driver would decode back to those SI values.
 *
 * Math (forward direction, from datasheet):
 *     dT   = D2 - C5 * 2^8
 *     TEMP = 2000 + dT * C6 / 2^23         (units of 0.01 °C)
 *     OFF  = C2 * 2^16 + (C4 * dT) / 2^7
 *     SENS = C1 * 2^15 + (C3 * dT) / 2^8
 *     P    = (D1 * SENS / 2^21 - OFF) / 2^15   (units of Pa)
 *
 * Inverse:
 *     dT   = (TEMP_hundredths - 2000) * 2^23 / C6
 *     D2   = dT + C5 * 2^8
 *     OFF  = C2 * 2^16 + (C4 * dT) / 2^7
 *     SENS = C1 * 2^15 + (C3 * dT) / 2^8
 *     D1   = (P_pa * 2^15 + OFF) * 2^21 / SENS
 *
 * We compute in double precision throughout for numerical stability
 * (dT can be large-magnitude when temp is far from 20°C), then saturate
 * to 24-bit unsigned.
 */

#include <boardrunner/packers/measurement_specialties.h>

#include <math.h>
#include <stdint.h>

#define MS5611_24BIT_MAX  0xFFFFFFu

static uint32_t sat_u24(double v) {
    if (v <= 0.0)                 return 0u;
    if (v >= (double)MS5611_24BIT_MAX) return MS5611_24BIT_MAX;
    return (uint32_t)llround(v);
}

void ms5611_pack_d1_d2(const BoardrunnerBaroSample* si,
                       const MS5611PromCoeffs* coeffs,
                       uint32_t* out_d1,
                       uint32_t* out_d2)
{
    if (si == NULL || coeffs == NULL) {
        return;
    }

    /* Guard against degenerate PROM values that would divide by zero. */
    if (coeffs->c6 == 0u || coeffs->c1 == 0u) {
        if (out_d1) *out_d1 = 0u;
        if (out_d2) *out_d2 = 0u;
        return;
    }

    /* Step 1: temperature-derived quantities. */
    /* TEMP is in 0.01 °C in the datasheet formula. */
    const double temp_hundredths = (double)si->temp_c * 100.0;
    /* dT = (TEMP - 2000) * 2^23 / C6 */
    const double dT = (temp_hundredths - 2000.0) * 8388608.0 / (double)coeffs->c6;

    /* D2 = dT + C5 * 2^8 */
    const double D2_d = dT + (double)coeffs->c5 * 256.0;

    /* Step 2: OFF and SENS at this temperature. */
    /* OFF  = C2 * 2^16 + (C4 * dT) / 2^7 */
    const double OFF  = (double)coeffs->c2 * 65536.0 + (double)coeffs->c4 * dT / 128.0;
    /* SENS = C1 * 2^15 + (C3 * dT) / 2^8 */
    const double SENS = (double)coeffs->c1 * 32768.0 + (double)coeffs->c3 * dT / 256.0;

    /* Step 3: pressure → D1. */
    /* D1 = (P * 2^15 + OFF) * 2^21 / SENS */
    const double D1_d = ((double)si->pressure_pa * 32768.0 + OFF) * 2097152.0 / SENS;

    if (out_d1) *out_d1 = sat_u24(D1_d);
    if (out_d2) *out_d2 = sat_u24(D2_d);
}
