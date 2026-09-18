// Device Model for USART2
#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <limits.h>

// Inferred Register Functions:
// SR   : dynamic status flags (TXE, TC, RXNE, IDLE, ORE)
// DR   : transmit / receive data register
// BRR  : baud rate register
// CR1  : UE/RE/TE and interrupt enables
// CR2  : stored control register
// CR3  : stored control register

#define USART2_BASE_ADDR        0x40004400ULL

#define USART_SR_OFF            0x00
#define USART_DR_OFF            0x04
#define USART_BRR_OFF           0x08
#define USART_CR1_OFF           0x0C
#define USART_CR2_OFF           0x10
#define USART_CR3_OFF           0x14
#define USART_GTPR_OFF          0x18

// SR bits (STM32F1 USART)
#define USART_SR_PE             (1u << 0)
#define USART_SR_FE             (1u << 1)
#define USART_SR_NE             (1u << 2)
#define USART_SR_ORE            (1u << 3)
#define USART_SR_IDLE           (1u << 4)
#define USART_SR_RXNE           (1u << 5)
#define USART_SR_TC             (1u << 6)
#define USART_SR_TXE            (1u << 7)
#define USART_SR_LBD            (1u << 8)
#define USART_SR_CTS            (1u << 9)

// CR1 bits
#define USART_CR1_RE            (1u << 2)
#define USART_CR1_TE            (1u << 3)
#define USART_CR1_IDLEIE        (1u << 4)
#define USART_CR1_RXNEIE        (1u << 5)
#define USART_CR1_TCIE          (1u << 6)
#define USART_CR1_TXEIE         (1u << 7)
#define USART_CR1_PEIE          (1u << 8)
#define USART_CR1_UE            (1u << 13)

#define USART_CR1_WR_MASK       0x3FFFu
#define USART_CR2_WR_MASK       0x3FFFu
#define USART_CR3_WR_MASK       0x0FFFu
#define USART_BRR_WR_MASK       0xFFFFu
#define USART_GTPR_WR_MASK      0xFFFFu

#define USART2_IRQ_NUM          38
#define USART2_IRQ_LINE         (USART2_IRQ_NUM + 16)

#define USART2_RX_FIFO_LEN      64
#define USART2_RX_POLL_NS       1000000ULL  // 1 ms virtual-time periodic poll

typedef struct USART2State {
    uint32_t sr;
    uint32_t dr;
    uint32_t brr;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cr3;
    uint32_t gtpr;

    uint8_t  rx_fifo[USART2_RX_FIFO_LEN];
    unsigned rx_head;
    unsigned rx_tail;
    unsigned rx_count;

    bool sr_read_for_clear;
    bool irq_latched;

    int pty_fd;

    uint64_t tx_timer;
    uint64_t rx_timer;

    int64_t txe_ready_ns;
    int64_t tc_ready_ns;
    unsigned txe_polls_remaining;
    unsigned tc_polls_remaining;
} USART2State;

static USART2State g_usart2;

static inline int64_t usart2_now_ns(void) {
    return qemu_plugin_get_virtual_timer();
}

static inline void usart2_rx_fifo_reset(USART2State *s) {
    s->rx_head = 0;
    s->rx_tail = 0;
    s->rx_count = 0;
    s->dr = 0;
}

static inline bool usart2_enabled(const USART2State *s) {
    return (s->cr1 & USART_CR1_UE) != 0;
}

static inline bool usart2_rx_enabled(const USART2State *s) {
    return (s->cr1 & (USART_CR1_UE | USART_CR1_RE)) == (USART_CR1_UE | USART_CR1_RE);
}

static inline bool usart2_tx_enabled(const USART2State *s) {
    return (s->cr1 & (USART_CR1_UE | USART_CR1_TE)) == (USART_CR1_UE | USART_CR1_TE);
}

static uint64_t usart2_estimate_baud(const USART2State *s) {
    /*
     * STM32F1 USART BRR format (oversampling by 16):
     *   DIV = mantissa + fraction/16
     *   baud = fPCLK / DIV = (fPCLK * 16) / (mantissa*16 + fraction)
     *
     * USART2 is on APB1; use the common STM32F103 APB1 clock of 36 MHz.
     * This is only used to shape TXE/TC timing approximately.
     */
    uint32_t brr = s->brr & 0xFFFFu;
    uint32_t mantissa = (brr >> 4) & 0x0FFFu;
    uint32_t fraction = brr & 0x000Fu;
    uint64_t div16;
    uint64_t baud;

    if (mantissa == 0 && fraction == 0) {
        return 9600;
    }

    div16 = (uint64_t)mantissa * 16ULL + (uint64_t)fraction;
    if (div16 == 0) {
        return 9600;
    }

    baud = (36000000ULL * 16ULL) / div16;
    if (baud == 0) {
        baud = 1;
    }
    return baud;
}

static uint64_t usart2_char_time_ns(const USART2State *s) {
    uint64_t baud = usart2_estimate_baud(s);
    uint64_t ns = (1000000000ULL * 10ULL + baud - 1ULL) / baud; // ~8N1
    if (ns < 1000ULL) {
        ns = 1000ULL;
    }
    return ns;
}

static void usart2_maybe_raise_irq(USART2State *s) {
    bool pending = false;

    if (usart2_enabled(s)) {
        if ((s->cr1 & USART_CR1_RXNEIE) && (s->sr & USART_SR_RXNE)) {
            pending = true;
        }
        if ((s->cr1 & USART_CR1_TXEIE) && (s->sr & USART_SR_TXE)) {
            pending = true;
        }
        if ((s->cr1 & USART_CR1_TCIE) && (s->sr & USART_SR_TC)) {
            pending = true;
        }
        if ((s->cr1 & USART_CR1_IDLEIE) && (s->sr & USART_SR_IDLE)) {
            pending = true;
        }
    }

    if (pending && !s->irq_latched) {
        qemu_plugin_raise_irq(USART2_IRQ_LINE, false);
        s->irq_latched = true;
    } else if (!pending) {
        s->irq_latched = false;
    }
}

static void usart2_arm_next_tx_timer(USART2State *s) {
    int64_t next_ns = INT64_MAX;

    if (!usart2_tx_enabled(s)) {
        return;
    }

    if ((s->sr & USART_SR_TXE) == 0 && s->txe_ready_ns < next_ns) {
        next_ns = s->txe_ready_ns;
    }
    if ((s->sr & USART_SR_TC) == 0 && s->tc_ready_ns < next_ns) {
        next_ns = s->tc_ready_ns;
    }

    if (next_ns != INT64_MAX) {
        qemu_plugin_timer_alarm(s->tx_timer, (uint64_t)next_ns);
    }
}

static void usart2_update_tx_state(USART2State *s, bool from_sr_read) {
    int64_t now = usart2_now_ns();
    (void)from_sr_read;

    if (!usart2_tx_enabled(s)) {
        s->sr &= ~(USART_SR_TXE | USART_SR_TC);
        s->txe_ready_ns = INT64_MAX;
        s->tc_ready_ns = INT64_MAX;
        s->txe_polls_remaining = 0;
        s->tc_polls_remaining = 0;
        usart2_maybe_raise_irq(s);
        return;
    }

    /*
     * Hardware does not advance TXE/TC simply because firmware keeps reading
     * SR. The previous model promoted TX state after a fixed number of SR polls,
     * which made TXE appear too early and caused premature DR writes. Instead,
     * drive TXE/TC only from elapsed virtual time, while still checking the
     * deadlines synchronously on reads so we do not rely solely on timer
     * callbacks having already fired.
     */
    if ((s->sr & USART_SR_TXE) == 0 &&
        s->txe_ready_ns != INT64_MAX &&
        now >= s->txe_ready_ns) {
        s->sr |= USART_SR_TXE;
        s->txe_ready_ns = INT64_MAX;
        s->txe_polls_remaining = 0;
    }

    if ((s->sr & USART_SR_TC) == 0 &&
        s->tc_ready_ns != INT64_MAX &&
        now >= s->tc_ready_ns) {
        s->sr |= USART_SR_TC;
        s->tc_ready_ns = INT64_MAX;
        s->tc_polls_remaining = 0;
    }

    usart2_arm_next_tx_timer(s);
    usart2_maybe_raise_irq(s);
}

static void usart2_schedule_tx_ready(USART2State *s) {
    int64_t now = usart2_now_ns();
    uint64_t char_ns = usart2_char_time_ns(s);
    uint64_t txe_delay = char_ns / 2ULL;
    uint64_t tc_delay  = char_ns;

    if (txe_delay < 1000ULL) {
        txe_delay = 1000ULL;
    }
    if (tc_delay < txe_delay) {
        tc_delay = txe_delay;
    }

    s->txe_ready_ns = now + (int64_t)txe_delay;
    s->tc_ready_ns  = now + (int64_t)tc_delay;

    /*
     * Do not synthesize TXE/TC from poll counts. Tight SR polling is exactly
     * what exposed the bug: the firmware saw TXE much earlier than on hardware.
     * Keep these counters cleared so TX state only changes when virtual time has
     * actually reached the scheduled deadline.
     */
    s->txe_polls_remaining = 0U;
    s->tc_polls_remaining = 0U;

    usart2_arm_next_tx_timer(s);
}

static void usart2_tx_timer_cb(void *opaque) {
    USART2State *s = (USART2State *)opaque;
    usart2_update_tx_state(s, false);
}

static void usart2_queue_rx_byte(USART2State *s, uint8_t byte) {
    if (!usart2_rx_enabled(s)) {
        return;
    }

    if (s->rx_count >= USART2_RX_FIFO_LEN) {
        s->sr |= USART_SR_ORE;
        usart2_maybe_raise_irq(s);
        return;
    }

    s->rx_fifo[s->rx_tail] = byte;
    s->rx_tail = (s->rx_tail + 1U) % USART2_RX_FIFO_LEN;
    s->rx_count++;

    if (s->rx_count == 1U) {
        s->dr = byte;
    }

    s->sr |= USART_SR_RXNE;
    s->sr &= ~USART_SR_IDLE;

    usart2_maybe_raise_irq(s);
}

static void usart2_poll_host_rx(USART2State *s) {
    uint8_t byte = 0;
    int rc;

    if (s->pty_fd < 0) {
        return;
    }

    while (1) {
        rc = api_pty_read_nonblock(s->pty_fd, &byte);
        if (rc <= 0) {
            break;
        }
        usart2_queue_rx_byte(s, byte);
    }
}

static void usart2_rx_timer_cb(void *opaque) {
    USART2State *s = (USART2State *)opaque;
    usart2_poll_host_rx(s);
}

static uint32_t usart2_pop_dr(USART2State *s) {
    uint32_t ret = 0;
    bool had_byte = false;

    usart2_poll_host_rx(s);
    usart2_update_tx_state(s, false);

    if (s->rx_count > 0U) {
        had_byte = true;
        ret = s->rx_fifo[s->rx_head];
        s->rx_head = (s->rx_head + 1U) % USART2_RX_FIFO_LEN;
        s->rx_count--;

        if (s->rx_count > 0U) {
            s->dr = s->rx_fifo[s->rx_head];
            s->sr |= USART_SR_RXNE;
        } else {
            s->dr = 0;
            s->sr &= ~USART_SR_RXNE;
            /*
             * Approximation: once the final buffered byte is consumed,
             * expose IDLE to match trace states such as 0xD0/0x90/0x10.
             */
            s->sr |= USART_SR_IDLE;
        }
    } else {
        ret = s->dr & 0x1FFu;
    }

    if (s->sr_read_for_clear) {
        s->sr &= ~(USART_SR_PE | USART_SR_FE | USART_SR_NE | USART_SR_ORE);
        if (!had_byte) {
            s->sr &= ~USART_SR_IDLE;
        }
    }
    s->sr_read_for_clear = false;

    usart2_maybe_raise_irq(s);
    return ret & 0x1FFu;
}

static uint32_t usart2_read_reg(USART2State *s, uint32_t offset) {
    switch (offset) {
    case USART_SR_OFF:
        usart2_poll_host_rx(s);
        usart2_update_tx_state(s, true);
        s->sr_read_for_clear = true;
        return s->sr & 0x3FFu;

    case USART_DR_OFF:
        return usart2_pop_dr(s);

    case USART_BRR_OFF:
        return s->brr & USART_BRR_WR_MASK;

    case USART_CR1_OFF:
        return s->cr1 & USART_CR1_WR_MASK;

    case USART_CR2_OFF:
        return s->cr2 & USART_CR2_WR_MASK;

    case USART_CR3_OFF:
        return s->cr3 & USART_CR3_WR_MASK;

    case USART_GTPR_OFF:
        return s->gtpr & USART_GTPR_WR_MASK;

    default:
        return 0;
    }
}

static void usart2_apply_enable_state(USART2State *s, uint32_t old_cr1) {
    bool old_tx = ((old_cr1 & (USART_CR1_UE | USART_CR1_TE)) == (USART_CR1_UE | USART_CR1_TE));
    bool new_tx = usart2_tx_enabled(s);
    bool new_rx = usart2_rx_enabled(s);

    if (!new_rx) {
        usart2_rx_fifo_reset(s);
        s->sr &= ~(USART_SR_RXNE | USART_SR_ORE | USART_SR_IDLE);
    }

    if (!new_tx) {
        s->sr &= ~(USART_SR_TXE | USART_SR_TC);
        s->txe_ready_ns = INT64_MAX;
        s->tc_ready_ns = INT64_MAX;
        s->txe_polls_remaining = 0;
        s->tc_polls_remaining = 0;
    } else if (!old_tx && new_tx) {
        /*
         * Hardware trace shows transmitter status progressing after enable
         * rather than becoming ready immediately.
         */
        s->sr &= ~(USART_SR_TXE | USART_SR_TC);
        usart2_schedule_tx_ready(s);
    }

    usart2_poll_host_rx(s);
    usart2_update_tx_state(s, false);
    usart2_maybe_raise_irq(s);
}

static void usart2_write_reg(USART2State *s, uint32_t offset, uint32_t value) {
    switch (offset) {
    case USART_SR_OFF:
        /*
         * Minimal support for software clearing writable status bits.
         * TXE is hardware-owned; TC may be cleared by software.
         */
        if ((value & USART_SR_TC) == 0) {
            s->sr &= ~USART_SR_TC;
        }
        if ((value & USART_SR_IDLE) == 0) {
            s->sr &= ~USART_SR_IDLE;
        }
        if ((value & USART_SR_ORE) == 0) {
            s->sr &= ~USART_SR_ORE;
        }
        usart2_maybe_raise_irq(s);
        break;

    case USART_DR_OFF: {
        uint8_t byte = (uint8_t)(value & 0xFFu);

        s->dr = value & 0x1FFu;

        if (usart2_tx_enabled(s)) {
            if (s->pty_fd >= 0) {
                api_pty_write_req(s->pty_fd, byte);
            }

            s->sr &= ~(USART_SR_TXE | USART_SR_TC);
            usart2_schedule_tx_ready(s);
        }

        usart2_maybe_raise_irq(s);
        break;
    }

    case USART_BRR_OFF:
        s->brr = value & USART_BRR_WR_MASK;
        break;

    case USART_CR1_OFF: {
        uint32_t old_cr1 = s->cr1;
        s->cr1 = value & USART_CR1_WR_MASK;
        usart2_apply_enable_state(s, old_cr1);
        break;
    }

    case USART_CR2_OFF:
        s->cr2 = value & USART_CR2_WR_MASK;
        break;

    case USART_CR3_OFF:
        s->cr3 = value & USART_CR3_WR_MASK;
        break;

    case USART_GTPR_OFF:
        s->gtpr = value & USART_GTPR_WR_MASK;
        break;

    default:
        break;
    }
}

// This function will emulate all device reads
uint64_t usart2_read(void *opaque, hwaddr addr, unsigned size) {
    USART2State *s = (USART2State *)opaque;
    hwaddr offset = addr - USART2_BASE_ADDR;
    uint32_t val = usart2_read_reg(s, (uint32_t)offset);

    (void)size;
    return (uint64_t)val;
}

// This function will emulate all device writes
void usart2_write(void *opaque, hwaddr addr, uint64_t value, unsigned size) {
    USART2State *s = (USART2State *)opaque;
    hwaddr offset = addr - USART2_BASE_ADDR;

    (void)size;
    usart2_write_reg(s, (uint32_t)offset, (uint32_t)value);
}

// MUST return pointer to state — framework passes it as opaque to _read/_write
void* usart2_init(ConfigSection* model_info) {
    (void)model_info;

    memset(&g_usart2, 0, sizeof(g_usart2));

    g_usart2.pty_fd = api_pty_fd_gen();
    g_usart2.txe_ready_ns = INT64_MAX;
    g_usart2.tc_ready_ns = INT64_MAX;

    g_usart2.tx_timer = qemu_plugin_timer_new_ns(usart2_tx_timer_cb, &g_usart2);
    g_usart2.rx_timer = qemu_plugin_timer_new_period_ns(usart2_rx_timer_cb, &g_usart2, USART2_RX_POLL_NS);

    /*
     * Reset-like state from trace:
     * SR initially reads as 0x0, not TXE/TC set yet.
     * CR1/CR2/CR3/BRR all start at 0.
     */
    g_usart2.sr = 0;
    g_usart2.dr = 0;
    g_usart2.brr = 0;
    g_usart2.cr1 = 0;
    g_usart2.cr2 = 0;
    g_usart2.cr3 = 0;
    g_usart2.gtpr = 0;

    dev_debug("USART2 model initialized with PTY backend");
    return &g_usart2;
}