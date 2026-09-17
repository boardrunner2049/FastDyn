// Device Model for USART2

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

// Inferred Register Functions:
// SR   (0x00): status register; TXE/TC always ready, RXNE/IDLE on host input
// DR   (0x04): transmit/receive data register
// BRR  (0x08): baud rate register
// CR1  (0x0C): USART enable / transmitter enable / receiver enable
// CR2  (0x10): control register 2
// CR3  (0x14): control register 3
// GTPR (0x18): guard time / prescaler register

#define USART2_BASE        0x40004400ULL

#define USART_REG_SR       0x00
#define USART_REG_DR       0x04
#define USART_REG_BRR      0x08
#define USART_REG_CR1      0x0C
#define USART_REG_CR2      0x10
#define USART_REG_CR3      0x14
#define USART_REG_GTPR     0x18

// SR bits (STM32F1 USART)
#define USART_SR_PE        (1u << 0)
#define USART_SR_FE        (1u << 1)
#define USART_SR_NE        (1u << 2)
#define USART_SR_ORE       (1u << 3)
#define USART_SR_IDLE      (1u << 4)
#define USART_SR_RXNE      (1u << 5)
#define USART_SR_TC        (1u << 6)
#define USART_SR_TXE       (1u << 7)
#define USART_SR_LBD       (1u << 8)
#define USART_SR_CTS       (1u << 9)

// CR1 bits
#define USART_CR1_RE       (1u << 2)
#define USART_CR1_TE       (1u << 3)
#define USART_CR1_UE       (1u << 13)

#define USART2_RX_FIFO_SIZE 64

typedef struct {
    uint32_t brr;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cr3;
    uint32_t gtpr;

    uint32_t sr_sticky;
    uint16_t dr_last;

    uint8_t rx_fifo[USART2_RX_FIFO_SIZE];
    unsigned rx_head;
    unsigned rx_tail;
    unsigned rx_count;

    bool sr_read_latched;
    bool idle_flag;
    bool idle_ephemeral;

    int pty_fd;
} USART2State;

static USART2State g_usart2;

static uint32_t usart2_mask_by_size(uint32_t value, unsigned size)
{
    if (size == 1) {
        return value & 0xFFu;
    } else if (size == 2) {
        return value & 0xFFFFu;
    }
    return value;
}

static uint32_t usart2_merge_write(uint32_t oldv, uint64_t value, unsigned size)
{
    if (size == 1) {
        return (oldv & ~0xFFu) | ((uint32_t)value & 0xFFu);
    } else if (size == 2) {
        return (oldv & ~0xFFFFu) | ((uint32_t)value & 0xFFFFu);
    }
    return (uint32_t)value;
}

static void usart2_log_unknown(const char *op, uint64_t offset, uint64_t value, unsigned size)
{
    char msg[128];
    snprintf(msg, sizeof(msg),
             "USART2: unknown %s offset=0x%llx value=0x%llx size=%u",
             op,
             (unsigned long long)offset,
             (unsigned long long)value,
             size);
    dev_debug(msg);
}

static bool usart2_enabled_rx(USART2State *s)
{
    return (s->cr1 & USART_CR1_UE) && (s->cr1 & USART_CR1_RE);
}

static bool usart2_enabled_tx(USART2State *s)
{
    return (s->cr1 & USART_CR1_UE) && (s->cr1 & USART_CR1_TE);
}

static void usart2_rx_push(USART2State *s, uint8_t byte)
{
    if (s->rx_count >= USART2_RX_FIFO_SIZE) {
        s->sr_sticky |= USART_SR_ORE;
        return;
    }

    if (s->rx_count == 0) {
        // First byte after idle: allow SR to show RXNE|IDLE together (matches rare trace values).
        s->idle_flag = true;
        s->idle_ephemeral = false;
    }

    s->rx_fifo[s->rx_tail] = byte;
    s->rx_tail = (s->rx_tail + 1) % USART2_RX_FIFO_SIZE;
    s->rx_count++;
}

static bool usart2_rx_pop(USART2State *s, uint8_t *out)
{
    if (s->rx_count == 0) {
        return false;
    }

    *out = s->rx_fifo[s->rx_head];
    s->rx_head = (s->rx_head + 1) % USART2_RX_FIFO_SIZE;
    s->rx_count--;
    return true;
}

static void usart2_poll_host_rx(USART2State *s)
{
    uint8_t ch;

    if (!usart2_enabled_rx(s)) {
        return;
    }
    if (s->pty_fd < 0) {
        return;
    }

    while (s->rx_count < USART2_RX_FIFO_SIZE) {
        int rc = api_pty_read_nonblock(s->pty_fd, &ch);
        if (rc <= 0) {
            break;
        }
        usart2_rx_push(s, ch);
    }
}

static uint32_t usart2_compute_sr(USART2State *s)
{
    uint32_t sr = USART_SR_TXE | USART_SR_TC | s->sr_sticky;

    if (s->rx_count > 0) {
        sr |= USART_SR_RXNE;
    }
    if (s->idle_flag) {
        sr |= USART_SR_IDLE;
    }

    return sr;
}

static void usart2_clear_rx_side_effects(USART2State *s)
{
    // Approximate STM32 "read SR then read DR" clear behavior for receive-side flags.
    s->sr_sticky &= ~(USART_SR_ORE | USART_SR_NE | USART_SR_FE | USART_SR_PE);
    s->idle_flag = false;
    s->idle_ephemeral = false;
}

// This function will emulate all device reads
uint64_t usart2_read(void *opaque, hwaddr addr, unsigned size)
{
    USART2State *s = (USART2State *)opaque;
    hwaddr offset = addr - USART2_BASE;

    usart2_poll_host_rx(s);

    switch (offset) {
    case USART_REG_SR: {
        uint32_t sr = usart2_compute_sr(s);
        s->sr_read_latched = true;

        // One-shot idle after the last byte was drained, so status can briefly show 0xD0
        // and then return to the dominant idle-ready state 0xC0.
        if (s->idle_ephemeral && s->rx_count == 0) {
            s->idle_flag = false;
            s->idle_ephemeral = false;
        }

        return usart2_mask_by_size(sr, size);
    }

    case USART_REG_DR: {
        uint8_t byte = 0;
        bool had_data = usart2_rx_pop(s, &byte);

        if (had_data) {
            s->dr_last = byte;
        }

        if (s->sr_read_latched) {
            usart2_clear_rx_side_effects(s);
        }
        s->sr_read_latched = false;

        if (had_data && s->rx_count == 0) {
            // Model line returning idle after last pending byte is consumed.
            s->idle_flag = true;
            s->idle_ephemeral = true;
        }

        return usart2_mask_by_size((uint32_t)(s->dr_last & 0x1FFu), size);
    }

    case USART_REG_BRR:
        return usart2_mask_by_size(s->brr, size);

    case USART_REG_CR1:
        return usart2_mask_by_size(s->cr1, size);

    case USART_REG_CR2:
        return usart2_mask_by_size(s->cr2, size);

    case USART_REG_CR3:
        return usart2_mask_by_size(s->cr3, size);

    case USART_REG_GTPR:
        return usart2_mask_by_size(s->gtpr, size);

    default:
        usart2_log_unknown("read", (uint64_t)offset, 0, size);
        return 0;
    }
}

// This function will emulate all device writes
void usart2_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    USART2State *s = (USART2State *)opaque;
    hwaddr offset = addr - USART2_BASE;

    switch (offset) {
    case USART_REG_SR:
        // Most SR bits are hardware-owned. Keep this minimal and only allow software
        // to clear sticky receive-side status bits by writing 0s into them.
        if (size == 4 || size == 2 || size == 1) {
            uint32_t v = usart2_mask_by_size((uint32_t)value, size);
            if ((v & USART_SR_ORE) == 0) {
                s->sr_sticky &= ~USART_SR_ORE;
            }
            if ((v & USART_SR_IDLE) == 0) {
                s->idle_flag = false;
                s->idle_ephemeral = false;
            }
        }
        return;

    case USART_REG_DR: {
        uint32_t dr = usart2_mask_by_size((uint32_t)value, size) & 0x1FFu;
        s->dr_last = (uint16_t)dr;

        // Writing DR after polling SR is the common transmit pattern in the trace.
        // TXE/TC stay ready in observed behavior, so transmit is modeled as immediate.
        if (usart2_enabled_tx(s) && s->pty_fd >= 0) {
            api_pty_write_req(s->pty_fd, (uint8_t)(dr & 0xFFu));
        }
        return;
    }

    case USART_REG_BRR:
        s->brr = usart2_merge_write(s->brr, value, size);
        return;

    case USART_REG_CR1: {
        uint32_t newv = usart2_merge_write(s->cr1, value, size);
        s->cr1 = newv & 0x3FFFu; // valid implemented bit range for STM32F1 CR1

        if (!usart2_enabled_rx(s)) {
            s->rx_head = 0;
            s->rx_tail = 0;
            s->rx_count = 0;
            s->idle_flag = false;
            s->idle_ephemeral = false;
            s->sr_read_latched = false;
        }
        return;
    }

    case USART_REG_CR2:
        s->cr2 = usart2_merge_write(s->cr2, value, size);
        return;

    case USART_REG_CR3:
        s->cr3 = usart2_merge_write(s->cr3, value, size);
        return;

    case USART_REG_GTPR:
        s->gtpr = usart2_merge_write(s->gtpr, value, size);
        return;

    default:
        usart2_log_unknown("write", (uint64_t)offset, value, size);
        return;
    }
}

// MUST return pointer to state — framework passes it as opaque to _read/_write
void* usart2_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_usart2, 0, sizeof(g_usart2));

    // Observed reset/runtime-ready behavior:
    // firmware initially reads CR1/CR2/CR3 as zero, and polls SR seeing 0xC0.
    g_usart2.sr_sticky = 0;
    g_usart2.dr_last = 0;
    g_usart2.brr = 0;
    g_usart2.cr1 = 0;
    g_usart2.cr2 = 0;
    g_usart2.cr3 = 0;
    g_usart2.gtpr = 0;
    g_usart2.idle_flag = false;
    g_usart2.idle_ephemeral = false;
    g_usart2.sr_read_latched = false;

    g_usart2.pty_fd = api_pty_fd_gen("usart2");
    if (g_usart2.pty_fd < 0) {
        char msg[96];
        snprintf(msg, sizeof(msg), "USART2: failed to create PTY backend");
        dev_debug(msg);
    }

    return &g_usart2;
}