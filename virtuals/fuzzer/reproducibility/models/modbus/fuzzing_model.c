// Device Model for USART2
#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <limits.h>

typedef void (*fuzz_callback_t)(void);
size_t fuzz_get_data(char *buf, size_t len);
uint32_t fuzz_get_register(int reg);
int fuzz_write_memory(unsigned long long addr, uint8_t *mem_buf, int len);
void fuzz_request_sync(uint32_t resume_pc);
void fuzz_set_register(uint32_t value, int reg);
void fuzz_set_data(char *buf, size_t len);
void fuzz_register_callback(fuzz_callback_t cb);
void fuzz_register_input_callback(fuzz_callback_t cb);
void fuzz_register_restore(fuzz_callback_t cb);
typedef void (*core_callback_t)(unsigned int cpu_index, void *userdata);
bool core_register_virtual_rule(uint64_t address, core_callback_t callback,
                                const char *args);

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
#define USART2_FUZZ_RX_BUF_LEN  1024u
#define USART2_TX_CAPTURE_LEN   256u
#define USART2_MODBUS_MAX_RTU_LEN 256u
#define USART2_RX_POLL_NS       1000000ULL  // 1 ms virtual-time periodic poll
#define USART2_RTU_INITIAL_GAP_NUM 1000ULL
#define USART2_RTU_INITIAL_GAP_DEN 2ULL

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

    uint8_t tx_capture_buf[USART2_TX_CAPTURE_LEN];
    unsigned tx_capture_len;

    bool sr_read_for_clear;
    bool irq_latched;

    int pty_fd;
    uint8_t fuzz_rx_buf[USART2_FUZZ_RX_BUF_LEN];
    unsigned fuzz_rx_len;
    unsigned fuzz_rx_pos;

    uint64_t tx_timer;
    uint64_t rx_timer;

    int64_t txe_ready_ns;
    int64_t tc_ready_ns;
    int64_t fuzz_rx_release_ns;
    unsigned txe_polls_remaining;
    unsigned tc_polls_remaining;
} USART2State;

static USART2State g_usart2;
static USART2State g_usart2_snapshot;
static bool g_usart2_snapshot_valid;
static bool g_usart2_fuzz_ready;
/* A testcase is a single RTU frame: never refill it from fuzz_get_data(). */
static bool g_usart2_fuzz_input_consumed;
/* Armed only after fuzz_activate_message(); keeps the startup run unchanged. */
static volatile uint8_t g_usart2_fuzz_irq_schedule_active;
/* Pending test input byte; unlike the flag above, never armed by TX setup. */
static volatile uint8_t g_usart2_fuzz_rx_pending;
/* Route only malformed requests through a handler so they complete a fuzz round. */
static volatile uint8_t g_usart2_fuzz_force_handler;
static volatile uint8_t g_usart2_fuzz_force_min_len;
/* Set while an AFLNet request awaits its complete RTU reply. */
static volatile uint8_t g_usart2_fuzz_response_pending;
/* Correct only the first post-snapshot RTU state dispatch. */
static volatile uint8_t g_usart2_force_first_rx_idle;
/* Values consumed by inline guest-register updates at UART load sites. */
static volatile uint32_t g_usart2_irq_sr;
static volatile uint32_t g_usart2_irq_dr;

static void usart2_poll_fuzz_rx(USART2State *s);

static uint32_t usart2_pop_dr(USART2State *s);

/* FreeMODBUS RTU globals in this fixed firmware image.  Snapshotting occurs
 * after initialization, but its prior receive state is not a transaction
 * boundary, so establish one before delivering each AFLNet request. */
static void usart2_reset_modbus_rtu_frame_state(void) {
    uint8_t rx_idle = 1U;
    uint16_t rx_length = 0U;

    (void)fuzz_write_memory(0x2000030CU, &rx_idle, (int)sizeof(rx_idle));
    (void)fuzz_write_memory(0x20000202U, (uint8_t *)&rx_length,
                            (int)sizeof(rx_length));
}


static uint64_t usart2_char_time_ns(const USART2State *s);

static inline int64_t usart2_now_ns(void) {
    return qemu_plugin_get_virtual_timer();
}

static inline void usart2_rx_fifo_reset(USART2State *s) {
    s->rx_head = 0;
    s->rx_tail = 0;
    s->rx_count = 0;
    s->dr = 0;
}

static inline void usart2_reset_fuzz_rx(USART2State *s) {
    s->fuzz_rx_len = 0U;
    s->fuzz_rx_pos = 0U;
}

static inline void usart2_arm_initial_fuzz_gap(USART2State *s) {
    uint64_t char_ns = usart2_char_time_ns(s);
    uint64_t gap_ns =
        (char_ns * USART2_RTU_INITIAL_GAP_NUM +
         (USART2_RTU_INITIAL_GAP_DEN - 1ULL)) /
        USART2_RTU_INITIAL_GAP_DEN;

    if (gap_ns < USART2_RX_POLL_NS) {
        gap_ns = USART2_RX_POLL_NS;
    }

    s->fuzz_rx_release_ns = usart2_now_ns() + (int64_t)gap_ns;
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

static uint16_t usart2_modbus_crc16(const uint8_t *buf, size_t len) {
    uint16_t crc = 0xFFFFu;

    for (size_t pos = 0; pos < len; pos++) {
        crc ^= buf[pos];

        for (int i = 0; i < 8; i++) {
            if (crc & 1u) {
                crc = (uint16_t)((crc >> 1) ^ 0xA001u);
            } else {
                crc >>= 1;
            }
        }
    }

    return crc;
}

static int usart2_modbus_crc_ok(const uint8_t *buf, size_t len) {
    uint16_t calc;
    uint16_t got;

    if (len < 4) {
        return 0;
    }

    calc = usart2_modbus_crc16(buf, len - 2);
    got = (uint16_t)buf[len - 2] | ((uint16_t)buf[len - 1] << 8);
    return calc == got;
}

static size_t usart2_modbus_request_len(const uint8_t *buf, size_t len) {
    size_t expected;

    if (len < 2U) {
        return len;
    }

    switch (buf[1]) {
    case 0x01: case 0x02: case 0x03: case 0x04:
    case 0x05: case 0x06:
        expected = 8U;
        break;
    case 0x07: case 0x11:
        expected = 4U;
        break;
    case 0x0f: case 0x10:
        if (len < 7U) return len;
        expected = 9U + (size_t)buf[6];
        break;
    case 0x17:
        if (len < 11U) return len;
        expected = 13U + (size_t)buf[10];
        break;
    default:
        return len;
    }

    return expected < len ? expected : len;
}

static int usart2_modbus_response_done(const uint8_t *buf,
                                       size_t len,
                                       size_t *expected_len_out) {
    size_t expected = 0;
    uint8_t func;

    if (expected_len_out != NULL) {
        *expected_len_out = 0;
    }

    if (len < 2) {
        return 0;
    }

    func = buf[1];
    if (func & 0x80u) {
        expected = 5;
    } else {
        switch (func) {
        case 0x01:
        case 0x02:
        case 0x03:
        case 0x04:
        case 0x17:
        case 0x11:
            if (len < 3) {
                return 0;
            }
            expected = 3u + (size_t)buf[2] + 2u;
            break;

        case 0x05:
        case 0x06:
        case 0x0F:
        case 0x10:
            expected = 8;
            break;

        case 0x07:
            expected = 5;
            break;

        default:
            return -1;
        }
    }

    if (expected_len_out != NULL) {
        *expected_len_out = expected;
    }

    if (expected > USART2_TX_CAPTURE_LEN) {
        return -1;
    }

    if (len < expected) {
        return 0;
    }

    if (!usart2_modbus_crc_ok(buf, expected)) {
        return -1;
    }

    return 1;
}

static void usart2_capture_fuzz_tx_byte(USART2State *s, uint8_t byte) {
    size_t expected_len = 0;
    int done;

    if (s->tx_capture_len >= USART2_TX_CAPTURE_LEN) {
        s->tx_capture_len = 0;
        return;
    }

    s->tx_capture_buf[s->tx_capture_len++] = byte;
    done = usart2_modbus_response_done(
        s->tx_capture_buf, s->tx_capture_len, &expected_len);

    if (done == 1) {
        fuzz_set_data((char *)s->tx_capture_buf, expected_len);

        if (s->tx_capture_len > expected_len) {
            memmove(s->tx_capture_buf,
                    s->tx_capture_buf + expected_len,
                    s->tx_capture_len - expected_len);
            s->tx_capture_len -= (unsigned)expected_len;
        } else {
            s->tx_capture_len = 0;
        }
    } else if (done < 0) {
        s->tx_capture_len = 0;
    }
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
    g_usart2_irq_sr = s->sr & 0x3FFu;
}

static void usart2_save_runtime_snapshot(void) {
    g_usart2_snapshot = g_usart2;
    usart2_reset_fuzz_rx(&g_usart2_snapshot);
    g_usart2_snapshot_valid = true;
}

static void usart2_restore_runtime_snapshot(void) {
    if (!g_usart2_snapshot_valid) {
        return;
    }

    g_usart2 = g_usart2_snapshot;
    usart2_reset_fuzz_rx(&g_usart2);

    if (g_usart2.txe_ready_ns != INT64_MAX ||
        g_usart2.tc_ready_ns != INT64_MAX) {
        usart2_arm_next_tx_timer(&g_usart2);
    }

    usart2_maybe_raise_irq(&g_usart2);
}

static void usart2_sync_hook(void) {
    g_usart2_fuzz_irq_schedule_active = 0;
    g_usart2_fuzz_rx_pending = 0;
    g_usart2_fuzz_force_handler = 0;
    g_usart2_fuzz_force_min_len = 0;
    g_usart2_fuzz_response_pending = 0;
    g_usart2_force_first_rx_idle = 0;
    g_usart2_fuzz_input_consumed = false;
    if (!g_usart2_snapshot_valid) {
        usart2_save_runtime_snapshot();
    }

    g_usart2_fuzz_ready = true;
    usart2_arm_initial_fuzz_gap(&g_usart2);
}

static void usart2_restore_hook(void) {
    g_usart2_fuzz_irq_schedule_active = 0;
    g_usart2_fuzz_rx_pending = 0;
    g_usart2_fuzz_force_handler = 0;
    g_usart2_fuzz_force_min_len = 0;
    g_usart2_fuzz_response_pending = 0;
    g_usart2_force_first_rx_idle = 0;
    g_usart2_fuzz_input_consumed = false;
    if (!g_usart2_snapshot_valid) {
        usart2_save_runtime_snapshot();
    } else {
        usart2_restore_runtime_snapshot();
    }

    g_usart2_fuzz_ready = true;
    usart2_arm_initial_fuzz_gap(&g_usart2);
}

/* A testcase is now active and the snapshot has restored the UART setup.
 * The per-frame RTU state below is RX_IDLE, equivalent to a completed T3.5
 * initial silence interval, so the first byte can be delivered immediately. */
static void usart2_input_hook(void) {
    g_usart2_fuzz_irq_schedule_active = 0;
    g_usart2_fuzz_rx_pending = 0;
    g_usart2_fuzz_force_handler = 0;
    g_usart2_fuzz_force_min_len = 0;
    g_usart2_fuzz_response_pending = 1;
    g_usart2_force_first_rx_idle = 0;
    g_usart2_fuzz_input_consumed = false;
    usart2_reset_fuzz_rx(&g_usart2);
    usart2_rx_fifo_reset(&g_usart2);
    g_usart2.sr &= ~(USART_SR_RXNE | USART_SR_ORE | USART_SR_IDLE);
    g_usart2.irq_latched = false;
    g_usart2_irq_sr = g_usart2.sr & 0x3FFu;
    g_usart2.fuzz_rx_len = (unsigned)fuzz_get_data(
        (char *)g_usart2.fuzz_rx_buf, sizeof(g_usart2.fuzz_rx_buf));
    /* eMBRTUReceive asserts when its fixed 256-byte receive buffer is
     * exceeded. AFLNet's fallback region can be much larger after havoc;
     * expose no more than one physical RTU frame and route it to the safe
     * compatibility handler below. */
    if (g_usart2.fuzz_rx_len > USART2_MODBUS_MAX_RTU_LEN) {
        g_usart2.fuzz_rx_len = USART2_MODBUS_MAX_RTU_LEN;
        g_usart2_fuzz_force_handler = 1U;
    }
    g_usart2_fuzz_force_min_len = g_usart2.fuzz_rx_len < 4U;
    g_usart2_fuzz_force_handler |= g_usart2_fuzz_force_min_len;
    if (g_usart2.fuzz_rx_len == 0U) {
        g_usart2.fuzz_rx_buf[0] = 10U;
        g_usart2.fuzz_rx_buf[1] = 1U;
        g_usart2.fuzz_rx_buf[2] = 0U;
        g_usart2.fuzz_rx_buf[3] = 0U;
        g_usart2.fuzz_rx_len = 4U;
    } else if (!g_usart2_fuzz_force_handler) {
        switch (g_usart2.fuzz_rx_buf[1]) {
        case 0x01: case 0x02: case 0x03: case 0x04:
        case 0x05: case 0x06: case 0x0f:
        case 0x10: case 0x11: case 0x17:
            break;
        default:
            g_usart2_fuzz_force_handler = 1U;
            break;
        }
    }
    g_usart2.fuzz_rx_len = (unsigned)usart2_modbus_request_len(
        g_usart2.fuzz_rx_buf, g_usart2.fuzz_rx_len);
    g_usart2.fuzz_rx_pos = 0U;
    usart2_reset_modbus_rtu_frame_state();
    g_usart2_force_first_rx_idle = 1;
    g_usart2_fuzz_ready = true;
    g_usart2.fuzz_rx_release_ns = usart2_now_ns();
    usart2_poll_fuzz_rx(&g_usart2);
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
    g_usart2_irq_sr = s->sr & 0x3FFu;

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

static void usart2_poll_fuzz_rx(USART2State *s) {
    if (!g_usart2_fuzz_ready || g_usart2_fuzz_input_consumed ||
        g_usart2_fuzz_rx_pending != 0U || s->rx_count != 0U) {
        return;
    }

    if (s->fuzz_rx_release_ns != INT64_MAX &&
        usart2_now_ns() < s->fuzz_rx_release_ns) {
        return;
    }

    s->fuzz_rx_release_ns = INT64_MAX;

    if (s->fuzz_rx_pos >= s->fuzz_rx_len) {
        g_usart2_fuzz_input_consumed = true;
        return;
    }

    uint8_t byte = s->fuzz_rx_buf[s->fuzz_rx_pos++];
    g_usart2_irq_dr = byte;
    usart2_queue_rx_byte(s, byte);
    g_usart2_fuzz_irq_schedule_active = 1;
    g_usart2_fuzz_rx_pending = 1;
    /* Re-pend the real USART line for each synthetic byte.  This is the
     * model-local equivalent of the former guest-PC scheduling workaround. */
    qemu_plugin_raise_irq(USART2_IRQ_LINE, false);
}

static void usart2_after_fuzz_dr_store(unsigned int cpu_index, void *userdata) {
    USART2State *s = &g_usart2;
    (void)cpu_index;
    (void)userdata;

    if (!g_usart2_fuzz_ready || s->fuzz_rx_pos == 0U) {
        return;
    }


    /* 0x080008d0 is the STRB following the real DR load.  Supply the
     * current byte here, then prepare the following ISR pass. */
    fuzz_set_register(g_usart2_irq_dr, 3);
    s->rx_head = 0U;
    s->rx_tail = 0U;
    s->rx_count = 0U;
    s->sr &= ~(USART_SR_RXNE | USART_SR_ORE);
    s->irq_latched = false;

    if (s->fuzz_rx_pos < s->fuzz_rx_len) {
        uint8_t byte = s->fuzz_rx_buf[s->fuzz_rx_pos++];
        s->dr = byte;
        s->sr |= USART_SR_RXNE;
        s->sr &= ~USART_SR_IDLE;
        g_usart2_irq_dr = byte;
        g_usart2_fuzz_irq_schedule_active = 1;
        g_usart2_fuzz_rx_pending = 1;
        qemu_plugin_raise_irq(USART2_IRQ_LINE, false);
    } else {
        s->dr = 0U;
        s->sr |= USART_SR_IDLE;
        g_usart2_fuzz_input_consumed = true;
        g_usart2_fuzz_irq_schedule_active = 0;
        g_usart2_fuzz_rx_pending = 0;
    }

    g_usart2_irq_sr = s->sr & 0x3FFu;
}

static void usart2_after_fuzz_rx_fsm(unsigned int cpu_index, void *userdata) {
    (void)cpu_index;
    (void)userdata;
    g_usart2_force_first_rx_idle = 0U;
}

/* xMBRTUTransmitFSM reaches 0x08000ef2 only after it has posted the frame
 * sent event, disabled TX, and is about to mark eSndState TX_IDLE.  This is
 * IRQ context, so request the handoff and let the main poll-loop virtual do
 * the blocking AFLNet exchange after the IRQ returns. */
static void usart2_after_fuzz_tx_complete(unsigned int cpu_index, void *userdata) {
    (void)cpu_index;
    (void)userdata;

    if (g_usart2_fuzz_ready && g_usart2_fuzz_input_consumed &&
        g_usart2_fuzz_response_pending) {
        g_usart2_fuzz_response_pending = 0U;
        fuzz_request_sync(0x08000268U);
    }
}

/* Preserve the real receive and CRC work for malformed AFLNet frames, then
 * route only their post-CRC dispatch through a handler present in this image.
 * The function byte is the second byte of FreeMODBUS's fixed RTU buffer. */
static void usart2_force_compat_handler(unsigned int cpu_index, void *userdata) {
    uint8_t function = 0x11U; /* Report Slave ID: installed, bounded response. */

    (void)cpu_index;
    (void)userdata;
    if (g_usart2_fuzz_force_handler != 0U) {
        (void)fuzz_write_memory(0x080036D9U, &function, (int)sizeof(function));
    }
}

/* These callbacks replace the old host-word register update and gated
 * modifiers.  They use only the long-standing virtual-rule interface: the
 * model owns the conditions and updates the guest register at the exact PC. */
static void usart2_refresh_irq_sr(unsigned int cpu_index, void *userdata) {
    (void)cpu_index;
    (void)userdata;
    fuzz_set_register(g_usart2_irq_sr, 4);
}

static void usart2_force_min_frame(unsigned int cpu_index, void *userdata) {
    (void)cpu_index;
    (void)userdata;
    if (g_usart2_fuzz_force_min_len != 0U) {
        fuzz_set_register(4U, 3);
    }
}

static void usart2_force_handler_arg(unsigned int cpu_index, void *userdata) {
    (void)cpu_index;
    (void)userdata;
    if (g_usart2_fuzz_force_handler != 0U) {
        fuzz_set_register(fuzz_get_register(2), 1);
    }
}

static void usart2_force_first_idle(unsigned int cpu_index, void *userdata) {
    (void)cpu_index;
    (void)userdata;
    if (g_usart2_force_first_rx_idle != 0U) {
        fuzz_set_register(1U, 3);
    }
}

static void usart2_rx_timer_cb(void *opaque) {
    USART2State *s = (USART2State *)opaque;
    usart2_poll_fuzz_rx(s);
}

static uint32_t usart2_pop_dr(USART2State *s) {
    uint32_t ret = 0;
    bool had_byte = false;
    bool consumed_first_fuzz_byte = false;

    usart2_poll_fuzz_rx(s);
    usart2_update_tx_state(s, false);

    if (s->rx_count > 0U) {
        had_byte = true;
        consumed_first_fuzz_byte =
            g_usart2_force_first_rx_idle != 0U && s->fuzz_rx_pos == 1U;
        ret = s->rx_fifo[s->rx_head];
        s->rx_head = (s->rx_head + 1U) % USART2_RX_FIFO_LEN;
        s->rx_count--;

        if (s->rx_count > 0U) {
            s->dr = s->rx_fifo[s->rx_head];
            s->sr |= USART_SR_RXNE;
        } else {
            s->dr = 0;
            s->sr &= ~USART_SR_RXNE;
            /* Present the next RTU byte as a fresh RXNE edge.  The
             * FreeMODBUS IRQ handler consumes one byte per invocation;
             * keeping a whole frame level-latched lets its T3.5 timer fire
             * before the remaining bytes are serviced. */
            s->irq_latched = false;
            usart2_poll_fuzz_rx(s);
            if (s->rx_count == 0U) {
                s->sr |= USART_SR_IDLE;
            }
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

    if (had_byte && !consumed_first_fuzz_byte) {
        g_usart2_force_first_rx_idle = 0;
    }

    g_usart2_irq_dr = ret & 0x1FFu;
    g_usart2_irq_sr = s->sr & 0x3FFu;
    if (g_usart2_fuzz_input_consumed && s->rx_count == 0U) {
        /* The final RX handler is already in flight; let T3.5 expire next. */
        g_usart2_fuzz_irq_schedule_active = 0;
        g_usart2_fuzz_rx_pending = 0;
    }
    usart2_maybe_raise_irq(s);
    return ret & 0x1FFu;
}

static uint32_t usart2_read_reg(USART2State *s, uint32_t offset) {
    switch (offset) {
    case USART_SR_OFF:
        usart2_poll_fuzz_rx(s);
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

    usart2_poll_fuzz_rx(s);
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
            usart2_capture_fuzz_tx_byte(s, byte);
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

    if (offset == USART_CR1_OFF && (s->cr1 & USART_CR1_TXEIE) != 0U) {
        g_usart2_fuzz_irq_schedule_active = 1;
    }
    g_usart2_irq_sr = s->sr & 0x3FFu;
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
    memset(&g_usart2_snapshot, 0, sizeof(g_usart2_snapshot));
    g_usart2_snapshot_valid = false;
    g_usart2_fuzz_ready = false;
    g_usart2_fuzz_input_consumed = false;
    g_usart2_fuzz_irq_schedule_active = 0;
    g_usart2_fuzz_rx_pending = 0;
    g_usart2_fuzz_force_handler = 0;
    g_usart2_fuzz_force_min_len = 0;
    g_usart2_fuzz_response_pending = 0;
    g_usart2_force_first_rx_idle = 0;
    g_usart2_irq_sr = 0U;
    g_usart2_irq_dr = 0U;

    g_usart2.pty_fd = -1;
    g_usart2.txe_ready_ns = INT64_MAX;
    g_usart2.tc_ready_ns = INT64_MAX;
    g_usart2.fuzz_rx_release_ns = INT64_MAX;

    fuzz_register_callback(usart2_sync_hook);
    fuzz_register_input_callback(usart2_input_hook);
    fuzz_register_restore(usart2_restore_hook);
    if (!core_register_virtual_rule(0x080008d0u, usart2_after_fuzz_dr_store,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x080008f4u, usart2_after_fuzz_rx_fsm,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x08000ef2u,
                                    usart2_after_fuzz_tx_complete,
                                    "modbus-tx") ||
        !core_register_virtual_rule(0x08000d1cu, usart2_force_compat_handler,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x080008e2u, usart2_refresh_irq_sr,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x08000cf8u, usart2_force_min_frame,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x08000d28u, usart2_force_min_frame,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x08000ba4u, usart2_force_handler_arg,
                                    "modbus-rx") ||
        !core_register_virtual_rule(0x08000de2u, usart2_force_first_idle,
                                    "modbus-rx")) {
        dev_debug("failed to register USART2 RX progression hooks");
    }

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

    dev_debug("USART2 model initialized with fuzz backend");
    return &g_usart2;
}
