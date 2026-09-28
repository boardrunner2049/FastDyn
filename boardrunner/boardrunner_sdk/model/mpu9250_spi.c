// SPI Slave implementation: mpu9250_spi.c
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

#define MPU9250_REG_SMPLRT_DIV        0x19
#define MPU9250_REG_CONFIG            0x1A
#define MPU9250_REG_GYRO_CONFIG       0x1B
#define MPU9250_REG_ACCEL_CONFIG      0x1C
#define MPU9250_REG_ACCEL_CONFIG2     0x1D
#define MPU9250_REG_FIFO_EN           0x23
#define MPU9250_REG_INT_PIN_CFG       0x37
#define MPU9250_REG_INT_ENABLE        0x38
#define MPU9250_REG_INT_STATUS        0x3A
#define MPU9250_REG_ACCEL_XOUT_H      0x3B
#define MPU9250_REG_TEMP_OUT_H        0x41
#define MPU9250_REG_GYRO_XOUT_H       0x43
#define MPU9250_REG_SIGNAL_PATH_RESET 0x68
#define MPU9250_REG_USER_CTRL         0x6A
#define MPU9250_REG_PWR_MGMT_1        0x6B
#define MPU9250_REG_PWR_MGMT_2        0x6C
#define MPU9250_REG_FIFO_COUNTH       0x72
#define MPU9250_REG_FIFO_COUNTL       0x73
#define MPU9250_REG_FIFO_R_W          0x74
#define MPU9250_REG_WHOAMI            0x75

#define MPU9250_WHOAMI_VALUE          0x71
#define MPU9250_SPI_READ_FLAG         0x80

#define MPU9250_PWR_RESET             0x80
#define MPU9250_USER_FIFO_RESET       0x04
#define MPU9250_USER_FIFO_EN          0x40

typedef struct {
    bool initialized;
    bool cs_active;
    bool expect_command;
    bool read_phase;
    uint8_t current_reg;
    uint8_t regs[128];
    uint8_t fifo_sample[14];
    unsigned fifo_pos;
    uint32_t sample_counter;
} MPU9250State;

static MPU9250State g_mpu9250;

/* VIO channel state — populated by slave_spi_init if TOML declared a
 * data_source URI. When g_channel is NULL, the model falls back to the
 * legacy chip-LSB sample source for bit-exact boot behaviour. */
static BrChannel*          g_channel = NULL;
static InvensenseChipScale g_scale;

static void mpu9250_store_be16(uint8_t *dst, int16_t v) {
    dst[0] = (uint8_t)(((uint16_t)v >> 8) & 0xFFU);
    dst[1] = (uint8_t)((uint16_t)v & 0xFFU);
}

static void mpu9250_update_sample(MPU9250State *s) {
    s->sample_counter++;

    BoardrunnerImuSample si;
    if (g_channel != NULL && BR_IMU_READ(g_channel, &si)) {
        /* Live SI sample. Pack through the invensense translator. Layout is
         * ACCEL_TEMP_GYRO for MPU9250 (same as ICM20602). */
        uint8_t frame[14];
        invensense_pack_be16(&si, &g_scale, frame);
        memcpy(&s->regs[MPU9250_REG_ACCEL_XOUT_H], frame, sizeof(frame));
    } else {
        /* Fallback: legacy static defaults. Preserves bit-exact boot. */
        BoardrunnerImuSampleLSB lsb;
        boardrunner_imu_get_sample("mpu9250", &lsb);

        mpu9250_store_be16(&s->regs[MPU9250_REG_ACCEL_XOUT_H + 0], lsb.ax_lsb);
        mpu9250_store_be16(&s->regs[MPU9250_REG_ACCEL_XOUT_H + 2], lsb.ay_lsb);
        mpu9250_store_be16(&s->regs[MPU9250_REG_ACCEL_XOUT_H + 4], lsb.az_lsb);
        mpu9250_store_be16(&s->regs[MPU9250_REG_TEMP_OUT_H],       lsb.temp_lsb);
        mpu9250_store_be16(&s->regs[MPU9250_REG_GYRO_XOUT_H + 0],  lsb.gx_lsb);
        mpu9250_store_be16(&s->regs[MPU9250_REG_GYRO_XOUT_H + 2],  lsb.gy_lsb);
        mpu9250_store_be16(&s->regs[MPU9250_REG_GYRO_XOUT_H + 4],  lsb.gz_lsb);
    }

    memcpy(s->fifo_sample, &s->regs[MPU9250_REG_ACCEL_XOUT_H], sizeof(s->fifo_sample));
}

static void mpu9250_reset_registers(MPU9250State *s) {
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[MPU9250_REG_WHOAMI] = MPU9250_WHOAMI_VALUE;
    s->regs[MPU9250_REG_PWR_MGMT_1] = 0x40U;
    s->regs[MPU9250_REG_INT_STATUS] = 0x01U;
    s->fifo_pos = 0U;
    mpu9250_update_sample(s);
}

static void mpu9250_init_state(MPU9250State *s) {
    memset(s, 0, sizeof(*s));
    s->initialized = true;
    s->expect_command = true;
    mpu9250_reset_registers(s);
}

static bool mpu9250_fifo_enabled(MPU9250State *s) {
    return (s->regs[MPU9250_REG_FIFO_EN] != 0U) ||
           ((s->regs[MPU9250_REG_USER_CTRL] & MPU9250_USER_FIFO_EN) != 0U);
}

static bool mpu9250_reg_autoincrement(uint8_t reg) {
    uint8_t addr = reg & 0x7FU;

    if (addr == MPU9250_REG_FIFO_R_W) {
        return false;
    }

    return true;
}

static bool mpu9250_reg_refreshes_sample(uint8_t reg) {
    uint8_t addr = reg & 0x7FU;

    switch (addr) {
    case MPU9250_REG_ACCEL_XOUT_H:
    case MPU9250_REG_TEMP_OUT_H:
    case MPU9250_REG_GYRO_XOUT_H:
        return true;
    default:
        return false;
    }
}

static uint8_t mpu9250_read_reg(MPU9250State *s, uint8_t reg) {
    uint8_t addr = reg & 0x7FU;

    switch (addr) {
    case MPU9250_REG_INT_STATUS:
        mpu9250_update_sample(s);
        return 0x01U;

    case MPU9250_REG_FIFO_COUNTH:
        if (mpu9250_fifo_enabled(s)) {
            mpu9250_update_sample(s);
            s->fifo_pos = 0U;
            return 0x00U;
        }
        return 0x00U;

    case MPU9250_REG_FIFO_COUNTL:
        return mpu9250_fifo_enabled(s) ? (uint8_t)sizeof(s->fifo_sample) : 0x00U;

    case MPU9250_REG_FIFO_R_W:
        if (!mpu9250_fifo_enabled(s)) {
            return 0x00U;
        }
        if (s->fifo_pos >= sizeof(s->fifo_sample)) {
            s->fifo_pos = 0U;
            mpu9250_update_sample(s);
        }
        return s->fifo_sample[s->fifo_pos++];

    default:
        if (mpu9250_reg_refreshes_sample(addr)) {
            mpu9250_update_sample(s);
        }
        return s->regs[addr];
    }
}

static void mpu9250_write_reg(MPU9250State *s, uint8_t reg, uint8_t value) {
    uint8_t addr = reg & 0x7FU;

    if (addr == MPU9250_REG_PWR_MGMT_1 && (value & MPU9250_PWR_RESET) != 0U) {
        bool was_initialized = s->initialized;
        bool cs_active = s->cs_active;

        mpu9250_init_state(s);
        s->initialized = was_initialized;
        s->cs_active = cs_active;
        s->expect_command = false;
        return;
    }

    s->regs[addr] = value;

    if (addr == MPU9250_REG_USER_CTRL &&
        (value & MPU9250_USER_FIFO_RESET) != 0U) {
        s->fifo_pos = 0U;
        mpu9250_update_sample(s);
    }
}

uint32_t slave_spi_transfer(uint32_t value) {
    MPU9250State *s = &g_mpu9250;
    uint8_t byte = (uint8_t)(value & 0xFFU);

    if (!s->initialized) {
        mpu9250_init_state(s);
    }
    if (!s->cs_active) {
        return 0xFFU;
    }

    if (s->expect_command) {
        s->read_phase = ((byte & MPU9250_SPI_READ_FLAG) != 0U);
        s->current_reg = (uint8_t)(byte & 0x7FU);
        s->expect_command = false;
        return 0xFFU;
    }

    if (s->read_phase) {
        uint8_t ret = mpu9250_read_reg(s, s->current_reg);

        if (mpu9250_reg_autoincrement(s->current_reg)) {
            s->current_reg = (uint8_t)((s->current_reg + 1U) & 0x7FU);
        }

        return ret;
    }

    mpu9250_write_reg(s, s->current_reg, byte);
    if (mpu9250_reg_autoincrement(s->current_reg)) {
        s->current_reg = (uint8_t)((s->current_reg + 1U) & 0x7FU);
    }
    return 0xFFU;
}

void slave_spi_set_cs(int level) {
    MPU9250State *s = &g_mpu9250;

    if (!s->initialized) {
        mpu9250_init_state(s);
    }

    s->cs_active = (level == 0);
    s->expect_command = true;
    s->read_phase = false;
    s->current_reg = 0U;
}

/* Optional per-slave init hook — see icm20602_spi.c for rationale. */
void slave_spi_init(const SpiSlaveInit *info) {
    g_scale = invensense_scale_mpu9250_default();

    if (info != NULL && info->data_source != NULL && info->data_source[0] != '\0') {
        g_channel = api_channel_attach(info->data_source,
                                       BR_ROLE_READER,
                                       sizeof(BoardrunnerImuSample));
        if (g_channel == NULL) {
            dev_debug("mpu9250: attach '%s' failed, using static defaults\n",
                      info->data_source);
        } else {
            dev_debug("mpu9250: attached VIO channel '%s'\n", info->data_source);
        }
    }
}
