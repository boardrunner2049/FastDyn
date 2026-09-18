// Device Model for USART3

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

typedef void (*fuzz_callback_t)(void);
size_t fuzz_get_data(char *buf, size_t len);
void fuzz_set_data(char *buf, size_t len);
void fuzz_register_callback(fuzz_callback_t cb);
void fuzz_register_restore(fuzz_callback_t cb);

#define USART3_BASE 0x40004800ULL
#define USART3_MODEL_SNAPSHOT_PATH "fastdyn_work/model-snap.bin"

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
#define USART_CR1_TXFEIE             (1u << 30)

/* RQR bits used */
#define USART_RQR_RXFRQ              (1u << 3)

/* CR3 bits used by HAL_UART_IRQHandler. */
#define USART_CR3_EIE                (1u << 0)
#define USART_CR3_WUFIE              (1u << 22)

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
#define USART_ISR_WUF                (1u << 20)

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
#define USART3_FUZZ_RX_BUF_SIZE      1024u
#define USART3_FUZZ_TX_CAPTURE_SIZE  4096u
#define USART3_RX_FIFO_THRESHOLD     2u
#define USART3_RX_POLL_PERIOD_NS     1000000ULL
#define USART3_TX_COMPLETE_DELAY_NS  1000ULL
#define USART3_IRQ_SWEEP_DELAY_NS    1000ULL
#define USART3_MODEL_SNAPSHOT_MAGIC  0x55533353u
#define USART3_MODEL_SNAPSHOT_VER    1u
#define UART_HANDLE_ADDRESS           0x20000A1CULL
#define UART_HANDLE_SNAPSHOT_SIZE     160u
#define UART_HANDLE_RX_ISR_OFFSET     0x74u
#define UART_HANDLE_TX_ISR_OFFSET     0x78u
#define UART_SAFE_CALLBACK            0x080077A3u

/* The MQTT firmware uses a polled UART.  The following bounded event sweep is
 * deliberately comparison-only: it visits its otherwise dormant HAL IRQ
 * paths before the fuzz snapshot, saves both the model and UART handle, and
 * restores them when the exception returns.  It never consumes a broker byte
 * or leaves an interrupt mode enabled for the actual AFLNet testcase. */
#define USART3_IRQ_SWEEP_STAGES       6u

typedef enum {
    USART3_INPUT_PTY = 0,
    USART3_INPUT_FUZZ = 1,
} USART3InputMode;

typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cr3;
    uint32_t brr;
    uint32_t gtpr;
    uint32_t rtor;
    uint32_t presc;
    uint32_t isr_latched;
    uint32_t rx_head;
    uint32_t rx_tail;
    uint32_t rx_count;
    uint64_t tx_done_ns;
    uint8_t rx_fifo[USART3_RX_FIFO_SIZE];
    uint8_t last_tdr;
    uint8_t last_rdr;
    uint8_t fifo_activity_flag;
    uint8_t txe_flag;
    uint8_t tc_flag;
    uint8_t tx_busy;
    uint8_t irq_level;
} USART3DiskSnapshot;

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
    USART3InputMode input_mode;
    uint8_t fuzz_rx_buf[USART3_FUZZ_RX_BUF_SIZE];
    uint32_t fuzz_rx_len;
    uint32_t fuzz_rx_pos;

    uint8_t fuzz_tx_capture_buf[USART3_FUZZ_TX_CAPTURE_SIZE];
    uint32_t fuzz_tx_capture_len;
    int irq_num;
    bool irq_level;

    uint64_t rx_timer;
    uint64_t tx_timer;
    uint64_t irq_sweep_timer;
} USART3State;

static USART3State g_usart3;
static USART3State g_usart3_snapshot;
static bool g_usart3_snapshot_valid;
static bool g_usart3_first_sync_done;
static USART3State g_usart3_irq_sweep_saved;
static uint8_t g_usart3_irq_sweep_handle[UART_HANDLE_SNAPSHOT_SIZE];
static bool g_usart3_irq_sweep_enabled;
static bool g_usart3_irq_sweep_started;
static bool g_usart3_irq_sweep_active;
static uint32_t g_usart3_irq_sweep_stage;

/* Bring the client through CONNECT, SUBSCRIBE, and its first yield before the
 * fuzz snapshot.  AFLNet then mutates broker packets in an already-subscribed
 * session instead of trying to restore into the middle of the setup path. */
static const uint8_t g_usart3_bootstrap[] = {
    0x20, 0x02, 0x00, 0x00,                         /* CONNACK */
    0x90, 0x03, 0x00, 0x01, 0x00,                   /* SUBACK */
    0x32, 0x0f, 0x00, 0x09,                         /* PUBLISH QoS 1 */
    's', 't', 'm', '3', '2', '/', 'c', 'm', 'd',
    0x00, 0x01, 'o', 'n',
};
static size_t g_usart3_bootstrap_pos;

static void usart3_reset_fuzz_tx_capture(USART3State *s)
{
    s->fuzz_tx_capture_len = 0;
}

static void usart3_capture_fuzz_tx_byte(USART3State *s, uint8_t byte)
{
    if (s->input_mode != USART3_INPUT_FUZZ) {
        return;
    }

    if (s->fuzz_tx_capture_len == sizeof(s->fuzz_tx_capture_buf)) {
        /* A complete MQTT response is normally far smaller than this.
         * Drop an overlong partial exchange rather than presenting a
         * truncated packet to the state extractor. */
        usart3_reset_fuzz_tx_capture(s);
    }

    s->fuzz_tx_capture_buf[s->fuzz_tx_capture_len++] = byte;
}

static void usart3_flush_fuzz_tx_capture(USART3State *s)
{
    if (s->input_mode == USART3_INPUT_FUZZ &&
        s->fuzz_tx_capture_len != 0) {
        fuzz_set_data((char *)s->fuzz_tx_capture_buf,
                      s->fuzz_tx_capture_len);
    }
    usart3_reset_fuzz_tx_capture(s);
}

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

static bool usart3_model_snapshot_file_exists(void)
{
    FILE *f = fopen(USART3_MODEL_SNAPSHOT_PATH, "rb");

    if (f == NULL) {
        return false;
    }

    fclose(f);
    return true;
}

static void usart3_reset_fuzz_rx(USART3State *s)
{
    s->fuzz_rx_len = 0;
    s->fuzz_rx_pos = 0;
}

static void usart3_save_runtime_snapshot(void)
{
    g_usart3_snapshot = g_usart3;
    usart3_reset_fuzz_rx(&g_usart3_snapshot);
    usart3_reset_fuzz_tx_capture(&g_usart3_snapshot);
    g_usart3_snapshot_valid = true;
}

static void usart3_restore_runtime_snapshot(void)
{
    if (!g_usart3_snapshot_valid) {
        return;
    }

    g_usart3 = g_usart3_snapshot;
    usart3_reset_fuzz_rx(&g_usart3);
    usart3_reset_fuzz_tx_capture(&g_usart3);

}

static void usart3_capture_disk_snapshot(const USART3State *s,
                                         USART3DiskSnapshot *snap)
{
    memset(snap, 0, sizeof(*snap));

    snap->magic = USART3_MODEL_SNAPSHOT_MAGIC;
    snap->version = USART3_MODEL_SNAPSHOT_VER;
    snap->cr1 = s->cr1;
    snap->cr2 = s->cr2;
    snap->cr3 = s->cr3;
    snap->brr = s->brr;
    snap->gtpr = s->gtpr;
    snap->rtor = s->rtor;
    snap->presc = s->presc;
    snap->isr_latched = s->isr_latched;
    snap->rx_head = s->rx_head;
    snap->rx_tail = s->rx_tail;
    snap->rx_count = s->rx_count;
    snap->tx_done_ns = s->tx_done_ns;
    memcpy(snap->rx_fifo, s->rx_fifo, sizeof(snap->rx_fifo));
    snap->last_tdr = s->last_tdr;
    snap->last_rdr = s->last_rdr;
    snap->fifo_activity_flag = s->fifo_activity_flag ? 1u : 0u;
    snap->txe_flag = s->txe_flag ? 1u : 0u;
    snap->tc_flag = s->tc_flag ? 1u : 0u;
    snap->tx_busy = s->tx_busy ? 1u : 0u;
    snap->irq_level = s->irq_level ? 1u : 0u;
}

static bool usart3_apply_disk_snapshot(USART3State *s,
                                       const USART3DiskSnapshot *snap)
{
    if (snap->magic != USART3_MODEL_SNAPSHOT_MAGIC ||
        snap->version != USART3_MODEL_SNAPSHOT_VER ||
        snap->rx_head >= USART3_RX_FIFO_SIZE ||
        snap->rx_tail >= USART3_RX_FIFO_SIZE ||
        snap->rx_count > USART3_RX_FIFO_SIZE) {
        return false;
    }

    s->cr1 = snap->cr1;
    s->cr2 = snap->cr2;
    s->cr3 = snap->cr3;
    s->brr = snap->brr;
    s->gtpr = snap->gtpr;
    s->rtor = snap->rtor;
    s->presc = snap->presc;
    s->isr_latched = snap->isr_latched;
    s->tx_done_ns = snap->tx_done_ns;
    s->rx_head = snap->rx_head;
    s->rx_tail = snap->rx_tail;
    s->rx_count = snap->rx_count;
    memcpy(s->rx_fifo, snap->rx_fifo, sizeof(s->rx_fifo));
    s->last_tdr = snap->last_tdr;
    s->last_rdr = snap->last_rdr;
    s->fifo_activity_flag = snap->fifo_activity_flag != 0;
    s->txe_flag = snap->txe_flag != 0;
    s->tc_flag = snap->tc_flag != 0;
    s->tx_busy = snap->tx_busy != 0;
    s->irq_level = snap->irq_level != 0;
    usart3_reset_fuzz_rx(s);

    return true;
}

static bool usart3_write_model_snapshot_file(const USART3State *s)
{
    USART3DiskSnapshot snap;
    FILE *f;
    bool ok;

    usart3_capture_disk_snapshot(s, &snap);

    f = fopen(USART3_MODEL_SNAPSHOT_PATH, "wb");
    if (f == NULL) {
        dev_debug("USART3 failed to open model snapshot for writing");
        return false;
    }

    ok = fwrite(&snap, sizeof(snap), 1, f) == 1;
    fclose(f);

    if (!ok) {
        dev_debug("USART3 failed to write model snapshot");
    }

    return ok;
}

static bool usart3_read_model_snapshot_file(USART3State *s)
{
    USART3DiskSnapshot snap;
    FILE *f;
    bool ok;

    f = fopen(USART3_MODEL_SNAPSHOT_PATH, "rb");
    if (f == NULL) {
        return false;
    }

    ok = fread(&snap, sizeof(snap), 1, f) == 1;
    fclose(f);

    if (!ok || !usart3_apply_disk_snapshot(s, &snap)) {
        dev_debug("USART3 failed to load model snapshot");
        return false;
    }

    return true;
}

static void usart3_poll_pty_rx(USART3State *s)
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

static void usart3_poll_fuzz_rx(USART3State *s)
{
    while (s->rx_count < USART3_RX_FIFO_SIZE) {
        if (s->fuzz_rx_pos >= s->fuzz_rx_len) {
            s->fuzz_rx_len = (uint32_t)fuzz_get_data(
                (char *)s->fuzz_rx_buf, sizeof(s->fuzz_rx_buf));
            s->fuzz_rx_pos = 0;
            if (s->fuzz_rx_len == 0) {
                break;
            }
        }

        usart3_rx_push(s, s->fuzz_rx_buf[s->fuzz_rx_pos]);
        s->fuzz_rx_pos++;

        if (s->fuzz_rx_pos >= s->fuzz_rx_len) {
            usart3_reset_fuzz_rx(s);
        }
    }
}

static void usart3_poll_bootstrap_rx(USART3State *s)
{
    while (s->rx_count < USART3_RX_FIFO_SIZE &&
           g_usart3_bootstrap_pos < sizeof(g_usart3_bootstrap)) {
        usart3_rx_push(s, g_usart3_bootstrap[g_usart3_bootstrap_pos++]);
    }
}

static void usart3_poll_input_rx(USART3State *s)
{
    if (s->input_mode == USART3_INPUT_FUZZ) {
        if (!g_usart3_first_sync_done) {
            usart3_poll_bootstrap_rx(s);
            return;
        }
        usart3_poll_fuzz_rx(s);
    } else {
        usart3_poll_pty_rx(s);
    }
}

static void usart3_sync_hook(void)
{
    if (g_usart3_first_sync_done) {
        usart3_flush_fuzz_tx_capture(&g_usart3);
        return;
    }

    g_usart3_first_sync_done = true;
    usart3_reset_fuzz_tx_capture(&g_usart3);

    if (g_usart3.input_mode == USART3_INPUT_FUZZ) {
        (void)usart3_read_model_snapshot_file(&g_usart3);
    } else {
        (void)usart3_write_model_snapshot_file(&g_usart3);
    }

    usart3_save_runtime_snapshot();
}

static void usart3_irq_sweep_store32(uint32_t offset, uint32_t value)
{
    uint8_t bytes[sizeof(value)];

    memcpy(bytes, &value, sizeof(bytes));
    (void)qemu_plugin_write_memory(UART_HANDLE_ADDRESS + offset, bytes,
                                   (int)sizeof(bytes));
}

static bool usart3_irq_sweep_save_handle(void)
{
    return qemu_plugin_read_memory(UART_HANDLE_ADDRESS,
                                   g_usart3_irq_sweep_handle,
                                   (int)sizeof(g_usart3_irq_sweep_handle)) ==
           0;
}

static void usart3_irq_sweep_prepare(USART3State *s)
{
    /* Preserve the real polled-UART state.  The exception-return callback
     * restores this exact copy before MQTT bootstrap/fuzzing continues. */
    g_usart3_irq_sweep_saved = *s;
    s->irq_level = true;

    switch (g_usart3_irq_sweep_stage) {
    case 0:
        /* RXNE dispatch through a harmless callback.  The snapshot restore
         * returns the queued bootstrap byte untouched. */
        s->cr1 |= USART_CR1_RXNEIE_RXFNEIE;
        s->rx_head = 0;
        s->rx_tail = 1;
        s->rx_count = 1;
        s->rx_fifo[0] = 0;
        usart3_irq_sweep_store32(UART_HANDLE_RX_ISR_OFFSET,
                                 UART_SAFE_CALLBACK);
        break;
    case 1:
        /* Transmission-complete callback path. */
        s->cr1 |= USART_CR1_TCIE;
        s->tc_flag = true;
        break;
    case 2:
        /* TX-empty dispatch through the same safe, no-op callback. */
        s->cr1 |= USART_CR1_TXEIE_TXFNFIE;
        s->txe_flag = true;
        usart3_irq_sweep_store32(UART_HANDLE_TX_ISR_OFFSET,
                                 UART_SAFE_CALLBACK);
        break;
    case 3:
        /* A framing-error interrupt covers HAL's error aggregation and
         * callback route without entering its DMA-abort state machine. */
        s->cr3 |= USART_CR3_EIE;
        s->isr_latched |= USART_ISR_FE;
        break;
    case 4:
        /* Wake-up flag path (the firmware's weak callback simply returns). */
        s->cr3 |= USART_CR3_WUFIE;
        s->isr_latched |= USART_ISR_WUF;
        break;
    case 5:
        /* FIFO-empty callback path.  TXEIE remains clear, so the handler
         * reaches the later TXFE test rather than returning through TxISR. */
        s->cr1 |= USART_CR1_FIFOEN | USART_CR1_TXFEIE;
        break;
    default:
        break;
    }
}

static void usart3_irq_sweep_timer_cb(void *opaque)
{
    USART3State *s = (USART3State *)opaque;

    if (!g_usart3_irq_sweep_enabled || g_usart3_irq_sweep_active ||
        g_usart3_first_sync_done ||
        g_usart3_irq_sweep_stage >= USART3_IRQ_SWEEP_STAGES ||
        !usart3_enabled_rx(s) || !usart3_enabled_tx(s) ||
        !usart3_irq_sweep_save_handle()) {
        return;
    }

    usart3_irq_sweep_prepare(s);
    g_usart3_irq_sweep_active = true;
    qemu_plugin_raise_irq(s->irq_num + 16, false);
}

/* Called by BoardRunner at exception entry and exception return respectively.
 * The entry callback is intentionally minimal; all state is set before the
 * vector is asserted. */
int usart3_interrupt(int number)
{
    (void)number;
    return 0;
}

int usart3_serve(int number)
{
    int64_t now;
    uint64_t next_deadline;

    if (!g_usart3_irq_sweep_active ||
        number != USART3_IRQ_NUM + 16) {
        return 0;
    }

    g_usart3 = g_usart3_irq_sweep_saved;
    (void)qemu_plugin_write_memory(UART_HANDLE_ADDRESS,
                                   g_usart3_irq_sweep_handle,
                                   (int)sizeof(g_usart3_irq_sweep_handle));
    g_usart3_irq_sweep_active = false;
    g_usart3_irq_sweep_stage++;

    if (!g_usart3_first_sync_done &&
        g_usart3_irq_sweep_stage < USART3_IRQ_SWEEP_STAGES) {
        now = qemu_plugin_get_virtual_timer();
        next_deadline = (now < 0) ? USART3_IRQ_SWEEP_DELAY_NS :
            (uint64_t)now + USART3_IRQ_SWEEP_DELAY_NS;
        qemu_plugin_timer_alarm(g_usart3.irq_sweep_timer, next_deadline);
    }

    return 0;
}

static uint32_t usart3_get_isr(USART3State *s)
{
    uint32_t isr;
    bool rx_enabled;
    bool tx_enabled;
    bool fifo_enabled;

    usart3_poll_input_rx(s);
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
    usart3_poll_input_rx(s);
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

        usart3_poll_input_rx(s);

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
        if (g_usart3_irq_sweep_enabled && !g_usart3_irq_sweep_started &&
            usart3_enabled_rx(s) && usart3_enabled_tx(s)) {
            int64_t now = qemu_plugin_get_virtual_timer();
            uint64_t deadline = (now < 0) ? USART3_IRQ_SWEEP_DELAY_NS :
                (uint64_t)now + USART3_IRQ_SWEEP_DELAY_NS;

            g_usart3_irq_sweep_started = true;
            qemu_plugin_timer_alarm(s->irq_sweep_timer, deadline);
        }
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
            usart3_capture_fuzz_tx_byte(s, ch);
            int64_t now = qemu_plugin_get_virtual_timer();
            uint64_t base_now = (now < 0) ? 0 : (uint64_t)now;

            if (s->pty_fd >= 0) {
                api_pty_write_req(s->pty_fd, ch);
            }

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
    const char *aflnet_protocol = getenv("FASTDYN_AFLNET_PROTOCOL");
    bool force_aflnet_fuzz = aflnet_protocol != NULL &&
        strcmp(aflnet_protocol, "MQTT_CLIENT") == 0;

    memset(&g_usart3, 0, sizeof(g_usart3));
    memset(&g_usart3_snapshot, 0, sizeof(g_usart3_snapshot));
    g_usart3_snapshot_valid = false;
    g_usart3_first_sync_done = false;
    g_usart3_bootstrap_pos = 0;
    g_usart3_irq_sweep_enabled = force_aflnet_fuzz &&
        (getenv("FASTDYN_MQTT_IRQ_SWEEP") == NULL ||
         strcmp(getenv("FASTDYN_MQTT_IRQ_SWEEP"), "0") != 0);
    g_usart3_irq_sweep_started = false;
    g_usart3_irq_sweep_active = false;
    g_usart3_irq_sweep_stage = 0;

    g_usart3.input_mode = (force_aflnet_fuzz ||
        usart3_model_snapshot_file_exists()) ?
        USART3_INPUT_FUZZ : USART3_INPUT_PTY;
    g_usart3.pty_fd = (g_usart3.input_mode == USART3_INPUT_PTY) ?
        api_pty_fd_gen() : -1;
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

    fuzz_register_callback(usart3_sync_hook);
    fuzz_register_restore(usart3_restore_runtime_snapshot);

    g_usart3.tx_timer = qemu_plugin_timer_new_ns(usart3_tx_timer_cb, &g_usart3);
    g_usart3.rx_timer = qemu_plugin_timer_new_period_ns(
        usart3_rx_timer_cb, &g_usart3, USART3_RX_POLL_PERIOD_NS);
    g_usart3.irq_sweep_timer = qemu_plugin_timer_new_ns(
        usart3_irq_sweep_timer_cb, &g_usart3);

    return &g_usart3;
}

/* ------------------------------------------------------------------------- */
/* Lower-level boot models                                                    */
/* ------------------------------------------------------------------------- */

/* The firmware only needs an STM32H7 clock tree to acknowledge the selected
 * HSE/PLL source and mirror the SYSCLK switch status.  Keeping this at the
 * RCC/PWR register boundary allows both HAL_RCC_* routines to execute, unlike
 * the former entry-point return shims. */
#define CLOCKTREE_RCC_BASE       0x58024400ULL
#define CLOCKTREE_PWR_BASE       0x58024800ULL
#define CLOCKTREE_FLASH_ACR       0x52002000ULL
#define CLOCKTREE_BLOCK_SIZE     0x400u
#define CLOCKTREE_REG_COUNT      (CLOCKTREE_BLOCK_SIZE / sizeof(uint32_t))

#define RCC_CR_OFFSET            0x00u
#define RCC_CFGR_OFFSET          0x10u
#define RCC_PLLCKSELR_OFFSET     0x28u
#define RCC_PLLCFGR_OFFSET       0x2Cu
#define RCC_PLL1DIVR_OFFSET      0x30u
#define RCC_CR_HSION             (1u << 0)
#define RCC_CR_HSIRDY            (1u << 2)
#define RCC_CR_CSION             (1u << 7)
#define RCC_CR_CSIRDY            (1u << 8)
#define RCC_CR_HSI48ON           (1u << 12)
#define RCC_CR_HSI48RDY          (1u << 13)
#define RCC_CR_HSEON             (1u << 16)
#define RCC_CR_HSERDY            (1u << 17)
#define RCC_CR_PLL1ON            (1u << 24)
#define RCC_CR_PLL1RDY           (1u << 25)
#define RCC_CR_PLL2ON            (1u << 26)
#define RCC_CR_PLL2RDY           (1u << 27)
#define RCC_CR_PLL3ON            (1u << 28)
#define RCC_CR_PLL3RDY           (1u << 29)

typedef struct {
    uint32_t rcc[CLOCKTREE_REG_COUNT];
    uint32_t pwr[CLOCKTREE_REG_COUNT];
    uint32_t flash_acr;
} ClockTreeState;

static ClockTreeState g_clocktree;
static bool g_clocktree_oscillator_shim;
static unsigned int g_clocktree_reset_cr_writes;

static void clocktree_seed_mqtt_oscillator(ClockTreeState *s)
{
    /* Captured after this firmware's final HSE-bypass/PLL1 oscillator write.
     * ClockConfig still programs CFGR and all bus prescalers itself. */
    s->rcc[RCC_CR_OFFSET / sizeof(uint32_t)] = 0x03070005u;
    s->rcc[RCC_PLLCKSELR_OFFSET / sizeof(uint32_t)] = 0x00000042u;
    s->rcc[RCC_PLLCFGR_OFFSET / sizeof(uint32_t)] = 0x00070005u;
    s->rcc[RCC_PLL1DIVR_OFFSET / sizeof(uint32_t)] = 0x0103038fu;
}

static uint32_t clocktree_ready_bits(uint32_t value,
                                     uint32_t enable, uint32_t ready)
{
    if (value & enable) {
        return value | ready;
    }
    return value & ~ready;
}

static void clocktree_update_rcc_status(ClockTreeState *s)
{
    uint32_t *cr = &s->rcc[RCC_CR_OFFSET / sizeof(uint32_t)];
    uint32_t *cfgr = &s->rcc[RCC_CFGR_OFFSET / sizeof(uint32_t)];
    uint32_t sysclk_source;

    *cr = clocktree_ready_bits(*cr, RCC_CR_HSION, RCC_CR_HSIRDY);
    *cr = clocktree_ready_bits(*cr, RCC_CR_CSION, RCC_CR_CSIRDY);
    *cr = clocktree_ready_bits(*cr, RCC_CR_HSI48ON, RCC_CR_HSI48RDY);
    *cr = clocktree_ready_bits(*cr, RCC_CR_HSEON, RCC_CR_HSERDY);
    *cr = clocktree_ready_bits(*cr, RCC_CR_PLL1ON, RCC_CR_PLL1RDY);
    *cr = clocktree_ready_bits(*cr, RCC_CR_PLL2ON, RCC_CR_PLL2RDY);
    *cr = clocktree_ready_bits(*cr, RCC_CR_PLL3ON, RCC_CR_PLL3RDY);

    /* RCC_CFGR.SWS is the hardware-owned echo of RCC_CFGR.SW. */
    sysclk_source = *cfgr & 0x7u;
    *cfgr = (*cfgr & ~0x38u) | (sysclk_source << 3);
}

static uint32_t *clocktree_reg_ptr(ClockTreeState *s, hwaddr addr)
{
    uint64_t offset;

    if (addr >= CLOCKTREE_RCC_BASE &&
        addr < CLOCKTREE_RCC_BASE + CLOCKTREE_BLOCK_SIZE) {
        offset = addr - CLOCKTREE_RCC_BASE;
        return &s->rcc[offset / sizeof(uint32_t)];
    }
    if (addr >= CLOCKTREE_PWR_BASE &&
        addr < CLOCKTREE_PWR_BASE + CLOCKTREE_BLOCK_SIZE) {
        offset = addr - CLOCKTREE_PWR_BASE;
        return &s->pwr[offset / sizeof(uint32_t)];
    }
    if (addr >= CLOCKTREE_FLASH_ACR && addr < CLOCKTREE_FLASH_ACR + 4u) {
        return &s->flash_acr;
    }
    return NULL;
}

uint64_t clocktree_read(void *opaque, hwaddr addr, unsigned size)
{
    ClockTreeState *s = (ClockTreeState *)opaque;
    uint32_t *reg;

    (void)size;
    clocktree_update_rcc_status(s);
    reg = clocktree_reg_ptr(s, addr);
    return reg != NULL ? *reg : 0;
}

void clocktree_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ClockTreeState *s = (ClockTreeState *)opaque;
    uint32_t *reg = clocktree_reg_ptr(s, addr);

    if (reg == NULL) {
        return;
    }
    *reg = usart3_merge_reg_write(*reg, value, size);
    /* SystemInit clears RCC in three CR writes.  The third is after all PLL
     * registers are reset, so it is the last safe place to install the
     * captured oscillator image for the narrow OscConfig return shim. */
    if (g_clocktree_oscillator_shim && addr == CLOCKTREE_RCC_BASE + RCC_CR_OFFSET &&
        *reg == (RCC_CR_HSION | RCC_CR_HSIRDY) &&
        ++g_clocktree_reset_cr_writes == 3u) {
        clocktree_seed_mqtt_oscillator(s);
    }
    clocktree_update_rcc_status(s);
}

void* clocktree_init(ConfigSection* model_info)
{
    (void)model_info;
    memset(&g_clocktree, 0, sizeof(g_clocktree));
    g_clocktree_oscillator_shim = getenv("FASTDYN_MQTT_OSC_STARTUP_SHIM") != NULL &&
        strcmp(getenv("FASTDYN_MQTT_OSC_STARTUP_SHIM"), "0") != 0;
    g_clocktree_reset_cr_writes = 0;
    g_clocktree.rcc[RCC_CR_OFFSET / sizeof(uint32_t)] =
        RCC_CR_HSION | RCC_CR_HSIRDY;
    clocktree_update_rcc_status(&g_clocktree);
    return &g_clocktree;
}

/* HAL_SYSTICK_Config writes this small PPB window directly.  It is modeled
 * separately from the generic PPB fallback so the firmware executes the
 * actual setup function and one deterministic SysTick exception before the
 * fuzz snapshot; the virtual clock remains the source of post-snapshot time. */
#define SYSTICK_BASE              0xE000E010ULL
#define SYSTICK_CTRL_OFFSET       0x00u
#define SYSTICK_LOAD_OFFSET       0x04u
#define SYSTICK_VAL_OFFSET        0x08u
#define SYSTICK_CTRL_ENABLE       (1u << 0)
#define SYSTICK_CTRL_TICKINT      (1u << 1)

typedef struct {
    uint32_t ctrl;
    uint32_t load;
    uint32_t val;
    bool irq_emitted;
} SysTickState;

static SysTickState g_systick;

uint64_t systick_read(void *opaque, hwaddr addr, unsigned size)
{
    SysTickState *s = (SysTickState *)opaque;

    (void)size;
    switch (addr - SYSTICK_BASE) {
    case SYSTICK_CTRL_OFFSET:
        return s->ctrl;
    case SYSTICK_LOAD_OFFSET:
        return s->load;
    case SYSTICK_VAL_OFFSET:
        return s->val;
    default:
        return 0;
    }
}

void systick_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    SysTickState *s = (SysTickState *)opaque;

    switch (addr - SYSTICK_BASE) {
    case SYSTICK_CTRL_OFFSET:
        s->ctrl = usart3_merge_reg_write(s->ctrl, value, size);
        if (!s->irq_emitted &&
            (s->ctrl & (SYSTICK_CTRL_ENABLE | SYSTICK_CTRL_TICKINT)) ==
                (SYSTICK_CTRL_ENABLE | SYSTICK_CTRL_TICKINT)) {
            s->irq_emitted = true;
            qemu_plugin_raise_irq(15, false);
        }
        break;
    case SYSTICK_LOAD_OFFSET:
        s->load = usart3_merge_reg_write(s->load, value, size) & 0x00FFFFFFu;
        break;
    case SYSTICK_VAL_OFFSET:
        s->val = 0;
        break;
    default:
        break;
    }
}

void* systick_init(ConfigSection* model_info)
{
    (void)model_info;
    memset(&g_systick, 0, sizeof(g_systick));
    return &g_systick;
}
