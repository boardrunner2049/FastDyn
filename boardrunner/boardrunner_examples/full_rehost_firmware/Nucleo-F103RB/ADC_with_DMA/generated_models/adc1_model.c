#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

// Device Model for ADC1

#define ADC1_BASE           0x40012400ULL

#define ADC1_SR_OFF         0x00
#define ADC1_CR1_OFF        0x04
#define ADC1_CR2_OFF        0x08
#define ADC1_SMPR1_OFF      0x0C
#define ADC1_SMPR2_OFF      0x10
#define ADC1_JOFR1_OFF      0x14
#define ADC1_JOFR2_OFF      0x18
#define ADC1_JOFR3_OFF      0x1C
#define ADC1_JOFR4_OFF      0x20
#define ADC1_HTR_OFF        0x24
#define ADC1_LTR_OFF        0x28
#define ADC1_SQR1_OFF       0x2C
#define ADC1_SQR2_OFF       0x30
#define ADC1_SQR3_OFF       0x34
#define ADC1_JSQR_OFF       0x38
#define ADC1_JDR1_OFF       0x3C
#define ADC1_JDR2_OFF       0x40
#define ADC1_JDR3_OFF       0x44
#define ADC1_JDR4_OFF       0x48
#define ADC1_DR_OFF         0x4C

/* SR bits */
#define ADC_SR_AWD          (1u << 0)
#define ADC_SR_EOC          (1u << 1)
#define ADC_SR_JEOC         (1u << 2)
#define ADC_SR_JSTRT        (1u << 3)
#define ADC_SR_STRT         (1u << 4)
#define ADC_SR_CLEARABLE    0x1Fu

/* CR1 bits */
#define ADC_CR1_SCAN        (1u << 8)

/* CR2 bits */
#define ADC_CR2_ADON        (1u << 0)
#define ADC_CR2_CONT        (1u << 1)
#define ADC_CR2_CAL         (1u << 2)
#define ADC_CR2_RSTCAL      (1u << 3)
#define ADC_CR2_DMA         (1u << 8)
#define ADC_CR2_ALIGN       (1u << 11)
#define ADC_CR2_JEXTSEL_MASK (7u << 12)
#define ADC_CR2_JEXTTRIG    (1u << 15)
#define ADC_CR2_EXTSEL_MASK (7u << 17)
#define ADC_CR2_EXTTRIG     (1u << 20)
#define ADC_CR2_JSWSTART    (1u << 21)
#define ADC_CR2_SWSTART     (1u << 22)
#define ADC_CR2_TSVREFE     (1u << 23)

#define ADC_CR2_VALID_MASK  (ADC_CR2_ADON      | ADC_CR2_CONT      | \
                             ADC_CR2_CAL       | ADC_CR2_RSTCAL    | \
                             ADC_CR2_DMA       | ADC_CR2_ALIGN     | \
                             ADC_CR2_JEXTSEL_MASK | ADC_CR2_JEXTTRIG | \
                             ADC_CR2_EXTSEL_MASK  | ADC_CR2_EXTTRIG  | \
                             ADC_CR2_JSWSTART  | ADC_CR2_SWSTART   | \
                             ADC_CR2_TSVREFE)

#define ADC1_DMA_CONTROLLER 1
#define ADC1_DMA_STREAM     1   /* STM32F103 ADC1 -> DMA1 Channel 1 */

/*
 * Coarse periodic cadence for continuous conversion mode.
 * Chosen to match the observed DMA-side runtime periodicity (~10 ms).
 */
#define ADC1_CONT_PERIOD_NS 10000000ULL

typedef struct ADC1State {
    uint32_t sr;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t smpr1;
    uint32_t smpr2;
    uint32_t jofr1;
    uint32_t jofr2;
    uint32_t jofr3;
    uint32_t jofr4;
    uint32_t htr;
    uint32_t ltr;
    uint32_t sqr1;
    uint32_t sqr2;
    uint32_t sqr3;
    uint32_t jsqr;
    uint32_t jdr1;
    uint32_t jdr2;
    uint32_t jdr3;
    uint32_t jdr4;
    uint32_t dr;

    bool running_regular;
    int current_rank;

    uint64_t cont_timer;
} ADC1State;

static ADC1State g_adc1;

static uint32_t adc1_size_mask(unsigned size)
{
    if (size >= 4) {
        return 0xFFFFFFFFu;
    }
    return (1u << (size * 8)) - 1u;
}

static int adc1_regular_seq_len(ADC1State *s)
{
    return ((s->sqr1 >> 20) & 0xF) + 1;
}

static int adc1_channel_for_rank(ADC1State *s, int rank)
{
    if (rank < 1 || rank > 16) {
        return 0;
    }

    if (rank <= 6) {
        return (s->sqr3 >> ((rank - 1) * 5)) & 0x1F;
    } else if (rank <= 12) {
        return (s->sqr2 >> ((rank - 7) * 5)) & 0x1F;
    } else {
        return (s->sqr1 >> ((rank - 13) * 5)) & 0x1F;
    }
}

static uint16_t adc1_format_sample(ADC1State *s, uint16_t sample)
{
    sample &= 0x0FFFu;
    if (s->cr2 & ADC_CR2_ALIGN) {
        return (uint16_t)(sample << 4);
    }
    return sample;
}

static void adc1_dma_push(ADC1State *s)
{
    if (!(s->cr2 & ADC_CR2_DMA)) {
        return;
    }

    uint16_t v = (uint16_t)(s->dr & 0xFFFFu);
    uint8_t buf[2];
    buf[0] = (uint8_t)(v & 0xFF);
    buf[1] = (uint8_t)((v >> 8) & 0xFF);

    if (api_dma_request_data(ADC1_DMA_CONTROLLER,
                             ADC1_DMA_STREAM,
                             (uint32_t)(ADC1_BASE + ADC1_DR_OFF),
                             buf,
                             2) < 0) {
        dev_debug("adc1: dma request failed");
    }
}

static void adc1_complete_regular_conversion(ADC1State *s)
{
    if (!(s->cr2 & ADC_CR2_ADON) || !s->running_regular) {
        return;
    }

    int seq_len = adc1_regular_seq_len(s);
    if (s->current_rank < 1 || s->current_rank > seq_len) {
        s->current_rank = 1;
    }

    int channel = adc1_channel_for_rank(s, s->current_rank);
    uint16_t raw = api_adc_get_sample(1, channel);
    uint16_t formatted = adc1_format_sample(s, raw);

    s->dr = formatted;
    s->sr |= ADC_SR_EOC;

    adc1_dma_push(s);

    if ((s->cr1 & ADC_CR1_SCAN) && seq_len > 1) {
        s->current_rank++;
        if (s->current_rank > seq_len) {
            s->current_rank = 1;
        }
    } else {
        s->current_rank = 1;
    }

    if (!(s->cr2 & ADC_CR2_CONT)) {
        s->running_regular = false;
    }
}

static void adc1_start_regular_conversion(ADC1State *s)
{
    if (!(s->cr2 & ADC_CR2_ADON)) {
        return;
    }

    s->running_regular = true;
    s->current_rank = 1;
    s->sr |= ADC_SR_STRT;

    /* Immediate completion for software-visible progress. */
    adc1_complete_regular_conversion(s);

    s->sr &= ~ADC_SR_STRT;
}

static void adc1_timer_cb(void *opaque)
{
    ADC1State *s = (ADC1State *)opaque;

    if (!(s->cr2 & ADC_CR2_ADON)) {
        return;
    }
    if (!(s->cr2 & ADC_CR2_CONT)) {
        return;
    }
    if (!s->running_regular) {
        return;
    }

    adc1_complete_regular_conversion(s);
}

static uint32_t adc1_read_reg32(ADC1State *s, hwaddr reg)
{
    switch (reg) {
    case ADC1_SR_OFF:    return s->sr;
    case ADC1_CR1_OFF:   return s->cr1;
    case ADC1_CR2_OFF:   return s->cr2;
    case ADC1_SMPR1_OFF: return s->smpr1;
    case ADC1_SMPR2_OFF: return s->smpr2;
    case ADC1_JOFR1_OFF: return s->jofr1;
    case ADC1_JOFR2_OFF: return s->jofr2;
    case ADC1_JOFR3_OFF: return s->jofr3;
    case ADC1_JOFR4_OFF: return s->jofr4;
    case ADC1_HTR_OFF:   return s->htr;
    case ADC1_LTR_OFF:   return s->ltr;
    case ADC1_SQR1_OFF:  return s->sqr1;
    case ADC1_SQR2_OFF:  return s->sqr2;
    case ADC1_SQR3_OFF:  return s->sqr3;
    case ADC1_JSQR_OFF:  return s->jsqr;
    case ADC1_JDR1_OFF:  return s->jdr1;
    case ADC1_JDR2_OFF:  return s->jdr2;
    case ADC1_JDR3_OFF:  return s->jdr3;
    case ADC1_JDR4_OFF:  return s->jdr4;
    case ADC1_DR_OFF:
        /*
         * If firmware reads DR before any completed conversion but ADC is on,
         * synthesize a one-shot sample so the register is useful.
         */
        if (!(s->sr & ADC_SR_EOC) && (s->cr2 & ADC_CR2_ADON) && !s->running_regular) {
            s->running_regular = true;
            adc1_complete_regular_conversion(s);
        }
        return s->dr;
    default:
        return 0;
    }
}

static void adc1_write_reg32(ADC1State *s, hwaddr reg, uint32_t value)
{
    switch (reg) {
    case ADC1_SR_OFF:
        /*
         * STM32-style write-clear behavior:
         * writing 0 clears a flag, writing 1 leaves it unchanged.
         */
        s->sr &= (value | ~ADC_SR_CLEARABLE);
        break;

    case ADC1_CR1_OFF:
        s->cr1 = value;
        break;

    case ADC1_CR2_OFF: {
        uint32_t old = s->cr2;
        bool swstart = (value & (ADC_CR2_SWSTART | ADC_CR2_JSWSTART)) != 0;

        s->cr2 = value & ADC_CR2_VALID_MASK;

        /* Calibration bits are hardware-owned/self-clearing. */
        s->cr2 &= ~(ADC_CR2_CAL | ADC_CR2_RSTCAL);

        if (!(s->cr2 & ADC_CR2_ADON)) {
            s->running_regular = false;
        }

        if (((old ^ s->cr2) & ADC_CR2_ADON) && (s->cr2 & ADC_CR2_ADON)) {
            adc1_start_regular_conversion(s);
        } else if (swstart && (s->cr2 & ADC_CR2_ADON)) {
            adc1_start_regular_conversion(s);
        }

        /* Software start bits do not persist. */
        s->cr2 &= ~(ADC_CR2_SWSTART | ADC_CR2_JSWSTART);
        break;
    }

    case ADC1_SMPR1_OFF:
        s->smpr1 = value;
        break;

    case ADC1_SMPR2_OFF:
        s->smpr2 = value;
        break;

    case ADC1_JOFR1_OFF:
        s->jofr1 = value & 0x0FFFu;
        break;

    case ADC1_JOFR2_OFF:
        s->jofr2 = value & 0x0FFFu;
        break;

    case ADC1_JOFR3_OFF:
        s->jofr3 = value & 0x0FFFu;
        break;

    case ADC1_JOFR4_OFF:
        s->jofr4 = value & 0x0FFFu;
        break;

    case ADC1_HTR_OFF:
        s->htr = value & 0x0FFFu;
        break;

    case ADC1_LTR_OFF:
        s->ltr = value & 0x0FFFu;
        break;

    case ADC1_SQR1_OFF:
        s->sqr1 = value;
        break;

    case ADC1_SQR2_OFF:
        s->sqr2 = value;
        break;

    case ADC1_SQR3_OFF:
        s->sqr3 = value;
        break;

    case ADC1_JSQR_OFF:
        s->jsqr = value;
        break;

    case ADC1_DR_OFF:
        /* Read-only in hardware; ignore writes. */
        break;

    default:
        break;
    }
}

uint64_t adc1_read(void *opaque, hwaddr addr, unsigned size)
{
    ADC1State *s = (ADC1State *)opaque;
    hwaddr offset = addr - ADC1_BASE;
    hwaddr reg = offset & ~0x3ULL;
    unsigned shift = (unsigned)((offset & 0x3ULL) * 8);
    uint32_t mask = adc1_size_mask(size);
    uint32_t regv = adc1_read_reg32(s, reg);
    uint64_t ret = (regv >> shift) & mask;

    if (reg == ADC1_DR_OFF) {
        s->sr &= ~ADC_SR_EOC;
    }

    return ret;
}

void adc1_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    ADC1State *s = (ADC1State *)opaque;
    hwaddr offset = addr - ADC1_BASE;
    hwaddr reg = offset & ~0x3ULL;
    unsigned shift = (unsigned)((offset & 0x3ULL) * 8);
    uint32_t mask = adc1_size_mask(size);
    uint32_t oldv = adc1_read_reg32(s, reg);
    uint32_t merged = (oldv & ~(mask << shift)) | (((uint32_t)value & mask) << shift);

    adc1_write_reg32(s, reg, merged);
}

// MUST return &g_state — framework stores this and passes it as opaque to _read/_write
void* adc1_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_adc1, 0, sizeof(g_adc1));

    /* Reset-like defaults */
    g_adc1.htr = 0x00000FFFu;
    g_adc1.ltr = 0x00000000u;
    g_adc1.sqr1 = 0x00000000u; /* regular sequence length = 1 */
    g_adc1.current_rank = 1;
    g_adc1.running_regular = false;

    g_adc1.cont_timer = qemu_plugin_timer_new_period_ns(adc1_timer_cb,
                                                        &g_adc1,
                                                        ADC1_CONT_PERIOD_NS);

    return &g_adc1;
}