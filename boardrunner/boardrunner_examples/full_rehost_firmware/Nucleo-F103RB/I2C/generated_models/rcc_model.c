// Device Model for RCC

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

// Inferred Register Functions:
// CR      (0x00): Clock control. Writable control bits, read-only ready/calibration bits.
// CFGR    (0x04): Clock config. Writable config bits, read-only SWS status bits.
// CIR     (0x08): Clock interrupt register. Kept as simple stateful storage.
// APB2RSTR(0x0C): Peripheral reset register. Simple stateful storage.
// APB1RSTR(0x10): Peripheral reset register. Simple stateful storage.
// AHBENR  (0x14): AHB enable register. Simple stateful storage.
// APB2ENR (0x18): APB2 enable register. Stateful R/W.
// APB1ENR (0x1C): APB1 enable register. Simple stateful storage.
// BDCR    (0x20): Backup domain control register. Simple stateful storage.
// CSR     (0x24): Control/status register. Simple stateful storage.

#define RCC_BASE        0x40021000ULL

#define RCC_CR_OFF      0x00
#define RCC_CFGR_OFF    0x04
#define RCC_CIR_OFF     0x08
#define RCC_APB2RSTR_OFF 0x0C
#define RCC_APB1RSTR_OFF 0x10
#define RCC_AHBENR_OFF  0x14
#define RCC_APB2ENR_OFF 0x18
#define RCC_APB1ENR_OFF 0x1C
#define RCC_BDCR_OFF    0x20
#define RCC_CSR_OFF     0x24

// RCC_CR bits
#define RCC_CR_HSION        (1u << 0)
#define RCC_CR_HSIRDY       (1u << 1)   // read-only status
#define RCC_CR_HSITRIM_MASK (0x1Fu << 3)
#define RCC_CR_HSICAL_MASK  (0xFFu << 8) // read-only calibration value
#define RCC_CR_HSEON        (1u << 16)
#define RCC_CR_HSERDY       (1u << 17)  // read-only status
#define RCC_CR_HSEBYP       (1u << 18)
#define RCC_CR_CSSON        (1u << 19)
#define RCC_CR_PLLON        (1u << 24)
#define RCC_CR_PLLRDY       (1u << 25)  // read-only status

#define RCC_CR_WRITABLE_MASK \
    (RCC_CR_HSION | RCC_CR_HSITRIM_MASK | RCC_CR_HSEON | \
     RCC_CR_HSEBYP | RCC_CR_CSSON | RCC_CR_PLLON)

#define RCC_CR_READONLY_MASK \
    (RCC_CR_HSIRDY | RCC_CR_HSERDY | RCC_CR_PLLRDY | RCC_CR_HSICAL_MASK)

// RCC_CFGR bits
#define RCC_CFGR_SW_MASK    (0x3u << 0)
#define RCC_CFGR_SWS_MASK   (0x3u << 2) // read-only status

typedef struct {
    uint32_t cr;
    uint32_t cfgr;
    uint32_t cir;
    uint32_t apb2rstr;
    uint32_t apb1rstr;
    uint32_t ahbenr;
    uint32_t apb2enr;
    uint32_t apb1enr;
    uint32_t bdcr;
    uint32_t csr;
    uint32_t sysclk_status; // 0=HSI, 1=HSE, 2=PLL
} RCCState;

static RCCState g_rcc;

static uint64_t rcc_size_mask(unsigned size)
{
    switch (size) {
    case 1: return 0xFFu;
    case 2: return 0xFFFFu;
    case 4: return 0xFFFFFFFFu;
    case 8: return 0xFFFFFFFFFFFFFFFFULL;
    default:
        return 0xFFFFFFFFu;
    }
}

static void rcc_debug_unknown(const char *op, hwaddr addr, unsigned size, uint64_t value)
{
    char buf[128];
    snprintf(buf, sizeof(buf),
             "RCC %s unknown access addr=0x%llx size=%u value=0x%llx\n",
             op,
             (unsigned long long)addr,
             size,
             (unsigned long long)value);
    dev_debug(buf);
}

static void rcc_update_cr_status(RCCState *s)
{
    // HSI ready follows HSION immediately in this model.
    if (s->cr & RCC_CR_HSION) {
        s->cr |= RCC_CR_HSIRDY;
    } else {
        s->cr &= ~RCC_CR_HSIRDY;
    }

    // HSE ready follows HSEON immediately in this model.
    if (s->cr & RCC_CR_HSEON) {
        s->cr |= RCC_CR_HSERDY;
    } else {
        s->cr &= ~RCC_CR_HSERDY;
    }

    // PLL ready follows PLLON immediately in this model.
    if (s->cr & RCC_CR_PLLON) {
        s->cr |= RCC_CR_PLLRDY;
    } else {
        s->cr &= ~RCC_CR_PLLRDY;
    }
}

static void rcc_update_sysclk_status(RCCState *s)
{
    uint32_t sw = s->cfgr & RCC_CFGR_SW_MASK;

    switch (sw) {
    case 0x0: // HSI
        s->sysclk_status = 0;
        break;
    case 0x1: // HSE
        s->sysclk_status = (s->cr & RCC_CR_HSERDY) ? 1 : 0;
        break;
    case 0x2: // PLL
        s->sysclk_status = (s->cr & RCC_CR_PLLRDY) ? 2 : 0;
        break;
    default: // reserved -> keep a safe value
        s->sysclk_status = 0;
        break;
    }
}

static uint32_t rcc_cfgr_readback(RCCState *s)
{
    uint32_t v = s->cfgr & ~RCC_CFGR_SWS_MASK;
    v |= ((s->sysclk_status & 0x3u) << 2);
    return v;
}

static uint32_t rcc_read_reg(RCCState *s, hwaddr reg_off, bool *known)
{
    *known = true;

    switch (reg_off) {
    case RCC_CR_OFF:
        return s->cr;
    case RCC_CFGR_OFF:
        return rcc_cfgr_readback(s);
    case RCC_CIR_OFF:
        return s->cir;
    case RCC_APB2RSTR_OFF:
        return s->apb2rstr;
    case RCC_APB1RSTR_OFF:
        return s->apb1rstr;
    case RCC_AHBENR_OFF:
        return s->ahbenr;
    case RCC_APB2ENR_OFF:
        return s->apb2enr;
    case RCC_APB1ENR_OFF:
        return s->apb1enr;
    case RCC_BDCR_OFF:
        return s->bdcr;
    case RCC_CSR_OFF:
        return s->csr;
    default:
        *known = false;
        return 0;
    }
}

static void rcc_write_reg(RCCState *s, hwaddr reg_off, uint32_t value, bool *known)
{
    *known = true;

    switch (reg_off) {
    case RCC_CR_OFF: {
        uint32_t preserved_ro = s->cr & RCC_CR_READONLY_MASK;
        uint32_t writable = value & RCC_CR_WRITABLE_MASK;

        s->cr = preserved_ro | writable;

        // Keep factory calibration constant from reset model.
        s->cr &= ~RCC_CR_HSICAL_MASK;
        s->cr |= (0x57u << 8);

        rcc_update_cr_status(s);
        rcc_update_sysclk_status(s);
        break;
    }

    case RCC_CFGR_OFF:
        // SWS is read-only status; all other bits are kept statefully.
        s->cfgr = (value & ~RCC_CFGR_SWS_MASK);
        rcc_update_sysclk_status(s);
        break;

    case RCC_CIR_OFF:
        s->cir = value;
        break;

    case RCC_APB2RSTR_OFF:
        s->apb2rstr = value;
        break;

    case RCC_APB1RSTR_OFF:
        s->apb1rstr = value;
        break;

    case RCC_AHBENR_OFF:
        s->ahbenr = value;
        break;

    case RCC_APB2ENR_OFF:
        s->apb2enr = value;
        break;

    case RCC_APB1ENR_OFF:
        s->apb1enr = value;
        break;

    case RCC_BDCR_OFF:
        s->bdcr = value;
        break;

    case RCC_CSR_OFF:
        s->csr = value;
        break;

    default:
        *known = false;
        break;
    }
}

// This function will emulate all device reads
uint64_t rcc_read(void *opaque, hwaddr addr, unsigned size)
{
    RCCState *s = (RCCState *)opaque;
    hwaddr offset = addr - RCC_BASE;
    hwaddr reg_off = offset & ~0x3ULL;
    unsigned shift = (unsigned)(offset & 0x3ULL) * 8;
    uint64_t mask = rcc_size_mask(size);
    bool known = false;
    uint32_t regv = rcc_read_reg(s, reg_off, &known);

    if (!known) {
        rcc_debug_unknown("read", addr, size, 0);
        return 0;
    }

    return (regv >> shift) & mask;
}

// This function will emulate all device writes
void rcc_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    RCCState *s = (RCCState *)opaque;
    hwaddr offset = addr - RCC_BASE;
    hwaddr reg_off = offset & ~0x3ULL;
    unsigned shift = (unsigned)(offset & 0x3ULL) * 8;
    uint64_t mask = rcc_size_mask(size);
    bool known = false;
    uint32_t cur = rcc_read_reg(s, reg_off, &known);

    if (!known) {
        rcc_debug_unknown("write", addr, size, value);
        return;
    }

    uint32_t newv = cur;
    newv &= ~((uint32_t)(mask << shift));
    newv |= ((uint32_t)((value & mask) << shift));

    rcc_write_reg(s, reg_off, newv, &known);

    if (!known) {
        rcc_debug_unknown("write", addr, size, value);
    }
}

// MUST return pointer to state — framework passes it as opaque to _read/_write
void* rcc_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_rcc, 0, sizeof(g_rcc));

    // Reset-like defaults chosen from the observed trace and STM32F103 RCC behavior.
    // CR observed at 0x00005783 before PLL enable:
    // - HSION=1, HSIRDY=1
    // - HSITRIM default
    // - HSICAL observed as 0x57
    g_rcc.cr = 0x00005783u;

    // Reset/default values.
    g_rcc.cfgr = 0x00000000u;
    g_rcc.cir = 0x00000000u;
    g_rcc.apb2rstr = 0x00000000u;
    g_rcc.apb1rstr = 0x00000000u;
    g_rcc.ahbenr = 0x00000014u;   // typical STM32F1 reset: FLITFEN + SRAMEN
    g_rcc.apb2enr = 0x00000000u;
    g_rcc.apb1enr = 0x00000000u;
    g_rcc.bdcr = 0x00000000u;
    g_rcc.csr = 0x0C000000u;      // common reset flags state on STM32F1

    rcc_update_cr_status(&g_rcc);
    rcc_update_sysclk_status(&g_rcc);

    return &g_rcc;
}