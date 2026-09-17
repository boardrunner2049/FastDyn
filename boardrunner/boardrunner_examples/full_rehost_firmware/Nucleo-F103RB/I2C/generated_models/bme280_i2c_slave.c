// bme280_slave.c
//
// Minimal stateful I2C slave model for a BME280-like device.
// Implements:
//   - register pointer write on first byte after I2C_START_SEND
//   - sequential register writes/reads with auto-increment
//   - CHIP_ID register at 0xD0 returning 0x60
//   - soft reset on write 0xB6 to 0xE0
//
// Required framework entry points:
//   - slave_i2c_event
//   - slave_i2c_send
//   - slave_i2c_recv
//
// Optional aliases also exported:
//   - bme280_event
//   - bme280_send
//   - bme280_receive
//
// Build:
//   gcc -shared -fPIC -O2 -o slave.so bme280_slave.c

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include <device.h>
#include <boardrunner/vio.h>

// ---- BME280 register map ----
#define REG_ID         0xD0
#define REG_RESET      0xE0
#define REG_CTRL_HUM   0xF2
#define REG_STATUS     0xF3
#define REG_CTRL_MEAS  0xF4
#define REG_CONFIG     0xF5
#define REG_DATA       0xF7

#define CHIP_ID_BME280 0x60
#define RESET_CMD      0xB6

typedef struct {
    uint8_t regs[256];
    bool    expect_reg_addr;   // next written byte is register pointer
    uint8_t reg_ptr;           // current register pointer
    bool    inited;
} bme280_state_t;

static bme280_state_t g_state;

static void load_defaults(bme280_state_t *s)
{
    int i;

    memset(s->regs, 0, sizeof(s->regs));

    // Core identity
    s->regs[REG_ID] = CHIP_ID_BME280;

    // Control / status defaults
    s->regs[REG_CTRL_HUM]  = 0x00;
    s->regs[REG_STATUS]    = 0x00;
    s->regs[REG_CTRL_MEAS] = 0x00;
    s->regs[REG_CONFIG]    = 0x00;

    // Calibration blocks (0x88..0xA1 and 0xE1..0xE7) as read from the sensor
    {
        static const uint8_t calib_a[26] = {
            0xBB, 0x6E, 0x14, 0x68, 0x32, 0x00, 0x77, 0x8F, 0x8A, 0xD6, 0xD0, 0x0B, 0x5B,
            0x13, 0x5E, 0x00, 0xF9, 0xFF, 0xB4, 0x2D, 0xE8, 0xD1, 0x88, 0x13, 0x00, 0x96
        };
        static const uint8_t calib_h[7] = { 0x7A, 0x01, 0x00, 0x11, 0x2B, 0x03, 0x3C };
        for (i = 0; i < 26; i++) {
            s->regs[0x88 + i] = calib_a[i];
        }
        for (i = 0; i < 7; i++) {
            s->regs[0xE1 + i] = calib_h[i];
        }
    }

    // Measurement data: pressure(3), temperature(3), humidity(2)
    s->regs[0xF7] = 0x5F;
    s->regs[0xF8] = 0x82;
    s->regs[0xF9] = 0x00;
    s->regs[0xFA] = 0x80;
    s->regs[0xFB] = 0x71;
    s->regs[0xFC] = 0x00;
    s->regs[0xFD] = 0x66;
    s->regs[0xFE] = 0x16;

    s->expect_reg_addr = false;
    s->reg_ptr = 0x00;
}

static void lazy_init(void)
{
    if (!g_state.inited) {
        memset(&g_state, 0, sizeof(g_state));
        load_defaults(&g_state);
        g_state.inited = true;

        {
            static char msg[] = "[bme280] slave initialized\n";
            dev_debug(msg);
        }
    }
}

#ifdef __cplusplus
extern "C" {
#endif

// Return 0 for ACK, 1 for NACK.
int slave_i2c_send(uint8_t data)
{
    uint8_t reg;

    lazy_init();

    if (g_state.expect_reg_addr) {
        g_state.reg_ptr = data;
        g_state.expect_reg_addr = false;
        return 0;
    }

    reg = g_state.reg_ptr;
    g_state.regs[reg] = data;

    if (reg == REG_RESET && data == RESET_CMD) {
        load_defaults(&g_state);
        {
            static char msg[] = "[bme280] soft reset\n";
            dev_debug(msg);
        }
        return 0;
    }

    g_state.reg_ptr = (uint8_t)(g_state.reg_ptr + 1);
    return 0;
}

uint8_t slave_i2c_recv(void)
{
    uint8_t reg;
    uint8_t val;

    lazy_init();

    reg = g_state.reg_ptr;
    val = g_state.regs[reg];

    // Keep STATUS deterministic and idle
    if (reg == REG_STATUS) {
        val = 0x00;
    }

    g_state.reg_ptr = (uint8_t)(g_state.reg_ptr + 1);
    return val;
}

int slave_i2c_event(enum i2c_event event)
{
    lazy_init();

    switch (event) {
    case I2C_START_SEND:
        // First byte of write phase is treated as the register pointer
        g_state.expect_reg_addr = true;
        break;

    case I2C_START_RECV:
        // Repeated-start read uses previously latched register pointer
        g_state.expect_reg_addr = false;
        break;

    case I2C_FINISH:
    case I2C_NACK:
    default:
        g_state.expect_reg_addr = false;
        break;
    }

    return 0;
}

// Optional aliases
int bme280_send(uint8_t data)
{
    return slave_i2c_send(data);
}

uint8_t bme280_receive(void)
{
    return slave_i2c_recv();
}

int bme280_event(enum i2c_event event)
{
    return slave_i2c_event(event);
}

#ifdef __cplusplus
}
#endif