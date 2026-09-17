// Device Model for I2C1

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define I2C1_BASE 0x40005400ULL

/* Register offsets */
#define I2C_CR1_OFF    0x00
#define I2C_CR2_OFF    0x04
#define I2C_OAR1_OFF   0x08
#define I2C_OAR2_OFF   0x0C
#define I2C_DR_OFF     0x10
#define I2C_SR1_OFF    0x14
#define I2C_SR2_OFF    0x18
#define I2C_CCR_OFF    0x1C
#define I2C_TRISE_OFF  0x20

/* CR1 bits */
#define CR1_PE         0x0001
#define CR1_START      0x0100
#define CR1_STOP       0x0200
#define CR1_ACK        0x0400
#define CR1_POS        0x0800
#define CR1_SWRST      0x8000

/* SR1 bits */
#define SR1_SB         0x0001
#define SR1_ADDR       0x0002
#define SR1_BTF        0x0004
#define SR1_RXNE       0x0040
#define SR1_TXE        0x0080
#define SR1_AF         0x0400

/* SR2 bits */
#define SR2_MSL        0x0001
#define SR2_BUSY       0x0002
#define SR2_TRA        0x0004

typedef struct {
    I2CBus bus;

    uint16_t cr1;
    uint16_t cr2;
    uint16_t oar1;
    uint16_t oar2;
    uint16_t sr1;
    uint16_t sr2;
    uint16_t ccr;
    uint16_t trise;
    uint8_t  dr;
    uint8_t  rx_buf[2];
    uint8_t  rx_count;

    bool transfer_active;
    bool recv_mode;
    bool awaiting_address;
    bool sr1_read_for_addr_clear;
    bool stop_pending;
    bool rx_single_byte_mode;
} I2C1State;

static I2C1State g_i2c1;

static void i2c1_soft_reset(I2C1State *s)
{
    s->cr1 = 0x0000;
    s->cr2 = 0x0000;
    s->oar1 = 0x0000;
    s->oar2 = 0x0000;
    s->sr1 = 0x0000;
    s->sr2 = 0x0000;
    s->ccr = 0x0000;
    s->trise = 0x0002; /* conservative STM32-like reset */
    s->dr = 0x00;
    s->rx_buf[0] = 0x00;
    s->rx_buf[1] = 0x00;
    s->rx_count = 0;

    s->transfer_active = false;
    s->recv_mode = false;
    s->awaiting_address = false;
    s->sr1_read_for_addr_clear = false;
    s->stop_pending = false;
    s->rx_single_byte_mode = false;
}

static void i2c1_end_transfer_if_needed(I2C1State *s)
{
    if (s->transfer_active) {
        api_i2c_end_transfer(&s->bus);
    }

    s->transfer_active = false;
    s->recv_mode = false;
    s->awaiting_address = false;
    s->sr1_read_for_addr_clear = false;
    s->stop_pending = false;
    s->rx_single_byte_mode = false;
    s->rx_count = 0;

    s->sr1 &= (uint16_t)~(SR1_SB | SR1_ADDR | SR1_BTF | SR1_RXNE | SR1_TXE);
    s->sr2 &= (uint16_t)~(SR2_MSL | SR2_BUSY | SR2_TRA);
}

static void i2c1_begin_start(I2C1State *s)
{
    /*
     * A repeated START must not be turned into a STOP on the bus.
     * Preserve slave-side transaction context by avoiding api_i2c_end_transfer()
     * here; just close the current internal phase and wait for the next address.
     */
    if (s->transfer_active) {
        s->transfer_active = false;
        s->recv_mode = false;
    }

    s->awaiting_address = true;
    s->sr1_read_for_addr_clear = false;
    s->stop_pending = false;
    s->rx_single_byte_mode = false;
    s->rx_count = 0;

    s->sr1 &= (uint16_t)~(SR1_ADDR | SR1_BTF | SR1_RXNE | SR1_TXE | SR1_AF);
    s->sr1 |= SR1_SB;

    s->sr2 &= (uint16_t)~(SR2_MSL | SR2_BUSY | SR2_TRA);
}

static void i2c1_update_rx_flags(I2C1State *s)
{
    /*
     * By the time firmware polls SR1 the shift register has already clocked in
     * the following byte, so a pending receive byte is always reported as
     * RXNE|BTF (0x44); the flags fall together once DR is drained.
     */
    if (s->rx_count > 0) {
        s->dr = s->rx_buf[0];
        s->sr1 |= (SR1_RXNE | SR1_BTF);
    } else {
        s->sr1 &= (uint16_t)~(SR1_RXNE | SR1_BTF);
    }

    s->sr1 &= (uint16_t)~SR1_TXE;
}

static int i2c1_rx_target_depth(I2C1State *s)
{
    if (!s->transfer_active || !s->recv_mode) {
        return 0;
    }

    /*
     * STM32F1 master-receiver normally exposes a two-byte receive pipeline
     * (DR + shift register), so SR1 shows RXNE|BTF when both are occupied.
     *
     * Only the true single-byte receive sequence collapses that pipeline to a
     * single staged byte. Terminal multi-byte cases (including the STM32F1
     * 2-byte receive sequence) still keep two bytes visible until firmware
     * drains them.
     */
    if (s->rx_single_byte_mode) {
        return 1;
    }

    return 2;
}

static bool i2c1_addr_phase_is_single_byte_receive(I2C1State *s)
{
    /*
     * ACK=0 before clearing ADDR does not uniquely identify a 1-byte receive on
     * STM32F1. Firmware also clears ACK before ADDR in the 2-byte receive path,
     * but distinguishes that case with POS=1. Only ACK=0 with POS=0 should
     * force the one-byte pipeline behavior.
     */
    if ((s->cr1 & CR1_ACK) != 0) {
        return false;
    }

    if ((s->cr1 & CR1_POS) != 0) {
        return false;
    }

    return true;
}

static void i2c1_prefetch_rx(I2C1State *s)
{
    int target;

    if (!s->transfer_active || !s->recv_mode) {
        s->rx_count = 0;
        s->sr1 &= (uint16_t)~(SR1_RXNE | SR1_BTF);
        return;
    }

    target = i2c1_rx_target_depth(s);
    while (s->rx_count < target && s->rx_count < 2) {
        s->rx_buf[s->rx_count++] = api_i2c_recv(&s->bus);
    }

    i2c1_update_rx_flags(s);
}

static void i2c1_finish_addr_phase(I2C1State *s)
{
    if (!(s->sr1 & SR1_ADDR)) {
        return;
    }

    s->sr1 &= (uint16_t)~SR1_ADDR;
    s->sr1_read_for_addr_clear = false;

    if (!s->transfer_active) {
        return;
    }

    if (s->recv_mode) {
        /*
         * Latch whether this address phase is the true STM32F1 single-byte
         * master-receive sequence. Do not treat every ACK=0 case as single-byte:
         * the 2-byte receive path also clears ACK before ADDR, but still needs
         * the normal two-byte RXNE|BTF pipeline.
         */
        s->rx_single_byte_mode = i2c1_addr_phase_is_single_byte_receive(s);
        s->rx_count = 0;
        i2c1_prefetch_rx(s);
    } else {
        s->sr1 |= SR1_TXE;
        s->sr1 &= (uint16_t)~(SR1_RXNE | SR1_BTF);
    }
}

static void i2c1_handle_address_write(I2C1State *s, uint8_t value)
{
    uint8_t address = (uint8_t)(value >> 1);
    bool is_recv = (value & 0x1) ? true : false;
    int ret;

    s->dr = value;
    s->awaiting_address = false;
    s->sr1 &= (uint16_t)~SR1_SB;
    s->sr1_read_for_addr_clear = false;

    ret = api_i2c_start_transfer(&s->bus, address, is_recv);
    if (ret == 0) {
        s->transfer_active = true;
        s->recv_mode = is_recv;

        s->sr1 &= (uint16_t)~(SR1_AF | SR1_RXNE | SR1_BTF);
        s->sr1 |= SR1_ADDR;

        /*
         * On STM32F1 in master-transmitter mode, once the address byte is ACKed
         * the peripheral exposes ADDR and TXE together. Firmware often polls
         * for TXE before performing the SR1/SR2 sequence that clears ADDR.
         */
        if (!is_recv) {
            s->sr1 |= SR1_TXE;
        } else {
            s->sr1 &= (uint16_t)~SR1_TXE;
        }

        s->sr2 |= (SR2_MSL | SR2_BUSY);
        if (!is_recv) {
            s->sr2 |= SR2_TRA;
        } else {
            s->sr2 &= (uint16_t)~SR2_TRA;
        }
    } else {
        s->transfer_active = false;
        s->recv_mode = false;

        s->sr1 |= SR1_AF;
        s->sr1 &= (uint16_t)~(SR1_ADDR | SR1_RXNE | SR1_BTF | SR1_TXE);
        s->sr2 &= (uint16_t)~(SR2_MSL | SR2_BUSY | SR2_TRA);
    }
}

static uint16_t i2c1_read_reg16(I2C1State *s, hwaddr offset)
{
    switch (offset) {
    case I2C_CR1_OFF:
        return s->cr1;

    case I2C_CR2_OFF:
        return s->cr2;

    case I2C_OAR1_OFF:
        return s->oar1;

    case I2C_OAR2_OFF:
        return s->oar2;

    case I2C_DR_OFF:
        if (s->recv_mode) {
            if (s->sr1 & SR1_ADDR) {
                i2c1_finish_addr_phase(s);
            }

            /*
             * Model DR + one extra staged byte so RXNE/BTF can persist across
             * reads without over-consuming the slave when ACK is cleared or
             * STOP is armed for the final byte(s).
             */
            {
                uint8_t val = s->dr;

                if (s->rx_count > 0) {
                    val = s->rx_buf[0];
                    if (s->rx_count > 1) {
                        s->rx_buf[0] = s->rx_buf[1];
                    }
                    s->rx_count--;
                }

                if (s->transfer_active) {
                    /*
                     * STM32F1 master-receiver keeps the normal two-byte
                     * receive pipeline alive for multi-byte transfers even
                     * after firmware clears ACK for the terminal phase.
                     *
                     * Only the latched single-byte receive sequence
                     * (ACK cleared before ADDR is cleared) and an explicit
                     * STOP should suppress further refills. If we key this off
                     * the live ACK bit, SR1 drops from RXNE|BTF (0x44) to
                     * RXNE-only (0x40) too early and firmware diverges.
                     */
                    if (!s->stop_pending && !s->rx_single_byte_mode) {
                        i2c1_prefetch_rx(s);
                    } else {
                        i2c1_update_rx_flags(s);
                        if (s->stop_pending && s->rx_count == 0) {
                            i2c1_end_transfer_if_needed(s);
                        }
                    }
                } else {
                    s->rx_count = 0;
                    s->sr1 &= (uint16_t)~(SR1_RXNE | SR1_BTF);
                }

                return val;
            }
        }
        return s->dr;

    case I2C_SR1_OFF:
        if (s->sr1 & SR1_ADDR) {
            s->sr1_read_for_addr_clear = true;
        }
        return s->sr1;

    case I2C_SR2_OFF:
        if ((s->sr1 & SR1_ADDR) && s->sr1_read_for_addr_clear) {
            i2c1_finish_addr_phase(s);
        }
        return s->sr2;

    case I2C_CCR_OFF:
        return s->ccr;

    case I2C_TRISE_OFF:
        return s->trise;

    default:
        dev_debug("I2C1: read from unknown register");
        return 0;
    }
}

static void i2c1_write_reg16(I2C1State *s, hwaddr offset, uint16_t value)
{
    switch (offset) {
    case I2C_CR1_OFF: {
        bool old_swrst = (s->cr1 & CR1_SWRST) != 0;
        bool do_swrst = (value & CR1_SWRST) != 0;
        bool do_start = (value & CR1_START) != 0;
        bool do_stop  = (value & CR1_STOP)  != 0;

        /* Writable software view; START/STOP auto-clear after action. */
        s->cr1 = (uint16_t)(value & ~(CR1_START | CR1_STOP));

        if (do_swrst) {
            /*
             * SWRST is software-visible while asserted. Hardware reads back the
             * bit as 1 until firmware clears it, so reset internal state but
             * keep the latched CR1 image.
             */
            if (!old_swrst) {
                i2c1_end_transfer_if_needed(s);
                i2c1_soft_reset(s);
                s->cr1 = (uint16_t)(value & ~(CR1_START | CR1_STOP));
            }
            return;
        }

        if (do_start) {
            i2c1_begin_start(s);
        }

        if (do_stop) {
            if (s->transfer_active && s->recv_mode) {
                int target;

                /*
                 * STOP during master-receive does not instantly make BTF vanish
                 * on STM32F1; the final staged bytes remain readable from the
                 * peripheral. Snapshot that terminal pipeline now, then stop
                 * fetching new bytes once firmware starts draining it.
                 */
                s->stop_pending = true;
                target = i2c1_rx_target_depth(s);
                while (s->rx_count < target && s->rx_count < 2) {
                    s->rx_buf[s->rx_count++] = api_i2c_recv(&s->bus);
                }
                i2c1_update_rx_flags(s);

                if (s->rx_count == 0) {
                    i2c1_end_transfer_if_needed(s);
                }
            } else {
                i2c1_end_transfer_if_needed(s);
            }
        }
        break;
    }

    case I2C_CR2_OFF:
        s->cr2 = value;
        break;

    case I2C_OAR1_OFF:
        s->oar1 = value;
        break;

    case I2C_OAR2_OFF:
        s->oar2 = value;
        break;

    case I2C_DR_OFF: {
        uint8_t v = (uint8_t)(value & 0xFF);
        s->dr = v;

        if (s->awaiting_address || (s->sr1 & SR1_SB)) {
            i2c1_handle_address_write(s, v);
            break;
        }

        if (s->transfer_active && !s->recv_mode) {
            int ret = api_i2c_send(&s->bus, v);
            if (ret == 0) {
                s->sr1 &= (uint16_t)~SR1_AF;
                s->sr1 |= (SR1_TXE | SR1_BTF);
            } else {
                s->sr1 |= SR1_AF;
                s->sr1 &= (uint16_t)~(SR1_TXE | SR1_BTF);
            }
        }
        break;
    }

    case I2C_CCR_OFF:
        s->ccr = value;
        break;

    case I2C_TRISE_OFF:
        s->trise = value;
        break;

    case I2C_SR1_OFF:
        /* Hardware-owned in this minimal model: ignore writes. */
        break;

    case I2C_SR2_OFF:
        /* Read-only status in this minimal model: ignore writes. */
        break;

    default:
        dev_debug("I2C1: write to unknown register");
        break;
    }
}

uint64_t i2c1_read(void *opaque, hwaddr addr, unsigned size)
{
    I2C1State *s = (I2C1State *)opaque;
    hwaddr offset;
    uint16_t regv;

    if (addr < I2C1_BASE) {
        dev_debug("I2C1: read below base");
        return 0;
    }

    offset = addr - I2C1_BASE;
    regv = i2c1_read_reg16(s, offset);

    switch (size) {
    case 1:
        return (uint8_t)(regv & 0xFF);
    case 2:
    case 4:
    default:
        return regv;
    }
}

void i2c1_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    I2C1State *s = (I2C1State *)opaque;
    hwaddr offset;
    uint16_t regv;

    (void)size;

    if (addr < I2C1_BASE) {
        dev_debug("I2C1: write below base");
        return;
    }

    offset = addr - I2C1_BASE;
    regv = (uint16_t)(value & 0xFFFF);
    i2c1_write_reg16(s, offset, regv);
}

/* MUST return &g_i2c1 — framework stores this and passes it as opaque to _read/_write */
void* i2c1_init(ConfigSection* model_info)
{
    memset(&g_i2c1, 0, sizeof(g_i2c1));
    g_i2c1.bus = api_i2c_init_bus(model_info);
    i2c1_soft_reset(&g_i2c1);
    return &g_i2c1;
}