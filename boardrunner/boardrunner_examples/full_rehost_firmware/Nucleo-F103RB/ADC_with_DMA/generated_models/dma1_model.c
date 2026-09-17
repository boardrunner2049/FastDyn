// Device Model for DMA1

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>

#define DMA1_BASE               0x40020000ULL
#define DMA1_NUM_CHANNELS       7

#define DMA1_ISR_OFFSET         0x00
#define DMA1_IFCR_OFFSET        0x04
#define DMA1_CH_BASE_OFFSET     0x08
#define DMA1_CH_STRIDE          0x14

#define DMA_CCR_EN              (1U << 0)
#define DMA_CCR_TCIE            (1U << 1)
#define DMA_CCR_HTIE            (1U << 2)
#define DMA_CCR_TEIE            (1U << 3)
#define DMA_CCR_DIR             (1U << 4)
#define DMA_CCR_CIRC            (1U << 5)
#define DMA_CCR_PINC            (1U << 6)
#define DMA_CCR_MINC            (1U << 7)
#define DMA_CCR_PSIZE_SHIFT     8
#define DMA_CCR_MSIZE_SHIFT     10
#define DMA_CCR_PL_SHIFT        12
#define DMA_CCR_MEM2MEM         (1U << 14)

#define DMA_CCR_VALID_MASK      0x7FFFU
#define DMA_CNDTR_VALID_MASK    0xFFFFU
#define DMA_ISR_VALID_MASK      0x0FFFFFFFU

/* Trace-derived periodic interrupt cadence: ~10.012 ms */
#define DMA1_PERIODIC_NS        10012000ULL

typedef struct DMA1State DMA1State;

typedef struct {
    DMA1State *s;
    int ch_num; /* 1..7 */
} DMA1ChannelCtx;

typedef struct {
    uint32_t ccr;
    uint32_t cndtr;
    uint32_t cpar;
    uint32_t cmar;
    uint32_t reload_cndtr;
    uint32_t cur_cpar;
    uint32_t cur_cmar;
} DMA1Channel;

struct DMA1State {
    uint32_t isr;
    DMA1Channel ch[DMA1_NUM_CHANNELS];
    DMA1ChannelCtx ctx[DMA1_NUM_CHANNELS];
};

static DMA1State g_dma1;

static void dma1_log(const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    dev_debug(buf);
}

static inline uint32_t access_mask(unsigned size)
{
    switch (size) {
    case 1: return 0xFFU;
    case 2: return 0xFFFFU;
    default: return 0xFFFFFFFFU;
    }
}

static inline uint32_t extract_access(uint32_t reg, hwaddr addr, unsigned size)
{
    uint32_t shift = (uint32_t)(addr & 0x3U) * 8U;
    return (reg >> shift) & access_mask(size);
}

static inline uint32_t merge_access(uint32_t oldv, hwaddr addr, uint64_t value,
                                    unsigned size, uint32_t valid_mask)
{
    uint32_t shift = (uint32_t)(addr & 0x3U) * 8U;
    uint32_t mask = access_mask(size) << shift;
    uint32_t newv = (oldv & ~mask) | (((uint32_t)value << shift) & mask);
    return newv & valid_mask;
}

static inline uint32_t expand_write_value(hwaddr addr, uint64_t value, unsigned size)
{
    uint32_t shift = (uint32_t)(addr & 0x3U) * 8U;
    return ((uint32_t)value & access_mask(size)) << shift;
}

static inline int dma1_irq_exc_for_channel(int ch_num)
{
    /* qemu_plugin_raise_irq expects exception number = IRQn + 16
     * STM32F103 DMA1:
     *   Ch1..Ch7 => IRQn 11..17 => exceptions 27..33
     */
    return 27 + (ch_num - 1);
}

static inline uint32_t dma1_flag_shift(int ch_num)
{
    return (uint32_t)(4 * (ch_num - 1));
}

static inline uint32_t dma1_flag_gif(int ch_num)
{
    return 1U << dma1_flag_shift(ch_num);
}

static inline uint32_t dma1_flag_tcif(int ch_num)
{
    return 1U << (dma1_flag_shift(ch_num) + 1U);
}

static inline uint32_t dma1_flag_htif(int ch_num)
{
    return 1U << (dma1_flag_shift(ch_num) + 2U);
}

static inline uint32_t dma1_flag_teif(int ch_num)
{
    return 1U << (dma1_flag_shift(ch_num) + 3U);
}

static int dma1_mem_size_bytes(uint32_t ccr)
{
    uint32_t msize = (ccr >> DMA_CCR_MSIZE_SHIFT) & 0x3U;
    switch (msize) {
    case 0: return 1;
    case 1: return 2;
    default: return 4;
    }
}

static int dma1_periph_size_bytes(uint32_t ccr)
{
    uint32_t psize = (ccr >> DMA_CCR_PSIZE_SHIFT) & 0x3U;
    switch (psize) {
    case 0: return 1;
    case 1: return 2;
    default: return 4;
    }
}

static bool dma1_decode_channel_reg(hwaddr offset, int *ch_num, hwaddr *suboff)
{
    if (offset < DMA1_CH_BASE_OFFSET) {
        return false;
    }

    hwaddr rel = offset - DMA1_CH_BASE_OFFSET;
    int idx = (int)(rel / DMA1_CH_STRIDE);
    hwaddr rem = rel % DMA1_CH_STRIDE;

    if (idx < 0 || idx >= DMA1_NUM_CHANNELS) {
        return false;
    }

    *ch_num = idx + 1;
    *suboff = rem;
    return true;
}

static void dma1_raise_if_enabled(DMA1State *s, int ch_num)
{
    DMA1Channel *c = &s->ch[ch_num - 1];
    uint32_t tc = dma1_flag_tcif(ch_num);
    uint32_t ht = dma1_flag_htif(ch_num);
    uint32_t te = dma1_flag_teif(ch_num);

    if (((s->isr & tc) && (c->ccr & DMA_CCR_TCIE)) ||
        ((s->isr & ht) && (c->ccr & DMA_CCR_HTIE)) ||
        ((s->isr & te) && (c->ccr & DMA_CCR_TEIE))) {
        qemu_plugin_raise_irq(dma1_irq_exc_for_channel(ch_num), false);
    }
}

static void dma1_set_flags(DMA1State *s, int ch_num, bool tc, bool ht, bool te)
{
    uint32_t bits = 0;

    if (tc) {
        bits |= dma1_flag_tcif(ch_num);
    }
    if (ht) {
        bits |= dma1_flag_htif(ch_num);
    }
    if (te) {
        bits |= dma1_flag_teif(ch_num);
    }
    if (bits) {
        bits |= dma1_flag_gif(ch_num);
    }

    s->isr |= bits;
    s->isr &= DMA_ISR_VALID_MASK;
    dma1_raise_if_enabled(s, ch_num);
}

static void dma1_apply_ifcr(DMA1State *s, uint32_t clear_bits)
{
    int ch_num;

    for (ch_num = 1; ch_num <= DMA1_NUM_CHANNELS; ch_num++) {
        uint32_t shift = dma1_flag_shift(ch_num);
        uint32_t group = 0xFU << shift;
        uint32_t req = clear_bits & group;

        if (!req) {
            continue;
        }

        /* CGIFx clears the whole group on STM32F1. */
        if (req & (1U << shift)) {
            s->isr &= ~group;
        } else {
            if (req & (1U << (shift + 1U))) {
                s->isr &= ~dma1_flag_tcif(ch_num);
            }
            if (req & (1U << (shift + 2U))) {
                s->isr &= ~dma1_flag_htif(ch_num);
            }
            if (req & (1U << (shift + 3U))) {
                s->isr &= ~dma1_flag_teif(ch_num);
            }
            /* Do not auto-clear GIFx unless CGIFx was written. */
        }
    }

    s->isr &= DMA_ISR_VALID_MASK;
}

static void dma1_advance_transfer(DMA1State *s, int ch_num, int transferred_bytes)
{
    DMA1Channel *c = &s->ch[ch_num - 1];
    int mem_unit = dma1_mem_size_bytes(c->ccr);
    int periph_unit = dma1_periph_size_bytes(c->ccr);
    uint32_t units = (uint32_t)((transferred_bytes + mem_unit - 1) / mem_unit);

    if (units == 0) {
        units = 1;
    }

    if (c->ccr & DMA_CCR_MINC) {
        c->cur_cmar += (uint32_t)transferred_bytes;
    }
    if (c->ccr & DMA_CCR_PINC) {
        c->cur_cpar += (uint32_t)periph_unit * units;
    }

    if (c->cndtr > units) {
        c->cndtr -= units;
    } else {
        c->cndtr = 0;
    }

    /* Minimal model: successful service sets HT + TC, matching observed ISR=0x7
     * for channel 1. In circular mode reload NDTR and internal addresses,
     * otherwise auto-disable.
     */
    dma1_set_flags(s, ch_num, true, true, false);

    if (c->cndtr == 0) {
        if ((c->ccr & DMA_CCR_CIRC) && c->reload_cndtr != 0) {
            c->cndtr = c->reload_cndtr;
            c->cur_cpar = c->cpar;
            c->cur_cmar = c->cmar;
        } else {
            c->ccr &= ~DMA_CCR_EN;
        }
    }
}

static void dma1_signal_transfer_error(DMA1State *s, int ch_num, const char *why)
{
    dma1_log("DMA1: channel %d transfer error: %s", ch_num, why);
    dma1_set_flags(s, ch_num, false, false, true);
}

static void dma1_handle_request_internal(DMA1State *s, int ch_num,
                                         const uint8_t *data, int len,
                                         bool has_payload)
{
    DMA1Channel *c;

    if (ch_num < 1 || ch_num > DMA1_NUM_CHANNELS) {
        return;
    }

    c = &s->ch[ch_num - 1];

    if (!(c->ccr & DMA_CCR_EN)) {
        return;
    }

    if (c->cndtr == 0 && !(c->ccr & DMA_CCR_CIRC)) {
        return;
    }

    if (has_payload) {
        /* Payload path models peripheral -> memory nicely. */
        if (c->ccr & DMA_CCR_DIR) {
            dma1_signal_transfer_error(s, ch_num,
                                       "payload request on memory-to-peripheral channel");
            return;
        }

        if (len <= 0 || len > 8) {
            dma1_signal_transfer_error(s, ch_num, "invalid payload length");
            return;
        }

        if (qemu_plugin_write_memory(c->cur_cmar, (uint8_t *)data, len) != 0) {
            dma1_signal_transfer_error(s, ch_num, "guest memory write failed");
            return;
        }

        dma1_advance_transfer(s, ch_num, len);
        return;
    }

    /* No-payload path: support memory-to-memory if both ends are RAM-accessible. */
    if (c->ccr & DMA_CCR_MEM2MEM) {
        int size = dma1_mem_size_bytes(c->ccr);
        uint8_t buf[4] = {0};

        if (qemu_plugin_read_memory(c->cur_cpar, buf, size) != 0) {
            dma1_signal_transfer_error(s, ch_num, "guest memory read failed");
            return;
        }
        if (qemu_plugin_write_memory(c->cur_cmar, buf, size) != 0) {
            dma1_signal_transfer_error(s, ch_num, "guest memory write failed");
            return;
        }

        dma1_advance_transfer(s, ch_num, size);
        return;
    }

    /* Without payload we cannot faithfully read a peripheral register or push
     * bytes into one using the provided RAM-only memory APIs.
     */
    if (c->ccr & DMA_CCR_DIR) {
        dma1_signal_transfer_error(s, ch_num,
                                   "memory-to-peripheral DMA requires unavailable MMIO sink API");
    } else {
        dma1_signal_transfer_error(s, ch_num,
                                   "peripheral-to-memory DMA requires payload source API use");
    }
}

static void dma1_stream_request_cb(void *opaque)
{
    DMA1ChannelCtx *ctx = (DMA1ChannelCtx *)opaque;
    dma1_handle_request_internal(ctx->s, ctx->ch_num, NULL, 0, false);
}

static void dma1_stream_request_data_cb(void *opaque, const uint8_t *data, int len)
{
    DMA1ChannelCtx *ctx = (DMA1ChannelCtx *)opaque;
    dma1_handle_request_internal(ctx->s, ctx->ch_num, data, len, true);
}

/* removed: synthetic periodic DMA flag generation caused premature steady-state */
uint64_t dma1_read(void *opaque, hwaddr addr, unsigned size)
{
    DMA1State *s = (DMA1State *)opaque;
    hwaddr offset = addr - DMA1_BASE;
    int ch_num;
    hwaddr suboff;

    switch (offset & ~0x3ULL) {
    case DMA1_ISR_OFFSET:
        return extract_access(s->isr, addr, size);

    case DMA1_IFCR_OFFSET:
        /* Write-only in hardware; return 0 on reads. */
        return 0;
    default:
        break;
    }

    if (!dma1_decode_channel_reg(offset, &ch_num, &suboff)) {
        dma1_log("DMA1: read from unknown offset 0x%08x", (unsigned)offset);
        return 0;
    }

    {
        DMA1Channel *c = &s->ch[ch_num - 1];
        uint32_t regv = 0;

        switch (suboff & ~0x3ULL) {
        case 0x00:
            regv = c->ccr;
            break;
        case 0x04:
            regv = c->cndtr;
            break;
        case 0x08:
            regv = c->cpar;
            break;
        case 0x0C:
            regv = c->cmar;
            break;
        case 0x10:
            regv = 0;
            break;
        default:
            dma1_log("DMA1: read from unknown channel reg offset 0x%08x",
                     (unsigned)offset);
            regv = 0;
            break;
        }

        return extract_access(regv, addr, size);
    }
}

void dma1_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    DMA1State *s = (DMA1State *)opaque;
    hwaddr offset = addr - DMA1_BASE;
    int ch_num;
    hwaddr suboff;

    switch (offset & ~0x3ULL) {
    case DMA1_ISR_OFFSET:
        /* Read-only. Ignore writes. */
        return;

    case DMA1_IFCR_OFFSET: {
        uint32_t full = expand_write_value(addr, value, size);
        dma1_apply_ifcr(s, full);
        return;
    }

    default:
        break;
    }

    if (!dma1_decode_channel_reg(offset, &ch_num, &suboff)) {
        dma1_log("DMA1: write to unknown offset 0x%08x value=0x%08x",
                 (unsigned)offset, (unsigned)value);
        return;
    }

    {
        DMA1Channel *c = &s->ch[ch_num - 1];

        switch (suboff & ~0x3ULL) {
        case 0x00: {
            uint32_t old_ccr = c->ccr;
            uint32_t new_ccr = merge_access(c->ccr, addr, value, size, DMA_CCR_VALID_MASK);

            c->ccr = new_ccr;

            if (!(old_ccr & DMA_CCR_EN) && (new_ccr & DMA_CCR_EN)) {
                c->cur_cpar = c->cpar;
                c->cur_cmar = c->cmar;
            }
            break;
        }

        case 0x04:
            c->cndtr = merge_access(c->cndtr, addr, value, size, DMA_CNDTR_VALID_MASK);
            c->reload_cndtr = c->cndtr;
            break;

        case 0x08:
            c->cpar = merge_access(c->cpar, addr, value, size, 0xFFFFFFFFU);
            c->cur_cpar = c->cpar;
            break;

        case 0x0C:
            c->cmar = merge_access(c->cmar, addr, value, size, 0xFFFFFFFFU);
            c->cur_cmar = c->cmar;
            break;

        case 0x10:
            /* reserved */
            break;

        default:
            dma1_log("DMA1: write to unknown channel reg offset 0x%08x value=0x%08x",
                     (unsigned)offset, (unsigned)value);
            break;
        }
    }
}

// MUST return &g_state — framework stores this and passes it as opaque to _read/_write
void* dma1_init(ConfigSection* model_info)
{
    int ch_num;
    (void)model_info;

    memset(&g_dma1, 0, sizeof(g_dma1));

    for (ch_num = 1; ch_num <= DMA1_NUM_CHANNELS; ch_num++) {
        g_dma1.ctx[ch_num - 1].s = &g_dma1;
        g_dma1.ctx[ch_num - 1].ch_num = ch_num;

        /* Register channels using STM32 channel numbering 1..7. */
        api_dma_register_stream(1, ch_num, dma1_stream_request_cb, &g_dma1.ctx[ch_num - 1]);
        api_dma_register_stream_data(1, ch_num, dma1_stream_request_data_cb,
                                     &g_dma1.ctx[ch_num - 1]);
    }

    return &g_dma1;
}