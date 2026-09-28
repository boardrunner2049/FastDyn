#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <device.h>
#include <boardrunner/vio.h>
#include <boardrunner/spi.h>
#include <boardrunner/canonical.h>
#include <boardrunner/channel.h>
#include <boardrunner/packers/measurement_specialties.h>

#define MS5611_CMD_RESET        0x1E
#define MS5611_CMD_PROM_BASE    0xA0
#define MS5611_CMD_ADC_READ     0x00

/* VIO channel state — set by slave_spi_init if data_source is in TOML.
 * When g_channel is NULL, the model falls back to the legacy hardcoded
 * D1/D2 counter pattern for bit-exact boot behaviour. */
static BrChannel*        g_channel = NULL;
static MS5611PromCoeffs  g_coeffs;

typedef struct {
    uint16_t prom[8];
    uint8_t out_buf[3];
    unsigned out_len;
    unsigned out_pos;
    bool cs_active;
    uint32_t d1;
    uint32_t d2;
    bool last_conv_is_d2;
    uint32_t sample_counter;
} MS5611State;

static MS5611State g_ms5611;

static uint8_t ms5611_crc4(uint16_t prom[8]) {
    uint16_t n_rem = 0;
    uint16_t crc_read = prom[7] & 0x000FU;
    uint8_t cnt;
    uint8_t n_bit;

    prom[7] &= 0xFF00U;

    for (cnt = 0; cnt < 16U; cnt++) {
        if ((cnt & 1U) != 0U) {
            n_rem ^= (uint16_t)(prom[cnt >> 1] & 0x00FFU);
        } else {
            n_rem ^= (uint16_t)(prom[cnt >> 1] >> 8);
        }

        for (n_bit = 8U; n_bit > 0U; n_bit--) {
            if ((n_rem & 0x8000U) != 0U) {
                n_rem = (uint16_t)((n_rem << 1) ^ 0x3000U);
            } else {
                n_rem = (uint16_t)(n_rem << 1);
            }
        }
    }

    prom[7] = (uint16_t)((prom[7] & 0xFF00U) | crc_read);
    return (uint8_t)((n_rem >> 12) & 0x0FU);
}

static void ms5611_load_default_prom(MS5611State *s) {
    uint16_t tmp[8];

    tmp[0] = 0x0000U;
    tmp[1] = 40127U;
    tmp[2] = 36924U;
    tmp[3] = 23317U;
    tmp[4] = 23282U;
    tmp[5] = 33464U;
    tmp[6] = 28312U;
    tmp[7] = 0x0000U;

    memcpy(s->prom, tmp, sizeof(tmp));
    s->prom[7] = (uint16_t)((s->prom[7] & 0xFFF0U) | ms5611_crc4(s->prom));
}

static void ms5611_reset_state(MS5611State *s) {
    s->out_len = 0U;
    s->out_pos = 0U;
    s->d1 = 9085466U;
    s->d2 = 8569150U;
    s->last_conv_is_d2 = false;
    s->sample_counter = 0U;
}

static void ms5611_prepare_prom(MS5611State *s, unsigned idx) {
    uint16_t v = s->prom[idx & 7U];

    s->out_buf[0] = (uint8_t)(v >> 8);
    s->out_buf[1] = (uint8_t)(v & 0xFFU);
    s->out_len = 2U;
    s->out_pos = 0U;
}

static void ms5611_prepare_adc(MS5611State *s) {
    uint32_t v = s->last_conv_is_d2 ? s->d2 : s->d1;

    s->out_buf[0] = (uint8_t)((v >> 16) & 0xFFU);
    s->out_buf[1] = (uint8_t)((v >> 8) & 0xFFU);
    s->out_buf[2] = (uint8_t)(v & 0xFFU);
    s->out_len = 3U;
    s->out_pos = 0U;
}

static void ms5611_start_conversion(MS5611State *s, bool is_d2) {
    s->sample_counter++;
    s->last_conv_is_d2 = is_d2;

    /* Live SI sample path — read from shm via channel API and pack to D1/D2. */
    BoardrunnerBaroSample si;
    if (g_channel != NULL && BR_BARO_READ(g_channel, &si)) {
        uint32_t d1, d2;
        ms5611_pack_d1_d2(&si, &g_coeffs, &d1, &d2);
        s->d1 = d1;
        s->d2 = d2;
        return;
    }

    /* Fallback: legacy hardcoded counter pattern. Preserves bit-exact boot
     * behaviour when no data_source is configured or the writer is silent. */
    s->d1 = 9085466U + (s->sample_counter & 0x3FU);
    s->d2 = 8569150U + ((s->sample_counter << 1) & 0x3FU);
}

uint32_t slave_spi_transfer(uint32_t value) {
    uint8_t cmd = (uint8_t)(value & 0xFFU);
    uint8_t ret = 0U;

    if (g_ms5611.prom[1] == 0U) {
        ms5611_load_default_prom(&g_ms5611);
        ms5611_reset_state(&g_ms5611);
    }

    if (g_ms5611.out_pos < g_ms5611.out_len) {
        ret = g_ms5611.out_buf[g_ms5611.out_pos++];
        if (g_ms5611.out_pos >= g_ms5611.out_len) {
            g_ms5611.out_pos = 0U;
            g_ms5611.out_len = 0U;
        }
        return ret;
    }

    if (cmd == MS5611_CMD_RESET) {
        ms5611_reset_state(&g_ms5611);
        return 0U;
    }

    if ((cmd >= MS5611_CMD_PROM_BASE) &&
        (cmd <= (MS5611_CMD_PROM_BASE + 14U)) &&
        ((cmd & 1U) == 0U)) {
        ms5611_prepare_prom(&g_ms5611, (unsigned)((cmd - MS5611_CMD_PROM_BASE) >> 1));
        return 0U;
    }

    if (cmd == MS5611_CMD_ADC_READ) {
        ms5611_prepare_adc(&g_ms5611);
        return 0U;
    }

    if ((cmd & 0xF0U) == 0x40U) {
        ms5611_start_conversion(&g_ms5611, false);
        return 0U;
    }

    if ((cmd & 0xF0U) == 0x50U) {
        ms5611_start_conversion(&g_ms5611, true);
        return 0U;
    }

    return ret;
}

void slave_spi_set_cs(int level) {
    if (g_ms5611.prom[1] == 0U) {
        ms5611_load_default_prom(&g_ms5611);
        ms5611_reset_state(&g_ms5611);
    }

    g_ms5611.cs_active = (level == 0);
    if (!g_ms5611.cs_active) {
        g_ms5611.out_pos = 0U;
        g_ms5611.out_len = 0U;
    }
}

/* Optional per-slave init hook. Attaches a VIO channel if TOML declared
 * a data_source URI. See docs/BoardRunner_VIO_Physics_Architecture.md
 * for the boot-safety fallback story. */
void slave_spi_init(const SpiSlaveInit *info) {
    /* Packer must use the same PROM coefficients the model reports on
     * PROM_READ. Otherwise driver decode(pack(x)) != x. Use the built-in
     * default set defined in the packer header. */
    g_coeffs = ms5611_default_prom();

    if (info != NULL && info->data_source != NULL && info->data_source[0] != '\0') {
        g_channel = api_channel_attach(info->data_source,
                                       BR_ROLE_READER,
                                       sizeof(BoardrunnerBaroSample));
        if (g_channel == NULL) {
            dev_debug("ms5611_spi1: attach '%s' failed, using static defaults\n",
                      info->data_source);
        } else {
            dev_debug("ms5611_spi1: attached VIO channel '%s'\n", info->data_source);
        }
    }
}
