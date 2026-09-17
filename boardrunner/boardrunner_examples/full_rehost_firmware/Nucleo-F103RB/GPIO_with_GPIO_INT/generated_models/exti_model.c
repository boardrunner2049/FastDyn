#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define EXTI_BASE_ADDR       0x40010400ULL

#define EXTI_IMR_OFF         0x00
#define EXTI_EMR_OFF         0x04
#define EXTI_RTSR_OFF        0x08
#define EXTI_FTSR_OFF        0x0C
#define EXTI_SWIER_OFF       0x10
#define EXTI_PR_OFF          0x14

/* STM32F103 EXTI implements lines 0..19 */
#define EXTI_VALID_MASK      0x000FFFFFU

/* Observed NVIC mapping from trace: EXTI15_10 => IRQn 40 => exception 56 */
#define EXTI15_10_EXCEPTION  56

typedef struct {
    uint32_t imr;
    uint32_t emr;
    uint32_t rtsr;
    uint32_t ftsr;
    uint32_t swier;
    uint32_t pr;

    bool line_level[20];
    bool line_valid[20];
} EXTIState;

static EXTIState g_exti;

static uint32_t exti_size_mask(unsigned size)
{
    switch (size) {
    case 1: return 0x000000FFu;
    case 2: return 0x0000FFFFu;
    case 4: return 0xFFFFFFFFu;
    default: return 0xFFFFFFFFu;
    }
}

static uint32_t exti_extract_reg(uint32_t reg, hwaddr offset, unsigned size)
{
    unsigned shift = (unsigned)(offset & 0x3u) * 8u;
    uint32_t mask = exti_size_mask(size);
    return (reg >> shift) & mask;
}

static uint32_t exti_merge_reg(uint32_t oldv, hwaddr offset, uint64_t value, unsigned size)
{
    unsigned shift = (unsigned)(offset & 0x3u) * 8u;
    uint32_t lane_mask = exti_size_mask(size) << shift;
    uint32_t lane_val = (((uint32_t)value) & exti_size_mask(size)) << shift;
    return (oldv & ~lane_mask) | (lane_val & lane_mask);
}

static void exti_debug_bad_access(const char *kind, hwaddr offset, unsigned size)
{
    char buf[128];
    snprintf(buf, sizeof(buf), "EXTI: bad %s offset=0x%llx size=%u",
             kind, (unsigned long long)offset, size);
    dev_debug(buf);
}

static void exti_maybe_raise_irq(EXTIState *s)
{
    /*
     * Trace evidence only shows EXTI15_10 usage (line 13 -> IRQn 40 -> exception 56).
     * Raise when any unmasked pending line in 10..15 exists.
     */
    uint32_t pending_15_10 = s->pr & s->imr & 0x0000FC00u;
    if (pending_15_10) {
        qemu_plugin_raise_irq(EXTI15_10_EXCEPTION, false);
    }
}

static void exti_set_pending_line(EXTIState *s, int line)
{
    uint32_t bit;

    if (line < 0 || line >= 20) {
        return;
    }

    bit = (1u << line);
    if ((s->pr & bit) == 0) {
        s->pr |= bit;
        exti_maybe_raise_irq(s);
    }
}

static void exti_handle_line_level(EXTIState *s, int line, bool new_level)
{
    bool old_level;
    bool rising;
    bool falling;
    uint32_t bit;

    if (line < 0 || line >= 20) {
        return;
    }

    if (!s->line_valid[line]) {
        s->line_valid[line] = true;
        s->line_level[line] = new_level;
        return;
    }

    old_level = s->line_level[line];
    if (old_level == new_level) {
        return;
    }

    s->line_level[line] = new_level;
    rising = (!old_level && new_level);
    falling = (old_level && !new_level);
    bit = (1u << line);

    if (rising && (s->rtsr & bit)) {
        exti_set_pending_line(s, line);
    }
    if (falling && (s->ftsr & bit)) {
        exti_set_pending_line(s, line);
    }
}

static void exti_signal_cb(void *opaque, int signal_id, bool level)
{
    EXTIState *s = (EXTIState *)opaque;

    /*
     * Assumption used by the framework integration:
     * signal_id N corresponds to EXTI line N.
     * This matches the available inter-peripheral API and the observed EXTI13 use.
     */
    if (signal_id >= 0 && signal_id < 20) {
        exti_handle_line_level(s, signal_id, level);
    }
}

uint64_t exti_read(void *opaque, hwaddr addr, unsigned size)
{
    EXTIState *s = (EXTIState *)opaque;
    hwaddr offset;
    hwaddr reg_off;
    uint32_t regv = 0;

    if (addr < EXTI_BASE_ADDR) {
        exti_debug_bad_access("read", addr, size);
        return 0;
    }

    offset = addr - EXTI_BASE_ADDR;
    reg_off = offset & ~0x3u;

    switch (reg_off) {
    case EXTI_IMR_OFF:
        regv = s->imr;
        break;
    case EXTI_EMR_OFF:
        regv = s->emr;
        break;
    case EXTI_RTSR_OFF:
        regv = s->rtsr;
        break;
    case EXTI_FTSR_OFF:
        regv = s->ftsr;
        break;
    case EXTI_SWIER_OFF:
        regv = s->swier;
        break;
    case EXTI_PR_OFF:
        regv = s->pr;
        break;
    default:
        exti_debug_bad_access("read", offset, size);
        return 0;
    }

    return exti_extract_reg(regv, offset, size);
}

void exti_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    EXTIState *s = (EXTIState *)opaque;
    hwaddr offset;
    hwaddr reg_off;
    uint32_t oldv, newv;
    uint32_t write_bits;
    int line;

    if (addr < EXTI_BASE_ADDR) {
        exti_debug_bad_access("write", addr, size);
        return;
    }

    offset = addr - EXTI_BASE_ADDR;
    reg_off = offset & ~0x3u;

    switch (reg_off) {
    case EXTI_IMR_OFF:
        oldv = s->imr;
        newv = exti_merge_reg(oldv, offset, value, size) & EXTI_VALID_MASK;
        s->imr = newv;
        exti_maybe_raise_irq(s);
        break;

    case EXTI_EMR_OFF:
        oldv = s->emr;
        newv = exti_merge_reg(oldv, offset, value, size) & EXTI_VALID_MASK;
        s->emr = newv;
        break;

    case EXTI_RTSR_OFF:
        oldv = s->rtsr;
        newv = exti_merge_reg(oldv, offset, value, size) & EXTI_VALID_MASK;
        s->rtsr = newv;
        break;

    case EXTI_FTSR_OFF:
        oldv = s->ftsr;
        newv = exti_merge_reg(oldv, offset, value, size) & EXTI_VALID_MASK;
        s->ftsr = newv;
        break;

    case EXTI_SWIER_OFF:
        oldv = s->swier;
        newv = exti_merge_reg(oldv, offset, value, size) & EXTI_VALID_MASK;
        s->swier = newv;

        write_bits = exti_extract_reg((uint32_t)value, offset & 0x3u, size);
        write_bits &= exti_size_mask(size);
        write_bits <<= ((unsigned)(offset & 0x3u) * 8u);
        write_bits &= EXTI_VALID_MASK;

        for (line = 0; line < 20; line++) {
            if (write_bits & (1u << line)) {
                exti_set_pending_line(s, line);
            }
        }
        break;

    case EXTI_PR_OFF:
        /*
         * PR is write-1-to-clear. Only the written '1' bits matter.
         */
        write_bits = exti_extract_reg((uint32_t)value, offset & 0x3u, size);
        write_bits &= exti_size_mask(size);
        write_bits <<= ((unsigned)(offset & 0x3u) * 8u);
        write_bits &= EXTI_VALID_MASK;
        s->pr &= ~write_bits;
        break;

    default:
        exti_debug_bad_access("write", offset, size);
        break;
    }
}

/* MUST return &g_exti so the framework passes it back as opaque */
void* exti_init(ConfigSection *model_info)
{
    int line;
    (void)model_info;

    memset(&g_exti, 0, sizeof(g_exti));

    /*
     * Register external signal consumers for GPIO-backed EXTI lines.
     * The source peripheral is expected to publish line-numbered signals.
     */
    for (line = 0; line < 16; line++) {
        api_signal_register(line, exti_signal_cb, &g_exti);
    }

    return &g_exti;
}