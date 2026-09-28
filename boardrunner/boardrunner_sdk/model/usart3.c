#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <device.h>
#include <boardrunner/vio.h>

#define USART3_BASE         0x40004800ULL

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

/* STM32F427 NVIC position 39 = USART3. */
#define USART3_IRQ_LINE     (39U + 16U)

#define USART3_RXQ_SIZE     256
#define USART3_POLL_NS      1000000ULL

typedef struct {
    uint32_t sr;
    uint32_t dr;
    uint32_t brr;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cr3;
    int pty_fd;
    uint64_t poll_timer;
    uint8_t rxq[USART3_RXQ_SIZE];
    unsigned rxq_head;
    unsigned rxq_tail;
    unsigned rxq_count;
} USART3State;

static USART3State g_usart3;

static void usart3_ensure_pty(USART3State *s) {
    if (s->pty_fd < 0) {
        s->pty_fd = api_pty_fd_gen("usart3");
    }
}

static void usart3_maybe_raise_tx_irq(USART3State *s) {
    if ((s->cr1 & USART_CR1_UE) == 0U) {
        return;
    }
    if ((s->cr1 & USART_CR1_TE) == 0U) {
        return;
    }
    if ((s->cr1 & USART_CR1_TXEIE) != 0U && (s->sr & USART_SR_TXE) != 0U) {
        qemu_plugin_raise_irq(USART3_IRQ_LINE, false);
        return;
    }
    if ((s->cr1 & USART_CR1_TCIE) != 0U && (s->sr & USART_SR_TC) != 0U) {
        qemu_plugin_raise_irq(USART3_IRQ_LINE, false);
    }
}

static bool usart3_rxq_push(USART3State *s, uint8_t v) {
    if (s->rxq_count >= USART3_RXQ_SIZE) {
        return false;
    }
    s->rxq[s->rxq_head] = v;
    s->rxq_head = (s->rxq_head + 1U) % USART3_RXQ_SIZE;
    s->rxq_count++;
    return true;
}

static bool usart3_rxq_pop(USART3State *s, uint8_t *out) {
    if (s->rxq_count == 0U) {
        return false;
    }
    *out = s->rxq[s->rxq_tail];
    s->rxq_tail = (s->rxq_tail + 1U) % USART3_RXQ_SIZE;
    s->rxq_count--;
    return true;
}

static void usart3_host_poll(USART3State *s) {
    uint8_t ch;
    int rc;

    usart3_ensure_pty(s);
    if (s->pty_fd < 0) {
        return;
    }

    while (1) {
        rc = api_pty_read_nonblock(s->pty_fd, &ch);
        if (rc <= 0) {
            break;
        }
        usart3_rxq_push(s, ch);
    }
}

static void usart3_drain_rx(USART3State *s) {
    uint8_t ch;

    if ((s->cr1 & USART_CR1_UE) == 0U || (s->cr1 & USART_CR1_RE) == 0U) {
        return;
    }

    if ((s->sr & USART_SR_RXNE) != 0U) {
        return;
    }

    if (usart3_rxq_pop(s, &ch)) {
        s->dr = ch;
        s->sr |= USART_SR_RXNE;
    }
}

static void usart3_transmit_byte(USART3State *s, uint8_t ch) {
    s->dr = ch;

    if ((s->cr1 & USART_CR1_UE) != 0U && (s->cr1 & USART_CR1_TE) != 0U) {
        usart3_ensure_pty(s);
        if (s->pty_fd >= 0) {
            api_pty_write_req(s->pty_fd, ch);
        }
    }

    s->sr |= (USART_SR_TXE | USART_SR_TC);
    usart3_maybe_raise_tx_irq(s);
}

static void usart3_poll_cb(void *opaque) {
    USART3State *s = (USART3State *)opaque;
    usart3_host_poll(s);
    usart3_drain_rx(s);
    usart3_maybe_raise_tx_irq(s);
}

void* usart3_init(ConfigSection* model_info) {
    (void)model_info;

    memset(&g_usart3, 0, sizeof(g_usart3));
    g_usart3.sr = USART_SR_TXE | USART_SR_TC;
    g_usart3.pty_fd = api_pty_fd_gen("usart3");
    g_usart3.poll_timer = qemu_plugin_timer_new_period_ns(usart3_poll_cb, &g_usart3, USART3_POLL_NS);
    dev_debug("usart3: initialized with stateful USART registers and PTY backend\n");
    return &g_usart3;
}

uint64_t usart3_read(void *opaque, uint64_t addr, unsigned size) {
    USART3State *s = (USART3State *)opaque;
    uint64_t offset = addr - USART3_BASE;
    uint64_t ret = 0;

    (void)size;

    usart3_host_poll(s);
    usart3_drain_rx(s);

    switch (offset) {
    case USART_SR_OFF:
        ret = s->sr;
        break;
    case USART_DR_OFF:
        ret = s->dr & 0xFFU;
        s->sr &= ~USART_SR_RXNE;
        s->dr = 0;
        usart3_drain_rx(s);
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

void usart3_write(void *opaque, uint64_t addr, uint64_t value, unsigned size) {
    USART3State *s = (USART3State *)opaque;
    uint64_t offset = addr - USART3_BASE;
    uint8_t ch;

    (void)size;

    switch (offset) {
    case USART_SR_OFF: {
        /* See usart2.c for full rationale. TXE is HW-managed and must NOT
         * be cleared by firmware SR writes, otherwise usart3_maybe_raise_tx_irq
         * refuses to raise IRQ 39 and ChibiOS's TX ISR never runs. */
        uint32_t w0c_mask = USART_SR_TC | (1U << 8) | (1U << 9); /* TC, LBD, CTS */
        uint32_t vw = (uint32_t)value;
        s->sr = (s->sr & ~w0c_mask) | (s->sr & w0c_mask & vw);
        break;
    }
    case USART_DR_OFF:
        ch = (uint8_t)(value & 0xFFU);
        usart3_transmit_byte(s, ch);
        break;
    case USART_BRR_OFF:
        s->brr = (uint32_t)value;
        break;
    case USART_CR1_OFF:
        s->cr1 = (uint32_t)value;
        usart3_drain_rx(s);
        usart3_maybe_raise_tx_irq(s);
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
