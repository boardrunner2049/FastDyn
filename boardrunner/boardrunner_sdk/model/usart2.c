#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <device.h>
#include <boardrunner/vio.h>

#define USART2_BASE         0x40004400ULL

#define USART_SR_OFF        0x00
#define USART_DR_OFF        0x04
#define USART_BRR_OFF       0x08
#define USART_CR1_OFF       0x0C
#define USART_CR2_OFF       0x10
#define USART_CR3_OFF       0x14

#define USART_SR_RXNE       (1U << 5)
#define USART_SR_TC         (1U << 6)
#define USART_SR_TXE        (1U << 7)

#define USART_CR1_RE        (1U << 2)
#define USART_CR1_TE        (1U << 3)
#define USART_CR1_TCIE      (1U << 6)
#define USART_CR1_TXEIE     (1U << 7)
#define USART_CR1_UE        (1U << 13)

/* STM32F427 NVIC position 38 = USART2. qemu_plugin_raise_irq() expects
 * (interrupt_line + 16) per the plugin API convention. */
#define USART2_IRQ_LINE     (38U + 16U)

#define USART2_RXQ_SIZE     256
#define USART2_POLL_NS      1000000ULL

typedef struct {
    uint32_t sr;
    uint32_t dr;
    uint32_t brr;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cr3;
    int pty_fd;
    uint64_t poll_timer;
    uint8_t rxq[USART2_RXQ_SIZE];
    unsigned rxq_head;
    unsigned rxq_tail;
    unsigned rxq_count;
} USART2State;

static USART2State g_usart2;

static void usart2_ensure_pty(USART2State *s) {
    if (s->pty_fd < 0) {
        s->pty_fd = api_pty_fd_gen("usart2");
    }
}

/* Raise the USART2 IRQ if the firmware has enabled a TX-side interrupt and
 * the corresponding status bit is asserted. ChibiOS's SerialDriver ISR reads
 * the next byte from its output queue and writes DR — which we forward to
 * the PTY — then re-raises TXEIE if more bytes remain, or clears TXEIE when
 * the queue drains. */
static void usart2_maybe_raise_tx_irq(USART2State *s) {
    if ((s->cr1 & USART_CR1_UE) == 0U) {
        return;
    }
    if ((s->cr1 & USART_CR1_TE) == 0U) {
        return;
    }
    if ((s->cr1 & USART_CR1_TXEIE) != 0U && (s->sr & USART_SR_TXE) != 0U) {
        qemu_plugin_raise_irq(USART2_IRQ_LINE, false);
        return;
    }
    if ((s->cr1 & USART_CR1_TCIE) != 0U && (s->sr & USART_SR_TC) != 0U) {
        qemu_plugin_raise_irq(USART2_IRQ_LINE, false);
    }
}

static bool usart2_rxq_push(USART2State *s, uint8_t v) {
    if (s->rxq_count >= USART2_RXQ_SIZE) {
        return false;
    }
    s->rxq[s->rxq_head] = v;
    s->rxq_head = (s->rxq_head + 1U) % USART2_RXQ_SIZE;
    s->rxq_count++;
    return true;
}

static bool usart2_rxq_pop(USART2State *s, uint8_t *out) {
    if (s->rxq_count == 0U) {
        return false;
    }
    *out = s->rxq[s->rxq_tail];
    s->rxq_tail = (s->rxq_tail + 1U) % USART2_RXQ_SIZE;
    s->rxq_count--;
    return true;
}

static void usart2_host_poll(USART2State *s) {
    uint8_t ch;
    int rc;

    usart2_ensure_pty(s);
    if (s->pty_fd < 0) {
        return;
    }

    while (1) {
        rc = api_pty_read_nonblock(s->pty_fd, &ch);
        if (rc <= 0) {
            break;
        }
        usart2_rxq_push(s, ch);
    }
}

static void usart2_drain_rx(USART2State *s) {
    uint8_t ch;

    if ((s->cr1 & USART_CR1_UE) == 0U || (s->cr1 & USART_CR1_RE) == 0U) {
        return;
    }

    if ((s->sr & USART_SR_RXNE) != 0U) {
        return;
    }

    if (usart2_rxq_pop(s, &ch)) {
        s->dr = ch;
        s->sr |= USART_SR_RXNE;
    }
}

static void usart2_transmit_byte(USART2State *s, uint8_t ch) {
    s->dr = ch;

    if ((s->cr1 & USART_CR1_UE) != 0U && (s->cr1 & USART_CR1_TE) != 0U) {
        if (s->pty_fd >= 0) {
            api_pty_write_req(s->pty_fd, ch);
        }
    }
    /* PTY forward is synchronous, so the shift register is immediately empty
     * again. Signal TXE|TC and re-fire the ISR to drain the next queued byte. */
    s->sr |= (USART_SR_TXE | USART_SR_TC);
    usart2_maybe_raise_tx_irq(s);
}

static void usart2_poll_cb(void *opaque) {
    USART2State *s = (USART2State *)opaque;
    usart2_host_poll(s);
    usart2_drain_rx(s);
    /* Recover if the TX ISR chain ever stalls between register writes. */
    usart2_maybe_raise_tx_irq(s);
}

void* usart2_init(ConfigSection* model_info) {
    (void)model_info;

    memset(&g_usart2, 0, sizeof(g_usart2));
    /* STM32 USART reset state has the transmit register empty. Without this,
     * ChibiOS's first TXEIE-enable sees TXE=0 and never gets an ISR fired. */
    g_usart2.sr = USART_SR_TXE | USART_SR_TC;
    g_usart2.pty_fd = api_pty_fd_gen("usart2");
    g_usart2.poll_timer = qemu_plugin_timer_new_period_ns(usart2_poll_cb, &g_usart2, USART2_POLL_NS);
    dev_debug("usart2: initialized with stateful USART registers and PTY backend\n");
    return &g_usart2;
}

uint64_t usart2_read(void *opaque, uint64_t addr, unsigned size) {
    USART2State *s = (USART2State *)opaque;
    uint64_t offset = addr - USART2_BASE;
    uint64_t ret = 0;

    (void)size;

    usart2_host_poll(s);
    usart2_drain_rx(s);

    switch (offset) {
    case USART_SR_OFF:
        ret = s->sr;
        break;
    case USART_DR_OFF:
        ret = s->dr & 0xFFU;
        s->sr &= ~USART_SR_RXNE;
        s->dr = 0;
        usart2_drain_rx(s);
        break;
    case USART_BRR_OFF:
        ret = s->brr;
        break;
    case USART_CR1_OFF:
        ret = s->cr1;
        break;
    case USART_CR2_OFF:
        ret = s->cr2;
        break;
    case USART_CR3_OFF:
        ret = s->cr3;
        break;
    default:
        ret = 0;
        break;
    }

    return ret;
}

void usart2_write(void *opaque, uint64_t addr, uint64_t value, unsigned size) {
    USART2State *s = (USART2State *)opaque;
    uint64_t offset = addr - USART2_BASE;
    uint8_t ch;

    (void)size;

    switch (offset) {
    case USART_SR_OFF: {
        /* STM32F4 SR write semantics: TXE is read-only (HW-managed, cleared
         * only by a DR write); TC/LBD/CTS are cleared by writing 0 to them;
         * RXNE/IDLE/ORE/NF/FE/PE are cleared by the read-SR + read-DR
         * sequence. A blind s->sr = value would wipe TXE and permanently
         * stall ISR-driven TX (the ISR gate needs TXE=1). */
        uint32_t w0c_mask = USART_SR_TC | (1U << 8) | (1U << 9); /* TC, LBD, CTS */
        uint32_t vw = (uint32_t)value;
        s->sr = (s->sr & ~w0c_mask) | (s->sr & w0c_mask & vw);
        break;
    }
    case USART_DR_OFF:
        ch = (uint8_t)(value & 0xFFU);
        usart2_transmit_byte(s, ch);
        break;
    case USART_BRR_OFF:
        s->brr = (uint32_t)value;
        break;
    case USART_CR1_OFF:
        s->cr1 = (uint32_t)value;
        usart2_drain_rx(s);
        usart2_maybe_raise_tx_irq(s);
        break;
    case USART_CR2_OFF:
        s->cr2 = (uint32_t)value;
        break;
    case USART_CR3_OFF:
        s->cr3 = (uint32_t)value;
        break;
    default:
        break;
    }
}
