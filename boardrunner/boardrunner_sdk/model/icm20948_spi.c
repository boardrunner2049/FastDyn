// SPI Slave implementation: icm20948_spi.c
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <device.h>
#include <boardrunner/imu_sample.h>
#include <boardrunner/vio.h>
#include <boardrunner/spi.h>
#include <boardrunner/canonical.h>
#include <boardrunner/channel.h>
#include <boardrunner/packers/invensense.h>

#define ICM20948_SPI_READ_FLAG      0x80U

#define ICM20948_REG_BANK_SEL       0x7FU

#define ICM20948_BANK0_WHOAMI       0x00U
#define ICM20948_BANK0_USER_CTRL    0x03U
#define ICM20948_BANK0_PWR_MGMT_1   0x06U
#define ICM20948_BANK0_PWR_MGMT_2   0x07U
#define ICM20948_BANK0_INT_STATUS   0x19U
#define ICM20948_BANK0_ACCEL_XOUT_H 0x2DU
#define ICM20948_BANK0_GYRO_XOUT_H  0x33U
#define ICM20948_BANK0_TEMP_OUT_H   0x39U
#define ICM20948_BANK0_FIFO_EN_2    0x67U
#define ICM20948_BANK0_FIFO_RST     0x68U
#define ICM20948_BANK0_FIFO_MODE    0x69U
#define ICM20948_BANK0_FIFO_COUNTH  0x70U
#define ICM20948_BANK0_FIFO_COUNTL  0x71U
#define ICM20948_BANK0_FIFO_R_W     0x72U

#define ICM20948_WHOAMI_VALUE       0xEAU

#define ICM20948_PWR_RESET          0x80U
#define ICM20948_USER_FIFO_EN       0x40U
#define ICM20948_USER_FIFO_RESET    0x04U

#define ICM20948_NUM_BANKS          4
#define ICM20948_FIFO_SAMPLE_SIZE   14

typedef struct {
    bool initialized;
    bool cs_active;
    bool expect_command;
    bool read_phase;
    uint8_t current_reg;
    uint8_t current_bank;
    uint8_t regs[ICM20948_NUM_BANKS][128];
    uint8_t fifo_sample[ICM20948_FIFO_SAMPLE_SIZE];
    unsigned fifo_pos;
    uint32_t sample_counter;
} ICM20948State;

static ICM20948State g_icm20948;

/* VIO channel state — populated by slave_spi_init if TOML declared a
 * data_source URI. When g_channel is NULL, fall back to legacy LSB defaults. */
static BrChannel*          g_channel = NULL;
static InvensenseChipScale g_scale;

static void icm20948_store_be16(uint8_t *dst, int16_t v) {
    dst[0] = (uint8_t)(((uint16_t)v >> 8) & 0xFFU);
    dst[1] = (uint8_t)((uint16_t)v & 0xFFU);
}

/* ICM20948 bank 0 register layout DIFFERS from MPU9250/ICM20602: gyro comes
 * right after accel, temp is LAST. Reflected via ICM20948-specific packer
 * layout INVENSENSE_LAYOUT_ACCEL_GYRO_TEMP. But the fake register file's
 * ACCEL_XOUT_H..TEMP_OUT_H range is NOT contiguous (accel at 0x2D, gyro at
 * 0x33, temp at 0x39) — so the 14-byte packer output can't be memcpy'd in
 * one shot. Split into three copies at the correct offsets. */
static void icm20948_update_sample(ICM20948State *s) {
    s->sample_counter++;

    BoardrunnerImuSample si;
    if (g_channel != NULL && BR_IMU_READ(g_channel, &si)) {
        /* Live SI sample. Packer emits 14-byte frame in ACCEL_GYRO_TEMP order:
         *   [0..5]   accel BE16 x/y/z
         *   [6..11]  gyro  BE16 x/y/z
         *   [12..13] temp  BE16
         * Split-copy into the discontiguous ICM20948 bank 0 register file. */
        uint8_t frame[14];
        invensense_pack_be16(&si, &g_scale, frame);
        memcpy(&s->regs[0][ICM20948_BANK0_ACCEL_XOUT_H], &frame[0], 6);
        memcpy(&s->regs[0][ICM20948_BANK0_GYRO_XOUT_H],  &frame[6], 6);
        memcpy(&s->regs[0][ICM20948_BANK0_TEMP_OUT_H],   &frame[12], 2);
    } else {
        /* Fallback: legacy static defaults. Preserves bit-exact boot. */
        BoardrunnerImuSampleLSB lsb;
        boardrunner_imu_get_sample("icm20948", &lsb);

        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_ACCEL_XOUT_H + 0], lsb.ax_lsb);
        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_ACCEL_XOUT_H + 2], lsb.ay_lsb);
        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_ACCEL_XOUT_H + 4], lsb.az_lsb);
        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_TEMP_OUT_H],       lsb.temp_lsb);
        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_GYRO_XOUT_H + 0],  lsb.gx_lsb);
        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_GYRO_XOUT_H + 2],  lsb.gy_lsb);
        icm20948_store_be16(&s->regs[0][ICM20948_BANK0_GYRO_XOUT_H + 4],  lsb.gz_lsb);
    }

    memcpy(s->fifo_sample,
           &s->regs[0][ICM20948_BANK0_ACCEL_XOUT_H],
           sizeof(s->fifo_sample));
}

static void icm20948_reset_registers(ICM20948State *s) {
    memset(s->regs, 0, sizeof(s->regs));
    s->current_bank = 0U;
    s->regs[0][ICM20948_BANK0_WHOAMI] = ICM20948_WHOAMI_VALUE;
    s->regs[0][ICM20948_REG_BANK_SEL] = 0x00U;
    s->regs[0][ICM20948_BANK0_PWR_MGMT_1] = 0x40U;
    s->regs[0][ICM20948_BANK0_INT_STATUS] = 0x01U;
    s->fifo_pos = 0U;
    icm20948_update_sample(s);
}

static void icm20948_init_state(ICM20948State *s) {
    memset(s, 0, sizeof(*s));
    s->initialized = true;
    s->expect_command = true;
    icm20948_reset_registers(s);
}

static bool icm20948_fifo_enabled(ICM20948State *s) {
    return (s->regs[0][ICM20948_BANK0_FIFO_EN_2] != 0U) ||
           ((s->regs[0][ICM20948_BANK0_USER_CTRL] & ICM20948_USER_FIFO_EN) != 0U);
}

static bool icm20948_reg_autoincrement(uint8_t reg) {
    uint8_t addr = reg & 0x7FU;

    if (addr == ICM20948_BANK0_FIFO_R_W) {
        return false;
    }

    return true;
}

static bool icm20948_reg_refreshes_sample(uint8_t reg) {
    uint8_t addr = reg & 0x7FU;

    switch (addr) {
    case ICM20948_BANK0_ACCEL_XOUT_H:
    case ICM20948_BANK0_GYRO_XOUT_H:
    case ICM20948_BANK0_TEMP_OUT_H:
        return true;
    default:
        return false;
    }
}

static uint8_t icm20948_read_reg(ICM20948State *s, uint8_t reg) {
    uint8_t addr = reg & 0x7FU;

    if (addr == ICM20948_REG_BANK_SEL) {
        return (uint8_t)(s->current_bank << 4);
    }

    if (s->current_bank == 0U) {
        switch (addr) {
        case ICM20948_BANK0_INT_STATUS:
            icm20948_update_sample(s);
            return 0x01U;

        case ICM20948_BANK0_FIFO_COUNTH:
            if (icm20948_fifo_enabled(s)) {
                icm20948_update_sample(s);
                s->fifo_pos = 0U;
            }
            return 0x00U;

        case ICM20948_BANK0_FIFO_COUNTL:
            return icm20948_fifo_enabled(s) ? (uint8_t)sizeof(s->fifo_sample) : 0x00U;

        case ICM20948_BANK0_FIFO_R_W:
            if (!icm20948_fifo_enabled(s)) {
                return 0x00U;
            }
            if (s->fifo_pos >= sizeof(s->fifo_sample)) {
                s->fifo_pos = 0U;
                icm20948_update_sample(s);
            }
            return s->fifo_sample[s->fifo_pos++];

        default:
            if (icm20948_reg_refreshes_sample(addr)) {
                icm20948_update_sample(s);
            }
            break;
        }
    }

    return s->regs[s->current_bank][addr];
}

static void icm20948_write_reg(ICM20948State *s, uint8_t reg, uint8_t value) {
    uint8_t addr = reg & 0x7FU;

    if (addr == ICM20948_REG_BANK_SEL) {
        s->current_bank = (uint8_t)((value >> 4) & 0x03U);
        s->regs[0][ICM20948_REG_BANK_SEL] = (uint8_t)(s->current_bank << 4);
        return;
    }

    if (s->current_bank == 0U &&
        addr == ICM20948_BANK0_PWR_MGMT_1 &&
        (value & ICM20948_PWR_RESET) != 0U) {
        bool was_initialized = s->initialized;
        bool cs_active = s->cs_active;

        icm20948_init_state(s);
        s->initialized = was_initialized;
        s->cs_active = cs_active;
        s->expect_command = false;
        return;
    }

    s->regs[s->current_bank][addr] = value;

    if (s->current_bank == 0U &&
        addr == ICM20948_BANK0_USER_CTRL &&
        (value & ICM20948_USER_FIFO_RESET) != 0U) {
        s->fifo_pos = 0U;
        icm20948_update_sample(s);
    }

    if (s->current_bank == 0U &&
        addr == ICM20948_BANK0_FIFO_RST) {
        s->fifo_pos = 0U;
        icm20948_update_sample(s);
    }
}

uint32_t slave_spi_transfer(uint32_t value) {
    ICM20948State *s = &g_icm20948;
    uint8_t byte = (uint8_t)(value & 0xFFU);

    if (!s->initialized) {
        icm20948_init_state(s);
    }
    if (!s->cs_active) {
        return 0xFFU;
    }

    if (s->expect_command) {
        s->read_phase = ((byte & ICM20948_SPI_READ_FLAG) != 0U);
        s->current_reg = (uint8_t)(byte & 0x7FU);
        s->expect_command = false;
        return 0xFFU;
    }

    if (s->read_phase) {
        uint8_t ret = icm20948_read_reg(s, s->current_reg);

        if (icm20948_reg_autoincrement(s->current_reg)) {
            s->current_reg = (uint8_t)((s->current_reg + 1U) & 0x7FU);
        }

        return ret;
    }

    icm20948_write_reg(s, s->current_reg, byte);
    if (icm20948_reg_autoincrement(s->current_reg)) {
        s->current_reg = (uint8_t)((s->current_reg + 1U) & 0x7FU);
    }
    return 0xFFU;
}

void slave_spi_set_cs(int level) {
    ICM20948State *s = &g_icm20948;

    if (!s->initialized) {
        icm20948_init_state(s);
    }

    s->cs_active = (level == 0);
    s->expect_command = true;
    s->read_phase = false;
    s->current_reg = 0U;
}

/* Optional per-slave init hook — see icm20602_spi.c for rationale. */
void slave_spi_init(const SpiSlaveInit *info) {
    g_scale = invensense_scale_icm20948_default();

    if (info != NULL && info->data_source != NULL && info->data_source[0] != '\0') {
        g_channel = api_channel_attach(info->data_source,
                                       BR_ROLE_READER,
                                       sizeof(BoardrunnerImuSample));
        if (g_channel == NULL) {
            dev_debug("icm20948: attach '%s' failed, using static defaults\n",
                      info->data_source);
        } else {
            dev_debug("icm20948: attached VIO channel '%s'\n", info->data_source);
        }
    }
}
