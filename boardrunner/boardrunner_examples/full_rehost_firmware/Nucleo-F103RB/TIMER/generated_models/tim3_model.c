// Device Model for TIM3

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define TIM3_BASE_ADDR        0x40000400ULL
#define TIM3_IRQ_EXCEPTION    45   /* NVIC IRQn 29 + 16 */

/*
 * STM32F103 TIM3 is on APB1; with the common F1 setup used here the timer
 * clock is 72 MHz (APB1 timer clock doubled when the APB prescaler != 1).
 * 64 MHz makes CNT consistently lag hardware.
 */
#define TIM3_INPUT_CLK_HZ     72000000ULL

/* Register offsets */
#define TIM3_CR1_OFF          0x00
#define TIM3_SMCR_OFF         0x08
#define TIM3_DIER_OFF         0x0C
#define TIM3_SR_OFF           0x10
#define TIM3_EGR_OFF          0x14
#define TIM3_CNT_OFF          0x24
#define TIM3_PSC_OFF          0x28
#define TIM3_ARR_OFF          0x2C

/* Bit definitions used by this model */
#define TIM_CR1_CEN           (1U << 0)
#define TIM_CR1_UDIS          (1U << 1)
#define TIM_CR1_URS           (1U << 2)

#define TIM_DIER_UIE          (1U << 0)

#define TIM_SR_UIF            (1U << 0)
#define TIM_SR_TRACE_STICKY   (0x1EU)  /* Observed stable readback: SR=0x1E/0x1F */

#define TIM_EGR_UG            (1U << 0)

typedef struct TIM3State {
    uint16_t cr1;
    uint16_t dier;
    uint16_t sr;

    uint16_t psc_shadow;
    uint16_t psc_active;
    uint16_t arr;
    uint16_t cnt;

    uint64_t last_ns;
    uint64_t frac_num;     /* remainder of elapsed time conversion */
    uint64_t alarm_handle;

    bool irq_raised_for_current_uif;
} TIM3State;

static TIM3State g_tim3;

static uint64_t tim3_now_ns(void)
{
    int64_t now = qemu_plugin_get_virtual_timer();
    return (now < 0) ? 0ULL : (uint64_t)now;
}

static uint64_t tim3_size_mask(unsigned size)
{
    switch (size) {
    case 1: return 0xFFULL;
    case 2: return 0xFFFFULL;
    default: return 0xFFFFFFFFULL;
    }
}

static void tim3_force_trace_sr_shape(TIM3State *s)
{
    s->sr |= TIM_SR_TRACE_STICKY;
}

static void tim3_maybe_raise_irq(TIM3State *s)
{
    if ((s->sr & TIM_SR_UIF) &&
        (s->dier & TIM_DIER_UIE) &&
        !s->irq_raised_for_current_uif) {
        qemu_plugin_raise_irq(TIM3_IRQ_EXCEPTION, false);
        s->irq_raised_for_current_uif = true;
    }
}

static void tim3_sync(TIM3State *s, bool allow_irq)
{
    uint64_t now = tim3_now_ns();

    if (!(s->cr1 & TIM_CR1_CEN)) {
        s->last_ns = now;
        return;
    }

    if (now <= s->last_ns) {
        return;
    }

    uint64_t delta_ns = now - s->last_ns;
    uint64_t psc_div = (uint64_t)s->psc_active + 1ULL;
    uint64_t denom = psc_div * 1000000000ULL;
    uint32_t period = (uint32_t)s->arr + 1U;

    if (period == 0) {
        period = 65536U;
    }

    unsigned __int128 numer =
        (unsigned __int128)s->frac_num +
        (unsigned __int128)delta_ns * (unsigned __int128)TIM3_INPUT_CLK_HZ;

    uint64_t ticks = (uint64_t)(numer / denom);
    s->frac_num = (uint64_t)(numer % denom);
    s->last_ns = now;

    if (ticks == 0) {
        return;
    }

    uint64_t total = (uint64_t)s->cnt + ticks;
    uint64_t overflows = total / period;
    s->cnt = (uint16_t)(total % period);

    if (overflows > 0 && !(s->cr1 & TIM_CR1_UDIS)) {
        if (!(s->sr & TIM_SR_UIF)) {
            s->irq_raised_for_current_uif = false;
        }
        s->sr |= TIM_SR_UIF;
        tim3_force_trace_sr_shape(s);

        if (allow_irq) {
            tim3_maybe_raise_irq(s);
        }
    }
}

static void tim3_schedule_next_alarm(TIM3State *s)
{
    if (!(s->cr1 & TIM_CR1_CEN)) {
        return;
    }

    if (s->cr1 & TIM_CR1_UDIS) {
        return;
    }

    if (!(s->dier & TIM_DIER_UIE)) {
        return;
    }

    uint32_t period = (uint32_t)s->arr + 1U;
    if (period == 0) {
        period = 65536U;
    }

    uint32_t ticks_to_overflow = period - (uint32_t)s->cnt;
    uint64_t psc_div = (uint64_t)s->psc_active + 1ULL;
    uint64_t denom = psc_div * 1000000000ULL;

    unsigned __int128 need = (unsigned __int128)ticks_to_overflow *
                             (unsigned __int128)denom;

    if (need > s->frac_num) {
        need -= s->frac_num;
    } else {
        need = 1;
    }

    uint64_t delta_ns =
        (uint64_t)((need + (TIM3_INPUT_CLK_HZ - 1ULL)) / TIM3_INPUT_CLK_HZ);
    if (delta_ns == 0) {
        delta_ns = 1;
    }

    qemu_plugin_timer_alarm(s->alarm_handle, tim3_now_ns() + delta_ns);
}

static void tim3_apply_update_event(TIM3State *s, bool from_ug)
{
    s->psc_active = s->psc_shadow;
    s->cnt = 0;
    s->frac_num = 0;
    s->last_ns = tim3_now_ns();

    if ((s->cr1 & TIM_CR1_UDIS) != 0) {
        return;
    }

    if (from_ug && (s->cr1 & TIM_CR1_URS)) {
        return;
    }

    if (!(s->sr & TIM_SR_UIF)) {
        s->irq_raised_for_current_uif = false;
    }
    s->sr |= TIM_SR_UIF;
    tim3_force_trace_sr_shape(s);
    tim3_maybe_raise_irq(s);
}

static void tim3_alarm_cb(void *opaque)
{
    TIM3State *s = (TIM3State *)opaque;

    tim3_sync(s, true);
    tim3_maybe_raise_irq(s);
    tim3_schedule_next_alarm(s);
}

uint64_t tim3_read(void *opaque, hwaddr addr, unsigned size)
{
    TIM3State *s = (TIM3State *)opaque;
    hwaddr offset = addr - TIM3_BASE_ADDR;
    uint64_t val = 0;

    tim3_sync(s, true);

    switch (offset) {
    case TIM3_CR1_OFF:
        val = s->cr1;
        break;
    case TIM3_SMCR_OFF:
        val = 0;
        break;
    case TIM3_DIER_OFF:
        val = s->dier;
        break;
    case TIM3_SR_OFF:
        tim3_force_trace_sr_shape(s);
        val = s->sr;
        break;
    case TIM3_CNT_OFF:
        val = s->cnt;
        break;
    case TIM3_PSC_OFF:
        val = s->psc_shadow;
        break;
    case TIM3_ARR_OFF:
        val = s->arr;
        break;
    case TIM3_EGR_OFF:
        val = 0;
        break;
    default:
        val = 0;
        break;
    }

    return val & tim3_size_mask(size);
}

void tim3_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    TIM3State *s = (TIM3State *)opaque;
    hwaddr offset = addr - TIM3_BASE_ADDR;
    uint16_t v16 = (uint16_t)(value & tim3_size_mask(size));

    (void)size;

    tim3_sync(s, true);

    switch (offset) {
    case TIM3_CR1_OFF: {
        uint16_t old_cr1 = s->cr1;
        s->cr1 = v16 & 0x03FFU;

        if ((old_cr1 & TIM_CR1_CEN) == 0 && (s->cr1 & TIM_CR1_CEN) != 0) {
            s->last_ns = tim3_now_ns();
            s->frac_num = 0;
        } else if ((old_cr1 & TIM_CR1_CEN) != 0 && (s->cr1 & TIM_CR1_CEN) == 0) {
            s->last_ns = tim3_now_ns();
            s->frac_num = 0;
        }

        tim3_maybe_raise_irq(s);
        tim3_schedule_next_alarm(s);
        break;
    }

    case TIM3_DIER_OFF:
        s->dier = v16;
        tim3_maybe_raise_irq(s);
        tim3_schedule_next_alarm(s);
        break;

    case TIM3_SR_OFF:
        /* STM32 timer status bits are typically cleared by writing 0. */
        if ((v16 & TIM_SR_UIF) == 0) {
            s->sr &= ~TIM_SR_UIF;
            s->irq_raised_for_current_uif = false;
        }
        tim3_force_trace_sr_shape(s);
        tim3_maybe_raise_irq(s);
        tim3_schedule_next_alarm(s);
        break;

    case TIM3_EGR_OFF:
        if (v16 & TIM_EGR_UG) {
            tim3_apply_update_event(s, true);
            tim3_schedule_next_alarm(s);
        }
        break;

    case TIM3_CNT_OFF: {
        uint32_t period = (uint32_t)s->arr + 1U;
        if (period == 0) {
            period = 65536U;
        }
        s->cnt = (uint16_t)((uint32_t)v16 % period);
        s->frac_num = 0;
        s->last_ns = tim3_now_ns();
        tim3_schedule_next_alarm(s);
        break;
    }

    case TIM3_PSC_OFF:
        /* PSC is buffered; it takes effect on update event / UG. */
        s->psc_shadow = v16;
        break;

    case TIM3_ARR_OFF: {
        uint32_t period;
        s->arr = v16;
        period = (uint32_t)s->arr + 1U;
        if (period == 0) {
            period = 65536U;
        }
        if (s->cnt >= period) {
            s->cnt = (uint16_t)(s->cnt % period);
        }
        tim3_schedule_next_alarm(s);
        break;
    }

    default:
        break;
    }
}

/* MUST return &g_state — framework stores this and passes it as opaque to _read/_write */
void* tim3_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_tim3, 0, sizeof(g_tim3));

    /*
     * Seed from reset/observed runtime behavior:
     * - CR1 reset/readback is 0 during init.
     * - DIER first hardware read is 0, then firmware explicitly writes UIE=1.
     *   Do not pre-enable interrupts in the model.
     * - ARR default to 0xFFFF, PSC default 0.
     * - SR reads preserve the observed sticky 0x1E baseline, with UIF toggling
     *   on real update events to produce 0x1E/0x1F.
     */
    g_tim3.cr1 = 0x0000;
    g_tim3.dier = 0x0000;
    g_tim3.sr = TIM_SR_TRACE_STICKY;

    g_tim3.psc_shadow = 0x0000;
    g_tim3.psc_active = 0x0000;
    g_tim3.arr = 0xFFFF;
    g_tim3.cnt = 0x0000;

    g_tim3.last_ns = tim3_now_ns();
    g_tim3.frac_num = 0;
    g_tim3.irq_raised_for_current_uif = false;

    g_tim3.alarm_handle = qemu_plugin_timer_new_ns(tim3_alarm_cb, &g_tim3);

    return &g_tim3;
}