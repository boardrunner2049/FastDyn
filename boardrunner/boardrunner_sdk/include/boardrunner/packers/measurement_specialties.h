#pragma once

/*
 * packers/measurement_specialties.h — SI → MS5611 D1/D2 raw ADC counts
 *
 * Covers the MS5611 baro on CubeBlack (both instances, SPI1 and SPI4).
 * The MS5607 is the same family with slightly different PROM coefficients
 * and a different constant table — add a sibling packer if needed.
 *
 * The MS5611 exposes:
 *   D1 = 24-bit raw pressure ADC count
 *   D2 = 24-bit raw temperature ADC count
 *   PROM[6] = factory calibration coefficients C1..C6 (unsigned 16-bit)
 *
 * ArduPilot's driver reads D1/D2 and computes pressure/temperature via
 * the datasheet formulas (§7 of DA5611-01BA03). Our packer inverts those
 * formulas so a live SI-unit sample from the endpoint gets converted
 * back to the D1/D2 counts the driver will decode into the SAME SI values.
 *
 * Round-trip fidelity is limited by the datasheet's linearised model and
 * the fact that our packer applies the coefficients we tell it to — if the
 * slave model reports a different PROM (via the SPI read path) than the
 * packer used, the driver's decoded pressure will disagree with what we
 * injected. Callers MUST use matching PROM coefficients on both sides.
 */

#include <stdint.h>
#include <boardrunner/canonical.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * The six MS5611 PROM calibration coefficients (word index 1..6).
 * Word 0 is a manufacturer/reserved ID and word 7 is a CRC — neither
 * enters the pressure/temperature math.
 */
typedef struct {
    uint16_t c1;    /* pressure sensitivity                   | SENS_T1 */
    uint16_t c2;    /* pressure offset                        | OFF_T1 */
    uint16_t c3;    /* temperature coefficient of SENS        | TCS */
    uint16_t c4;    /* temperature coefficient of OFF         | TCO */
    uint16_t c5;    /* reference temperature                  | T_REF */
    uint16_t c6;    /* temperature coefficient of temperature | TEMPSENS */
} MS5611PromCoeffs;

/*
 * Factory-typical PROM values used by the existing ms5611_spi{1,4}.c
 * fallback path. Kept here so both the slave model and the packer agree.
 * Change here → change in both slave models.
 */
static inline MS5611PromCoeffs ms5611_default_prom(void) {
    MS5611PromCoeffs c = {
        .c1 = 40127u, .c2 = 36924u, .c3 = 23317u,
        .c4 = 23282u, .c5 = 33464u, .c6 = 28312u,
    };
    return c;
}

/*
 * Convert an SI-unit BoardrunnerBaroSample (pressure_pa, temp_c) into
 * D1 and D2 24-bit raw ADC counts, given the chip's PROM coefficients.
 *
 * D1 and D2 are what ArduPilot's driver will fetch via ADC_READ after a
 * conversion command; reversing the datasheet formula guarantees that
 * driver decode(pack(x)) ≈ x.
 *
 * Values are saturated to the valid 24-bit range on overflow.
 *
 * Pure function: no globals, no allocation, thread-safe.
 */
void ms5611_pack_d1_d2(const BoardrunnerBaroSample* si,
                       const MS5611PromCoeffs* coeffs,
                       uint32_t* out_d1,
                       uint32_t* out_d2);

#ifdef __cplusplus
} /* extern "C" */
#endif
