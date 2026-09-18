// Device Model for USART3

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define USART3_BASE 0x40004800ULL

#define USART_CR1_OFFSET   0x00
#define USART_CR2_OFFSET   0x04
#define USART_CR3_OFFSET   0x08
#define USART_BRR_OFFSET   0x0C
#define USART_GTPR_OFFSET  0x10
#define USART_RTOR_OFFSET  0x14
#define USART_RQR_OFFSET   0x18
#define USART_ISR_OFFSET   0x1C
#define USART_ICR_OFFSET   0x20
#define USART_RDR_OFFSET   0x24
#define USART_TDR_OFFSET   0x28
#define USART_PRESC_OFFSET 0x2C

/* CR1 bits used */
#define USART_CR1_UE                 (1u << 0)
#define USART_CR1_RE                 (1u << 2)
#define USART_CR1_TE                 (1u << 3)
#define USART_CR1_RXNEIE_RXFNEIE     (1u << 5)
#define USART_CR1_TCIE               (1u << 6)
#define USART_CR1_TXEIE_TXFNFIE      (1u << 7)
#define USART_CR1_FIFOEN             (1u << 29)

/* RQR bits used */
#define USART_RQR_RXFRQ              (1u << 3)

/* ISR bits used */
#define USART_ISR_PE                 (1u << 0)
#define USART_ISR_FE                 (1u << 1)
#define USART_ISR_NE                 (1u << 2)
#define USART_ISR_ORE                (1u << 3)
#define USART_ISR_IDLE               (1u << 4)
#define USART_ISR_RXNE_RXFNE         (1u << 5)
#define USART_ISR_TC                 (1u << 6)
#define USART_ISR_TXE_TXFNF          (1u << 7)
#define USART_ISR_EOBF               (1u << 12)
#define USART_ISR_BUSY               (1u << 17)
#define USART_ISR_TEACK              (1u << 21)
#define USART_ISR_REACK              (1u << 22)
#define USART_ISR_TXFE               (1u << 23)
#define USART_ISR_RXFT               (1u << 26)
#define USART_ISR_TXFT               (1u << 27)

/* ICR bits used */
#define USART_ICR_PECF               (1u << 0)
#define USART_ICR_FECF               (1u << 1)
#define USART_ICR_NCF                (1u << 2)
#define USART_ICR_ORECF              (1u << 3)
#define USART_ICR_IDLECF             (1u << 4)
#define USART_ICR_TCCF               (1u << 6)
#define USART_ICR_EOBCF              (1u << 12)

#define USART3_IRQ_NUM               39
#define USART3_RX_FIFO_SIZE          64
#define USART3_RX_FIFO_THRESHOLD     2u
#define USART3_RX_POLL_PERIOD_NS     1000000ULL
#define USART3_TX_COMPLETE_DELAY_NS  1000ULL

typedef struct {
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cr3;
    uint32_t brr;
    uint32_t gtpr;
    uint32_t rtor;
    uint32_t presc;

    uint32_t isr_latched;
    bool fifo_activity_flag;

    bool txe_flag;
    bool tc_flag;
    bool tx_busy;
    uint64_t tx_done_ns;

    uint8_t rx_fifo[USART3_RX_FIFO_SIZE];
    uint32_t rx_head;
    uint32_t rx_tail;
    uint32_t rx_count;

    uint8_t last_tdr;
    uint8_t last_rdr;

    int pty_fd;
    int irq_num;
    bool irq_level;

    uint64_t rx_timer;
    uint64_t tx_timer;
} USART3State;

static USART3State g_usart3;

static uint32_t usart3_merge_reg_write(uint32_t oldv, uint64_t value, unsigned size)
{
    switch (size) {
    case 1:
        return (oldv & ~0xFFu) | ((uint32_t)value & 0xFFu);
    case 2:
        return (oldv & ~0xFFFFu) | ((uint32_t)value & 0xFFFFu);
    default:
        return (uint32_t)value;
    }
}

static bool usart3_enabled_rx(USART3State *s)
{
    return ((s->cr1 & USART_CR1_UE) && (s->cr1 & USART_CR1_RE));
}

static bool usart3_enabled_tx(USART3State *s)
{
    return ((s->cr1 & USART_CR1_UE) && (s->cr1 & USART_CR1_TE));
}

static bool usart3_fifo_enabled(USART3State *s)
{
    return ((s->cr1 & USART_CR1_FIFOEN) != 0);
}

static void usart3_rx_push(USART3State *s, uint8_t v)
{
    if (s->rx_count >= USART3_RX_FIFO_SIZE) {
        s->isr_latched |= USART_ISR_ORE;
        return;
    }

    s->rx_fifo[s->rx_tail] = v;
    s->rx_tail = (s->rx_tail + 1) % USART3_RX_FIFO_SIZE;
    s->rx_count++;
    s->fifo_activity_flag = true;
}

static bool usart3_rx_pop(USART3State *s, uint8_t *out)
{
    if (s->rx_count == 0) {
        return false;
    }

    *out = s->rx_fifo[s->rx_head];
    s->rx_head = (s->rx_head + 1) % USART3_RX_FIFO_SIZE;
    s->rx_count--;
    return true;
}

static void usart3_rx_flush(USART3State *s)
{
    s->rx_head = 0;
    s->rx_tail = 0;
    s->rx_count = 0;
    s->fifo_activity_flag = false;
}

static void usart3_maybe_raise_irq(USART3State *s);

static void usart3_update_tx_state(USART3State *s)
{
    if (!s->tx_busy) {
        return;
    }

    {
        int64_t now = qemu_plugin_get_virtual_timer();
        if (now >= 0 && (uint64_t)now >= s->tx_done_ns) {
            s->tx_busy = false;
            s->txe_flag = true;
            s->tc_flag = true;
        }
    }
}

static void usart3_poll_host_rx(USART3State *s)
{
    if (s->pty_fd < 0) {
        return;
    }

    while (s->rx_count < USART3_RX_FIFO_SIZE) {
        uint8_t ch = 0;
        int rc = api_pty_read_nonblock(s->pty_fd, &ch);
        if (rc <= 0) {
            break;
        }
        usart3_rx_push(s, ch);
    }
}

static uint32_t usart3_get_isr(USART3State *s)
{
    uint32_t isr;
    bool rx_enabled;
    bool tx_enabled;
    bool fifo_enabled;

    usart3_poll_host_rx(s);
    usart3_update_tx_state(s);

    rx_enabled = usart3_enabled_rx(s);
    tx_enabled = usart3_enabled_tx(s);
    fifo_enabled = usart3_fifo_enabled(s);

    isr = s->isr_latched;

    /*
     * Hardware exposes an additional sticky status bit 12 once FIFO/RX traffic
     * has occurred. The trace shows it persisting across later polls until the
     * receiver state is explicitly cleared/reset.
     */
    if (fifo_enabled && s->fifo_activity_flag) {
        isr |= USART_ISR_EOBF;
    } else {
        isr &= ~USART_ISR_EOBF;
    }

    if (rx_enabled) {
        /*
         * On STM32H7 the line-idle condition is hardware-owned. The trace shows
         * IDLE asserted in the steady-state poll loop once UE/RE are enabled.
         */
        isr |= USART_ISR_IDLE;

        if (s->rx_count > 0) {
            isr |= USART_ISR_RXNE_RXFNE;
        } else {
            isr &= ~USART_ISR_RXNE_RXFNE;
        }

        if (fifo_enabled && s->rx_count >= USART3_RX_FIFO_THRESHOLD) {
            isr |= USART_ISR_RXFT;
        } else {
            isr &= ~USART_ISR_RXFT;
        }

        isr |= USART_ISR_REACK;
    } else {
        isr &= ~(USART_ISR_IDLE | USART_ISR_RXNE_RXFNE | USART_ISR_RXFT | USART_ISR_REACK);
    }

    if (s->txe_flag) {
        isr |= USART_ISR_TXE_TXFNF;
    } else {
        isr &= ~USART_ISR_TXE_TXFNF;
    }

    if (s->tc_flag) {
        isr |= USART_ISR_TC;
    } else {
        isr &= ~USART_ISR_TC;
    }

    if (tx_enabled) {
        isr |= USART_ISR_TEACK;
    } else {
        isr &= ~USART_ISR_TEACK;
    }

    if (tx_enabled && s->tx_busy) {
        isr |= USART_ISR_BUSY;
    } else {
        isr &= ~USART_ISR_BUSY;
    }

    if (fifo_enabled && tx_enabled) {
        /*
         * With FIFO mode enabled, the trace shows TXFE/TXFT asserted in the
         * idle state; this minimal model exposes them whenever TX is enabled.
         */
        isr |= (USART_ISR_TXFE | USART_ISR_TXFT);
    } else {
        isr &= ~(USART_ISR_TXFE | USART_ISR_TXFT);
    }

    return isr;
}

static void usart3_maybe_raise_irq(USART3State *s)
{
    uint32_t isr = usart3_get_isr(s);
    bool pending = false;

    if ((s->cr1 & USART_CR1_RXNEIE_RXFNEIE) && (isr & USART_ISR_RXNE_RXFNE)) {
        pending = true;
    }
    if ((s->cr1 & USART_CR1_TCIE) && (isr & USART_ISR_TC)) {
        pending = true;
    }
    if ((s->cr1 & USART_CR1_TXEIE_TXFNFIE) && (isr & USART_ISR_TXE_TXFNF)) {
        pending = true;
    }

    if (pending && !s->irq_level) {
        qemu_plugin_raise_irq(s->irq_num + 16, false);
    }

    s->irq_level = pending;
}

static void usart3_rx_timer_cb(void *opaque)
{
    USART3State *s = (USART3State *)opaque;
    usart3_poll_host_rx(s);
    usart3_update_tx_state(s);
    usart3_maybe_raise_irq(s);
}

static void usart3_tx_timer_cb(void *opaque)
{
    USART3State *s = (USART3State *)opaque;
    s->tx_busy = false;
    s->txe_flag = true;
    s->tc_flag = true;
    usart3_maybe_raise_irq(s);
}

static void usart3_log_bad_access(const char *op, hwaddr addr, unsigned size, uint64_t value)
{
    char buf[128];
    snprintf(buf, sizeof(buf),
             "USART3 %s unknown access addr=0x%llx size=%u value=0x%llx",
             op,
             (unsigned long long)addr,
             size,
             (unsigned long long)value);
    dev_debug(buf);
}

// This function will emulate all device reads
uint64_t usart3_read(void *opaque, hwaddr addr, unsigned size)
{
    USART3State *s = (USART3State *)opaque;
    hwaddr offset = addr - USART3_BASE;

    (void)size;

    switch (offset) {
    case USART_CR1_OFFSET:
        return s->cr1;
    case USART_CR2_OFFSET:
        return s->cr2;
    case USART_CR3_OFFSET:
        return s->cr3;
    case USART_BRR_OFFSET:
        return s->brr;
    case USART_GTPR_OFFSET:
        return s->gtpr;
    case USART_RTOR_OFFSET:
        return s->rtor;
    case USART_RQR_OFFSET:
        return 0;
    case USART_ISR_OFFSET:
        return usart3_get_isr(s);
    case USART_ICR_OFFSET:
        return 0;
    case USART_RDR_OFFSET: {
        uint8_t ch = 0;

        usart3_poll_host_rx(s);

        if (!usart3_enabled_rx(s)) {
            return 0;
        }

        if (usart3_rx_pop(s, &ch)) {
            s->last_rdr = ch;
            usart3_maybe_raise_irq(s);
            return (uint32_t)ch;
        }

        return 0;
    }
    case USART_TDR_OFFSET:
        return (uint32_t)s->last_tdr;
    case USART_PRESC_OFFSET:
        return s->presc;
    default:
        usart3_log_bad_access("read", addr, size, 0);
        return 0;
    }
}

// This function will emulate all device writes
void usart3_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    USART3State *s = (USART3State *)opaque;
    hwaddr offset = addr - USART3_BASE;

    switch (offset) {
    case USART_CR1_OFFSET:
        s->cr1 = usart3_merge_reg_write(s->cr1, value, size);
        usart3_maybe_raise_irq(s);
        break;

    case USART_CR2_OFFSET:
        s->cr2 = usart3_merge_reg_write(s->cr2, value, size);
        break;

    case USART_CR3_OFFSET:
        s->cr3 = usart3_merge_reg_write(s->cr3, value, size);
        break;

    case USART_BRR_OFFSET:
        s->brr = usart3_merge_reg_write(s->brr, value, size);
        break;

    case USART_GTPR_OFFSET:
        s->gtpr = usart3_merge_reg_write(s->gtpr, value, size);
        break;

    case USART_RTOR_OFFSET:
        s->rtor = usart3_merge_reg_write(s->rtor, value, size);
        break;

    case USART_RQR_OFFSET: {
        uint32_t v = (uint32_t)value;

        if (v & USART_RQR_RXFRQ) {
            usart3_rx_flush(s);
        }

        usart3_maybe_raise_irq(s);
        break;
    }

    case USART_ISR_OFFSET:
        /* Read-only in this minimal model. Ignore writes. */
        break;

    case USART_ICR_OFFSET: {
        uint32_t v = (uint32_t)value;

        if (v & USART_ICR_PECF) {
            s->isr_latched &= ~USART_ISR_PE;
        }
        if (v & USART_ICR_FECF) {
            s->isr_latched &= ~USART_ISR_FE;
        }
        if (v & USART_ICR_NCF) {
            s->isr_latched &= ~USART_ISR_NE;
        }
        if (v & USART_ICR_ORECF) {
            s->isr_latched &= ~USART_ISR_ORE;
        }
        if (v & USART_ICR_IDLECF) {
            s->isr_latched &= ~USART_ISR_IDLE;
        }
        if (v & USART_ICR_TCCF) {
            s->tc_flag = false;
        }
        if (v & USART_ICR_EOBCF) {
            s->fifo_activity_flag = false;
        }

        usart3_maybe_raise_irq(s);
        break;
    }

    case USART_RDR_OFFSET:
        /* Read-only. Ignore writes. */
        break;

    case USART_TDR_OFFSET: {
        uint8_t ch = (uint8_t)(value & 0xFFu);
        s->last_tdr = ch;

        if (usart3_enabled_tx(s)) {
            int64_t now = qemu_plugin_get_virtual_timer();
            uint64_t base_now = (now < 0) ? 0 : (uint64_t)now;

            api_pty_write_req(s->pty_fd, ch);

            /*
             * Even in FIFO mode the hardware briefly reports activity (BUSY)
             * after a write. However TXFNF/TC remain asserted in the observed
             * trace, so only the busy lifetime is modeled there.
             */
            s->tx_busy = true;
            s->tx_done_ns = base_now + USART3_TX_COMPLETE_DELAY_NS;
            qemu_plugin_timer_alarm(s->tx_timer, s->tx_done_ns);

            if (usart3_fifo_enabled(s)) {
                s->txe_flag = true;
                s->tc_flag = true;
            } else {
                s->txe_flag = false;
                s->tc_flag = false;
            }
        }

        usart3_maybe_raise_irq(s);
        break;
    }

    case USART_PRESC_OFFSET:
        s->presc = usart3_merge_reg_write(s->presc, value, size);
        break;

    default:
        usart3_log_bad_access("write", addr, size, value);
        break;
    }
}

// MUST return pointer to state — framework passes it as opaque to _read/_write
void* usart3_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_usart3, 0, sizeof(g_usart3));

    g_usart3.pty_fd = api_pty_fd_gen();
    g_usart3.irq_num = USART3_IRQ_NUM;

    /* USART reset-like defaults useful for firmware bring-up */
    g_usart3.cr1 = 0x00000000;
    g_usart3.cr2 = 0x00000000;
    g_usart3.cr3 = 0x00000000;
    g_usart3.brr = 0x00000000;
    g_usart3.gtpr = 0x00000000;
    g_usart3.rtor = 0x00000000;
    g_usart3.presc = 0x00000000;
    g_usart3.isr_latched = 0x00000000;

    g_usart3.txe_flag = true;
    g_usart3.tc_flag = true;
    g_usart3.tx_busy = false;
    g_usart3.tx_done_ns = 0;
    g_usart3.irq_level = false;

    g_usart3.tx_timer = qemu_plugin_timer_new_ns(usart3_tx_timer_cb, &g_usart3);
    g_usart3.rx_timer = qemu_plugin_timer_new_period_ns(
        usart3_rx_timer_cb, &g_usart3, USART3_RX_POLL_PERIOD_NS);

    return &g_usart3;
}