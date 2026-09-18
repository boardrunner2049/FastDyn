// Device Model for SDMMC1 (STM32F769)
//
// Inferred Register Functions:
// POWER   : power/control
// CLKCR   : clock config (RMW)
// ARG     : command argument
// CMD     : command + CPSMEN; must read back last written value
// RESPCMD : last response command index
// RESP1-4 : response registers (short/long)
// DTIMER  : data timeout
// DLEN    : data length
// DCTRL   : data control (enable + dir)
// STA     : status (sticky + dynamic FIFO bits)
// ICR     : W1C clear of status bits
// FIFO    : 32-bit FIFO data port

#include <device.h>
#include <boardrunner/vio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define SDMMC1_BASE 0x40012C00u

#define REG_POWER   0x00
#define REG_CLKCR   0x04
#define REG_ARG     0x08
#define REG_CMD     0x0C
#define REG_RESPCMD 0x10
#define REG_RESP1   0x14
#define REG_RESP2   0x18
#define REG_RESP3   0x1C
#define REG_RESP4   0x20
#define REG_DTIMER  0x24
#define REG_DLEN    0x28
#define REG_DCTRL   0x2C
#define REG_STA     0x34
#define REG_ICR     0x38
#define REG_FIFO    0x80

// STA bits used in your traces
#define STA_CCRCFAIL (1u << 0)
#define STA_CTIMEOUT (1u << 2)
#define STA_CMDREND  (1u << 6)
#define STA_CMDSENT  (1u << 7)
#define STA_DATAEND  (1u << 8)
#define STA_DBCKEND  (1u << 10)
#define STA_TXACT    (1u << 12)
#define STA_RXACT    (1u << 13)
#define STA_TXFIFOHE (1u << 14)
#define STA_TXDAVL   (1u << 20)
#define STA_RXDAVL   (1u << 21)

// DCTRL bits
#define DCTRL_DTEN   (1u << 0)
#define DCTRL_DTDIR  (1u << 1)  // 1 = Card->Host (Read)

#define CMD_CPSMEN  (1u << 10)

typedef struct {
    uint32_t power, clkcr, arg, cmd;
    uint32_t respcmd;
    uint32_t resp[4];
    uint32_t dtimer, dlen, dctrl;

    // sticky status latch; ICR is W1C against this
    uint32_t sta_latch;

    // card state inferred from init trace
    bool     app_cmd_pending;
    uint16_t rca;
    bool     selected;
    uint32_t acmd41_tries;

    // FIFO/data state
    uint8_t  fifo[512];
    uint32_t fifo_len;
    uint32_t fifo_pos;
    bool     data_active;
    bool     data_is_read;

    // backing store
    int      host_fd;
    uint64_t card_size;
} SDMMC1_State;

static SDMMC1_State s;

static inline uint32_t off_from_addr(hwaddr addr) {
    uint64_t a = (uint64_t)addr;
    if (a >= (uint64_t)SDMMC1_BASE) return (uint32_t)(a - (uint64_t)SDMMC1_BASE);
    return (uint32_t)addr;
}

static inline uint32_t mask_for_size(unsigned size) {
    if (size == 1) return 0xFFu;
    if (size == 2) return 0xFFFFu;
    return 0xFFFFFFFFu;
}

static inline uint32_t apply_partial_write(uint32_t oldv, uint64_t value, hwaddr addr, unsigned size) {
    uint32_t shift = ((uint32_t)addr & 3u) * 8u;
    uint32_t mask  = mask_for_size(size) << shift;
    uint32_t nv    = (uint32_t)value;
    return (oldv & ~mask) | ((nv << shift) & mask);
}

static void set_resp4(uint32_t r1, uint32_t r2, uint32_t r3, uint32_t r4) {
    s.resp[0] = r1;
    s.resp[1] = r2;
    s.resp[2] = r3;
    s.resp[3] = r4;
}

static void fifo_load(const void *buf, uint32_t len) {
    if (len > sizeof(s.fifo)) len = sizeof(s.fifo);
    memcpy(s.fifo, buf, len);
    s.fifo_len = len;
    s.fifo_pos = 0;
    s.data_active = true;
    s.data_is_read = true;
}

static void fifo_prepare_write(uint32_t len) {
    if (len > sizeof(s.fifo)) len = sizeof(s.fifo);
    memset(s.fifo, 0, len);
    s.fifo_len = len;
    s.fifo_pos = 0;
    s.data_active = true;
    s.data_is_read = false;
}

// SDHC-style addressing based on your ACMD41 response CCS=1 (0xC1FF8000)
static uint64_t arg_to_offset(uint32_t arg) {
    return (uint64_t)arg * 512ull;
}

static uint32_t compute_sta(void) {
    // Start from sticky latch and add dynamic FIFO bits.
    uint32_t sta = s.sta_latch;

    // Clear dynamic bits before recompute
    sta &= ~(STA_RXACT | STA_RXDAVL | STA_TXACT | STA_TXFIFOHE | STA_TXDAVL);

    if ((s.dctrl & DCTRL_DTEN) == 0 || !s.data_active) {
        return sta;
    }

    uint32_t remaining = (s.fifo_pos < s.fifo_len) ? (s.fifo_len - s.fifo_pos) : 0;

    if (s.data_is_read) {
        if (remaining > 0) sta |= (STA_RXACT | STA_RXDAVL);
    } else {
        if (remaining > 0) sta |= (STA_TXACT | STA_TXFIFOHE | STA_TXDAVL);
    }

    return sta;
}

static void maybe_finish_write_and_commit(void) {
    if (!s.data_active || s.data_is_read) return;
    if (s.fifo_pos < s.fifo_len) return;

    // data complete
    s.data_active = false;
    s.sta_latch |= (STA_DATAEND | STA_DBCKEND);

    // commit if we have storage
    if (s.host_fd >= 0 && s.card_size) {
        uint64_t off = arg_to_offset(s.arg);
        int want = (int)s.fifo_len;
        if (off + (uint64_t)want > s.card_size) {
            if (off >= s.card_size) want = 0;
            else want = (int)(s.card_size - off);
        }
        if (want > 0) (void)api_file_pwrite(s.host_fd, s.fifo, want, off);
    }
}

static void exec_cmd(uint32_t cmd_val) {
    uint32_t cmd_idx = cmd_val & 0x3Fu;

    // Clear only cmd completion/error flags (match trace behavior; data bits are separate)
    s.sta_latch &= ~(STA_CMDSENT | STA_CMDREND | STA_CCRCFAIL | STA_CTIMEOUT);

    s.respcmd = cmd_idx;
    set_resp4(0, 0, 0, 0);

    // CMD55: APP_CMD
    if (cmd_idx == 55) {
        uint16_t arg_rca = (uint16_t)((s.arg >> 16) & 0xFFFFu);

        // Trace shows 0x120 before select, 0x920 after select with matching RCA
        if (s.selected && s.rca != 0 && arg_rca == s.rca) s.resp[0] = 0x00000920u;
        else                                              s.resp[0] = 0x00000120u;

        s.app_cmd_pending = true;
        s.sta_latch |= STA_CMDREND;
        return;
    }

    // ACMDs (after CMD55)
    if (s.app_cmd_pending) {
        s.app_cmd_pending = false;

        if (cmd_idx == 41) { // ACMD41
            // EXACTLY as trace: RESP1 busy then ready, and STA=0x1 (CCRCFAIL) not CMDREND.
            s.acmd41_tries++;
            if (s.acmd41_tries == 1) s.resp[0] = 0x41FF8000u;
            else                     s.resp[0] = 0xC1FF8000u;

            s.sta_latch |= STA_CCRCFAIL; // makes STA read 0x1
            return;
        }

        if (cmd_idx == 51) { // ACMD51 SEND_SCR
            // Trace: DTIMER=FFFFFFFF, DLEN=8, DCTRL=0x33, then STA=0x202540
            // Provide SCR such that first word is 0x03803502 (little-endian bytes: 02 35 80 03)
            uint8_t scr[8] = { 0x02, 0x35, 0x80, 0x03, 0x00, 0x00, 0x00, 0x00 };
            uint32_t len = (s.dlen > 0 && s.dlen <= 8) ? s.dlen : 8;

            fifo_load(scr, len);

            // Hardware shows DATAEND+DBCKEND already set while RX bits are still active
            s.sta_latch |= (STA_CMDREND | STA_DATAEND | STA_DBCKEND);
            s.resp[0] = 0x00000920u;
            return;
        }

        // default ACMD success
        s.sta_latch |= STA_CMDREND;
        s.resp[0] = s.selected ? 0x00000920u : 0x00000120u;
        return;
    }

    // Standard commands from your init + command list
    switch (cmd_idx) {
        case 0: // CMD0 GO_IDLE_STATE
            s.rca = 0;
            s.selected = false;
            s.acmd41_tries = 0;
            s.app_cmd_pending = false;
            s.data_active = false;
            // Trace: STA read is 0x80 (CMDSENT)
            s.sta_latch |= STA_CMDSENT;
            break;

        case 8: // CMD8 SEND_IF_COND
            s.resp[0] = s.arg;     // echo 0x1AA
            s.sta_latch |= STA_CMDREND;
            break;

        case 2: // CMD2 ALL_SEND_CID (long resp)
            set_resp4(0x03534453u, 0x53313647u, 0x808DF7DDu, 0x5C0164E4u);
            s.sta_latch |= STA_CMDREND;
            break;

        case 3: // CMD3 SEND_RELATIVE_ADDR
            s.rca = 0xAAAAu;
            s.resp[0] = 0xAAAA0520u;
            s.sta_latch |= STA_CMDREND;
            break;

        case 9: // CMD9 SEND_CSD (long resp)
            set_resp4(0x400E0032u, 0x5B590000u, 0x76B27F80u, 0x0A404012u);
            s.sta_latch |= STA_CMDREND;
            break;

        case 7: { // CMD7 SELECT_CARD
            uint16_t sel_rca = (uint16_t)((s.arg >> 16) & 0xFFFFu);
            s.selected = (sel_rca != 0 && sel_rca == s.rca);
            s.resp[0] = 0x00000700u;
            s.sta_latch |= STA_CMDREND;
            break;
        }

        case 16: // CMD16 SET_BLOCKLEN (trace returns 0x900 for both 0x200 and 0x8 args)
            s.resp[0] = 0x00000900u;
            s.sta_latch |= STA_CMDREND;
            break;

        case 13: // CMD13 SEND_STATUS (seen in your cmd list)
            s.resp[0] = 0x00000900u;
            s.sta_latch |= STA_CMDREND;
            break;

        case 17: { // CMD17 READ_SINGLE_BLOCK
            uint32_t len = (s.dlen > 0) ? s.dlen : 512;
            if (len > 512) len = 512;

            memset(s.fifo, 0, 512);
            s.fifo_len = len;
            s.fifo_pos = 0;
            s.data_active = true;
            s.data_is_read = true;

            if (s.host_fd >= 0 && s.card_size) {
                uint64_t off = arg_to_offset(s.arg);
                int want = (int)len;
                if (off + (uint64_t)want > s.card_size) {
                    if (off >= s.card_size) want = 0;
                    else want = (int)(s.card_size - off);
                }
                if (want > 0) {
                    (void)api_file_pread(s.host_fd, s.fifo, want, off);
                }
            }

            // Like ACMD51 behavior: present CMDREND + DATAEND/DBCKEND while FIFO still has bytes
            s.resp[0] = 0x00000900u;
            s.sta_latch |= (STA_CMDREND | STA_DATAEND | STA_DBCKEND);
            break;
        }

        case 24: { // CMD24 WRITE_SINGLE_BLOCK
            uint32_t len = (s.dlen > 0) ? s.dlen : 512;
            if (len > 512) len = 512;

            fifo_prepare_write(len);

            s.resp[0] = 0x00000900u;
            s.sta_latch |= STA_CMDREND;
            // DATAEND/DBCKEND will be set once FIFO is fully written
            break;
        }

        default:
            // Unknown commands: acknowledge to keep firmware moving.
            s.resp[0] = 0x00000900u;
            s.sta_latch |= STA_CMDREND;
            break;
    }
}

// This function will emulation all device reads
uint64_t sdmmc1_read(void *opaque, hwaddr addr, unsigned size) {
    (void)opaque;
    uint32_t off = off_from_addr(addr);
    uint32_t val32 = 0;

    switch (off) {
        case REG_POWER:   val32 = s.power; break;
        case REG_CLKCR:   val32 = s.clkcr; break;
        case REG_ARG:     val32 = s.arg; break;

        // Must read back last CMD written (trace shows reads returning previous command values)
        case REG_CMD:     val32 = s.cmd; break;

        case REG_RESPCMD: val32 = s.respcmd; break;
        case REG_RESP1:   val32 = s.resp[0]; break;
        case REG_RESP2:   val32 = s.resp[1]; break;
        case REG_RESP3:   val32 = s.resp[2]; break;
        case REG_RESP4:   val32 = s.resp[3]; break;

        case REG_DTIMER:  val32 = s.dtimer; break;
        case REG_DLEN:    val32 = s.dlen; break;
        case REG_DCTRL:   val32 = s.dctrl; break;

        case REG_STA:     val32 = compute_sta(); break;

        case REG_FIFO: {
            // Only meaningful for read transfers
            if (!s.data_active || !s.data_is_read) {
                val32 = 0;
                break;
            }
            uint32_t remaining = (s.fifo_pos < s.fifo_len) ? (s.fifo_len - s.fifo_pos) : 0;
            if (remaining == 0) {
                // drain complete -> deactivate; dynamic RX bits drop, but DATAEND/DBCKEND stay until cleared
                s.data_active = false;
                val32 = 0;
                break;
            }

            uint8_t b[4] = {0,0,0,0};
            uint32_t n = (remaining >= 4) ? 4 : remaining;
            for (uint32_t i = 0; i < n; i++) b[i] = s.fifo[s.fifo_pos + i];
            s.fifo_pos += n;

            val32 = (uint32_t)b[0]
                  | ((uint32_t)b[1] << 8)
                  | ((uint32_t)b[2] << 16)
                  | ((uint32_t)b[3] << 24);

            if (s.fifo_pos >= s.fifo_len) {
                s.data_active = false;
            }
            break;
        }

        default:
            val32 = 0;
            break;
    }

    uint32_t shift = ((uint32_t)addr & 3u) * 8u;
    return (val32 >> shift) & mask_for_size(size);
}

// This function will emulate all device writes
void sdmmc1_write(void *opaque, hwaddr addr, uint64_t value, unsigned size) {
    (void)opaque;
    uint32_t off = off_from_addr(addr);

    switch (off) {
        case REG_POWER:
            s.power = apply_partial_write(s.power, value, addr, size);
            break;

        case REG_CLKCR:
            s.clkcr = apply_partial_write(s.clkcr, value, addr, size);
            break;

        case REG_ARG:
            s.arg = apply_partial_write(s.arg, value, addr, size);
            break;

        case REG_CMD: {
            uint32_t old = s.cmd;
            uint32_t nv  = apply_partial_write(s.cmd, value, addr, size);
            s.cmd = nv;

            uint32_t old_idx = old & 0x3Fu;
            uint32_t new_idx = nv  & 0x3Fu;

            bool cpsmen = (nv & CMD_CPSMEN) != 0;
            bool idx_changed = (new_idx != old_idx);

            if (cpsmen || idx_changed) {
                exec_cmd(nv);
            }
            break;
        }

        case REG_DTIMER:
            s.dtimer = apply_partial_write(s.dtimer, value, addr, size);
            break;

        case REG_DLEN:
            s.dlen = apply_partial_write(s.dlen, value, addr, size);
            break;

        case REG_DCTRL:
            s.dctrl = apply_partial_write(s.dctrl, value, addr, size);
            break;

        case REG_ICR: {
            // W1C against sta_latch, exactly like trace uses 0xC5
            uint32_t v = (uint32_t)value;
            s.sta_latch &= ~v;
            break;
        }

        case REG_FIFO: {
            // Host->Card writes for CMD24
            if (!s.data_active || s.data_is_read) break;

            uint32_t remaining = (s.fifo_pos < s.fifo_len) ? (s.fifo_len - s.fifo_pos) : 0;
            if (remaining == 0) {
                maybe_finish_write_and_commit();
                break;
            }

            uint32_t v32 = (uint32_t)value;
            uint32_t n = size;
            if (n > remaining) n = remaining;

            for (uint32_t i = 0; i < n; i++) {
                s.fifo[s.fifo_pos++] = (uint8_t)(v32 >> (8u * i));
            }

            maybe_finish_write_and_commit();
            break;
        }

        default:
            break;
    }
}

void sdmmc1_init(ConfigSection* model_info) {
    (void)model_info;
    memset(&s, 0, sizeof(s));
    s.host_fd = -1;
    s.card_size = 0;

    // Match where you actually created the image (from your shell commands)
    const char *img_path = "sdcard.img";

    s.host_fd = api_file_open(img_path, 1); // 1 = Read/Write
    if (s.host_fd >= 0) {
        s.card_size = api_file_get_size(s.host_fd);

        char msg[160];
        snprintf(msg, sizeof(msg), "SDMMC1: opened %s size=%llu bytes",
                 img_path, (unsigned long long)s.card_size);
        dev_debug(msg);
    } else {
        dev_debug("SDMMC1: sdcard.img open failed; reads return 0.");
    }
}
