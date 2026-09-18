/*
 * STM32F7x9 Ethernet MAC/MMC/DMA Device Model — Milestone C (DEBUG, polling-only)
 *
 * Goals:
 *  - NO IRQs (your firmware polls via ethernetif_input / HAL_ETH_ReadData)
 *  - Ghost PHY that reliably reports LINK UP (incl. reg31 "special status")
 *  - Polling timer drains TAP and delivers frames into RX descriptors
 *  - TX scans descriptors and sends frames to TAP
 *  - Verbose logging of:
 *      - MACCR / DMAOMR gating bits
 *      - PHY MII reads/writes (addr/reg/value)
 *      - RX/TX desc OWN + key words (optional dump)
 *
 * Env knobs:
 *   ETH_TAP=tap0
 *   ETH_LOG_LEVEL=0|1|2    (default 2)
 *   ETH_POLL_NS=5000000    (default 5ms)
 *   ETH_RX_POOL_ENABLE=0|1 (default 1; if firmware leaves DESC2=0)
 *   ETH_IGNORE_ENABLE=0|1  (default 0; if 1, ignore RE/TE and SR/ST gating)
 *   ETH_PROMISC=0|1        (default 1; if 0, drop multicast noise; keep broadcast)
 *
 * Notes:
 *  - Descriptor stride is 0x28 (40 bytes) in your traces, so we treat each desc as 10 u32 words.
 *  - RX: when delivering a frame, we clear OWN, set FS/LS, and set FL including CRC (+4)
 *  - TX: when sending, we clear OWN and set TI in DMASR.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <arpa/inet.h>

#include <device.h>
#include <boardrunner/vio.h>

#ifndef ARRAY_SIZE
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#endif

// ---- MMIO base addresses ----
#define ETH_MAC_BASE   ((hwaddr)0x40028000ull)
#define ETH_MMC_BASE   ((hwaddr)0x40028100ull)
#define ETH_DMA_BASE   ((hwaddr)0x40029000ull)

// ---- MAC offsets (subset) ----
#define MACCR_OFF      0x0000u
#define MACMIIAR_OFF   0x0010u
#define MACMIIDR_OFF   0x0014u
#define MACIMR_OFF     0x003Cu
#define MACA0HR_OFF    0x0040u
#define MACA0LR_OFF    0x0044u

// MACCR bits
#define MACCR_RE_BIT   (1u << 2)
#define MACCR_TE_BIT   (1u << 3)

// ---- MMC offsets (subset) ----
#define MMCRIMR_OFF    0x000Cu
#define MMCTIMR_OFF    0x0010u

// ---- DMA offsets ----
#define DMABMR_OFF     0x0000u
#define DMATPDR_OFF    0x0004u
#define DMARPDR_OFF    0x0008u
#define DMARDLAR_OFF   0x000Cu
#define DMATDLAR_OFF   0x0010u
#define DMASR_OFF      0x0014u
#define DMAOMR_OFF     0x0018u

// ---- DMASR bits (subset) ----
#define DMASR_TI               (1u << 0)
#define DMASR_RI               (1u << 6)
#define DMASR_RU               (1u << 7)
#define DMASR_AIS              (1u << 15)
#define DMASR_NIS              (1u << 16)
#define DMASR_W1C_MASK         (0x0001FFFFu)

// ---- DMAOMR bits (Synopsys) ----
#define DMAOMR_SR              (1u << 1)     // Start/Stop Receive
#define DMAOMR_ST              (1u << 13)    // Start/Stop Transmission

// ---- DMABMR ----
#define DMABMR_SWR             (1u << 0)

// ---- Descriptor ----
#define ETH_DESC_STRIDE_BYTES  0x28u
typedef struct EthDesc40 { uint32_t w[10]; } EthDesc40;

// DESC0 bits
#define DESC_OWN_BIT      (1u << 31)
#define RXDESC_LS_BIT     (1u << 8)
#define RXDESC_FS_BIT     (1u << 9)
#define RXDESC_FL_SHIFT   16
#define RXDESC_FL_MASK    (0x3FFFu << RXDESC_FL_SHIFT)

// DESC1 bits
#define RXDESC_RBS1_MASK  (0x1FFFu)
#define RXDESC_RER_BIT    (1u << 15)
#define TXDESC_TER_BIT    (1u << 21)
#define TXDESC_TBS1_MASK  (0x1FFFu)
#define TXDESC_TCH_BIT (1u << 20)   // in DESC0
#define TXDESC_FS_BIT (1u << 28)
#define TXDESC_LS_BIT (1u << 29)

// ---- Ethernet ----
#define ETH_MAX_FRAME          1600

#define RXDESC_RCH_BIT    (1u << 14)   // Receive Chained
#define TXDESC_TCH_BIT    (1u << 20)   // Transmit Chained
#define TXDESC_TBS2_MASK  (0x1FFFu << 16)

#define TXDESC_CIC_MASK   (3u << 22)
#define TXDESC_CIC_SHIFT  22
#define TXDESC_CIC_BYPASS 0u
#define TXDESC_CIC_IPHDR  1u
#define TXDESC_CIC_FULL   2u

// ---- Logging ----
static int g_log_level = 0; // 0=quiet, 1=milestone, 2=verbose
#define LOG1(...) do { if (g_log_level >= 1) { printf(__VA_ARGS__); fflush(stdout); } } while (0)
#define LOG2(...) do { if (g_log_level >= 2) { printf(__VA_ARGS__); fflush(stdout); } } while (0)

// ---- State ----
typedef struct EthernetState {
  // MAC
  uint32_t MACCR, MACMIIAR, MACMIIDR, MACIMR, MACA0HR, MACA0LR;
  uint16_t phy_regs[32];

  // MMC
  uint32_t MMCRIMR, MMCTIMR;

  // DMA
  uint32_t DMABMR, DMATPDR, DMARPDR, DMARDLAR, DMATDLAR, DMASR, DMAOMR;

  // ring pointers
  hwaddr tx_cur, rx_cur;
  bool have_tx_base, have_rx_base;

  // tap + poll
  int tap_fd;
  uint64_t poll_timer_id;
  uint32_t poll_ticks;

  // RX pool
  bool   rx_pool_enable;
  bool   rx_pool_inited;
  hwaddr rx_pool_base;
  hwaddr rx_pool_next;
  uint32_t rx_pool_bufsz;

  // debug modes
  bool ignore_enable;  // ignore RE/TE and SR/ST gating
  bool promisc;        // accept all (except optional multicast drop)

  // counters
  uint32_t seen_rx_frames;
  uint32_t sent_tx_frames;
  uint32_t tap_pkts;
  uint32_t tap_bytes;

  bool dumped_rings_once;
} EthernetState;

static EthernetState g_eth;

// ---- helpers ----
static inline bool looks_like_sram_addr(uint32_t v) {
  // Allow SRAM (0x20000000) AND Flash (0x08000000)
  if ((v >= 0x20000000u) && (v < 0x30000000u)) return true;
  if ((v >= 0x08000000u) && (v < 0x08200000u)) return true; // 2MB Flash window
  return false;
}
static inline bool looks_like_sram_wordptr(uint32_t v) {
  return looks_like_sram_addr(v) && ((v & 3u) == 0);
}

static inline hwaddr align_up(hwaddr v, hwaddr a) {
  return (a ? (hwaddr)((v + (a - 1)) & ~(a - 1)) : v);
}
static int mem_read(hwaddr addr, void *out, int len) {
  return qemu_plugin_read_memory((unsigned long long)addr, (uint8_t*)out, len);
}
static int mem_write(hwaddr addr, const void *in, int len) {
  return qemu_plugin_write_memory((unsigned long long)addr, (uint8_t*)in, len);
}

static uint32_t merge_subwrite_u32(uint32_t oldv, uint64_t value, unsigned size, unsigned byte_off)
{
  uint32_t mask;
  if (size == 1) mask = 0xFFu << (byte_off * 8);
  else if (size == 2) mask = 0xFFFFu << (byte_off * 8);
  else { mask = 0xFFFFFFFFu; byte_off = 0; }

  uint32_t v32 = (uint32_t)value;
  uint32_t shifted = (byte_off ? (v32 << (byte_off * 8)) : v32);
  return (oldv & ~mask) | (shifted & mask);
}
static uint64_t subread_u32(uint32_t v, unsigned size, unsigned byte_off)
{
  if (size == 1) return (v >> (byte_off * 8)) & 0xFFu;
  if (size == 2) return (v >> (byte_off * 8)) & 0xFFFFu;
  return (uint64_t)v;
}

static bool desc_read(hwaddr desc_addr, EthDesc40 *d)
{
  memset(d, 0, sizeof(*d));
  return mem_read(desc_addr, d, (int)sizeof(*d)) == 0;
}
static bool desc_write(hwaddr desc_addr, const EthDesc40 *d)
{
  return mem_write(desc_addr, d, (int)sizeof(*d)) == 0;
}

static inline bool mac_rx_enabled(void) { return (g_eth.MACCR & MACCR_RE_BIT) != 0; }
static inline bool mac_tx_enabled(void) { return (g_eth.MACCR & MACCR_TE_BIT) != 0; }
static inline bool dma_rx_started(void) { return (g_eth.DMAOMR & DMAOMR_SR) != 0; }
static inline bool dma_tx_started(void) { return (g_eth.DMAOMR & DMAOMR_ST) != 0; }

static uint32_t rx_bufcap_from_des1(uint32_t des1) { return des1 & RXDESC_RBS1_MASK; }
static uint32_t tx_len_from_des1(uint32_t des1)    { return des1 & TXDESC_TBS1_MASK; }

static hwaddr next_desc_addr_rx(const EthDesc40 *d, hwaddr cur)
{
  uint32_t des1 = d->w[1];
  uint32_t d3   = d->w[3];

  if ((des1 & RXDESC_RCH_BIT) && looks_like_sram_wordptr(d3)) return (hwaddr)d3;

  if (des1 & RXDESC_RER_BIT) {
    if (looks_like_sram_wordptr(g_eth.DMARDLAR)) return (hwaddr)g_eth.DMARDLAR;
  }
  return (hwaddr)(cur + (hwaddr)ETH_DESC_STRIDE_BYTES);
}

static hwaddr next_desc_addr_tx(const EthDesc40 *d, hwaddr cur)
{
  uint32_t des0 = d->w[0];
  uint32_t des1 = d->w[1];
  uint32_t d3   = d->w[3];

  if ((des0 & TXDESC_TCH_BIT) && looks_like_sram_wordptr(d3)) return (hwaddr)d3;

  if (des1 & TXDESC_TER_BIT) {
    if (looks_like_sram_wordptr(g_eth.DMATDLAR)) return (hwaddr)g_eth.DMATDLAR;
  }
  return (hwaddr)(cur + (hwaddr)ETH_DESC_STRIDE_BYTES);
}


static void dump_first_descs_once(void)
{
  if (g_eth.dumped_rings_once) return;
  if (!g_eth.have_rx_base || !g_eth.have_tx_base) return;
  if (!looks_like_sram_wordptr(g_eth.DMARDLAR) || !looks_like_sram_wordptr(g_eth.DMATDLAR)) return;

  g_eth.dumped_rings_once = true;

  LOG1("[eth] --- ring dump (first 4 RX/TX desc) ---\n");
  for (int i = 0; i < 4; i++) {
    hwaddr a = (hwaddr)g_eth.DMARDLAR + (hwaddr)(i * ETH_DESC_STRIDE_BYTES);
    EthDesc40 d;
    if (desc_read(a, &d)) {
      LOG1("[eth] RX[%d] @0x%08x: D0=%08x D1=%08x D2=%08x D3=%08x\n",
           i, (unsigned)a, d.w[0], d.w[1], d.w[2], d.w[3]);
    }
  }
  for (int i = 0; i < 4; i++) {
    hwaddr a = (hwaddr)g_eth.DMATDLAR + (hwaddr)(i * ETH_DESC_STRIDE_BYTES);
    EthDesc40 d;
    if (desc_read(a, &d)) {
      LOG1("[eth] TX[%d] @0x%08x: D0=%08x D1=%08x D2=%08x D3=%08x\n",
           i, (unsigned)a, d.w[0], d.w[1], d.w[2], d.w[3]);
    }
  }
}

static uint16_t csum16(const uint8_t *buf, uint32_t len)
{
  uint32_t sum = 0;
  while (len > 1) {
    sum += ((uint32_t)buf[0] << 8) | buf[1];
    buf += 2; len -= 2;
  }
  if (len) sum += ((uint32_t)buf[0] << 8);

  while (sum >> 16) sum = (sum & 0xFFFFu) + (sum >> 16);
  return (uint16_t)~sum;
}

static void tx_apply_hw_csum(uint32_t first_des0, uint8_t *eth, uint32_t eth_len)
{
  uint32_t cic = (first_des0 & TXDESC_CIC_MASK) >> TXDESC_CIC_SHIFT;
  if (cic == TXDESC_CIC_BYPASS) return;

  if (eth_len < 14) return;
  uint16_t etype = (uint16_t)((eth[12] << 8) | eth[13]);
  if (etype != 0x0800) return; // IPv4 only

  if (eth_len < 14 + 20) return;
  uint8_t *ip = eth + 14;
  uint8_t ver_ihl = ip[0];
  if ((ver_ihl >> 4) != 4) return;
  uint32_t ihl = (ver_ihl & 0x0F) * 4;
  if (ihl < 20 || eth_len < 14 + ihl) return;

  uint16_t ip_tot = (uint16_t)((ip[2] << 8) | ip[3]);
  if (ip_tot < ihl) return;
  if (eth_len < 14 + ip_tot) return;

  // Always do IPv4 header checksum if requested
  if (cic == TXDESC_CIC_IPHDR || cic == TXDESC_CIC_FULL) {
    ip[10] = 0; ip[11] = 0;
    uint16_t ipcs = csum16(ip, ihl);
    ip[10] = (uint8_t)(ipcs >> 8);
    ip[11] = (uint8_t)(ipcs & 0xFF);
  }

  if (cic != TXDESC_CIC_FULL) return;

  uint8_t proto = ip[9];
  uint8_t *l4 = ip + ihl;
  uint32_t l4_len = (uint32_t)ip_tot - ihl;

  if (l4_len < 4) return;

  // TCP
  if (proto == 6 && l4_len >= 20) {
    l4[16] = 0; l4[17] = 0;
    uint32_t sum = 0;
    // pseudo-header
    sum += ((uint32_t)ip[12] << 8) | ip[13];
    sum += ((uint32_t)ip[14] << 8) | ip[15];
    sum += ((uint32_t)ip[16] << 8) | ip[17];
    sum += ((uint32_t)ip[18] << 8) | ip[19];
    sum += proto;
    sum += l4_len;
    // payload
    const uint8_t *p = l4;
    uint32_t n = l4_len;
    while (n > 1) { sum += ((uint32_t)p[0] << 8) | p[1]; p += 2; n -= 2; }
    if (n) sum += ((uint32_t)p[0] << 8);
    while (sum >> 16) sum = (sum & 0xFFFFu) + (sum >> 16);
    uint16_t cs = (uint16_t)~sum;
    l4[16] = (uint8_t)(cs >> 8);
    l4[17] = (uint8_t)(cs & 0xFF);
    return;
  }

  // UDP
  if (proto == 17 && l4_len >= 8) {
    l4[6] = 0; l4[7] = 0;
    uint32_t sum = 0;
    sum += ((uint32_t)ip[12] << 8) | ip[13];
    sum += ((uint32_t)ip[14] << 8) | ip[15];
    sum += ((uint32_t)ip[16] << 8) | ip[17];
    sum += ((uint32_t)ip[18] << 8) | ip[19];
    sum += proto;
    sum += l4_len;

    const uint8_t *p = l4;
    uint32_t n = l4_len;
    while (n > 1) { sum += ((uint32_t)p[0] << 8) | p[1]; p += 2; n -= 2; }
    if (n) sum += ((uint32_t)p[0] << 8);
    while (sum >> 16) sum = (sum & 0xFFFFu) + (sum >> 16);
    uint16_t cs = (uint16_t)~sum;
    if (cs == 0) cs = 0xFFFF; // UDP checksum: 0 means “not used”
    l4[6] = (uint8_t)(cs >> 8);
    l4[7] = (uint8_t)(cs & 0xFF);
    return;
  }

  // ICMP
  if (proto == 1 && l4_len >= 4) {
    l4[2] = 0; l4[3] = 0;
    uint16_t cs = csum16(l4, l4_len);
    l4[2] = (uint8_t)(cs >> 8);
    l4[3] = (uint8_t)(cs & 0xFF);
    return;
  }
}

static inline uint16_t rd_be16(const uint8_t *p) {
    return (uint16_t)((p[0] << 8) | p[1]);
}
static inline void wr_be16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)(v & 0xff);
}

/* 16-bit one's complement sum */
static uint32_t csum_accum(const uint8_t *buf, size_t len) {
    uint32_t sum = 0;
    size_t i = 0;
    while (i + 1 < len) {
        sum += (uint16_t)((buf[i] << 8) | buf[i + 1]);
        i += 2;
    }
    if (i < len) {
        sum += (uint16_t)(buf[i] << 8);
    }
    return sum;
}
static uint16_t csum_finalize(uint32_t sum) {
    while (sum >> 16) sum = (sum & 0xFFFFu) + (sum >> 16);
    return (uint16_t)(~sum);
}

/* Compute IPv4 header checksum (writes into header) */
static void ipv4_fix_hdr_checksum(uint8_t *ip, size_t ip_len) {
    if (ip_len < 20) return;
    uint8_t ihl = (uint8_t)(ip[0] & 0x0F);
    size_t hdr_len = (size_t)ihl * 4;
    if (ihl < 5 || hdr_len > ip_len) return;

    wr_be16(&ip[10], 0); // checksum field = 0
    uint32_t sum = csum_accum(ip, hdr_len);
    wr_be16(&ip[10], csum_finalize(sum));
}

/* Compute TCP/UDP checksum (writes into L4 header) */
static void ipv4_fix_l4_checksum(uint8_t *ip, size_t ip_len) {
    if (ip_len < 20) return;
    uint8_t ihl = (uint8_t)(ip[0] & 0x0F);
    size_t ip_hdr_len = (size_t)ihl * 4;
    if (ihl < 5 || ip_hdr_len > ip_len) return;

    uint16_t total_len = rd_be16(&ip[2]);
    if (total_len < ip_hdr_len || total_len > ip_len) return;

    uint8_t proto = ip[9];
    uint8_t *l4 = ip + ip_hdr_len;
    size_t l4_len = (size_t)total_len - ip_hdr_len;

    int is_tcp = (proto == 6);
    int is_udp = (proto == 17);
    if (!is_tcp && !is_udp) return;
    if (l4_len < (is_tcp ? 20 : 8)) return;

    size_t cksum_off = is_tcp ? 16 : 6;
    wr_be16(&l4[cksum_off], 0);

    /* pseudo-header calculation */
    uint32_t sum = 0;
    sum += csum_accum(&ip[12], 8);       // Source & Dest IP
    sum += (uint32_t)proto;              // Protocol (added ONCE)
    sum += (uint32_t)(l4_len & 0xFFFFu); // TCP/UDP Length

    /* add TCP/UDP header+payload */
    sum += csum_accum(l4, l4_len);

    uint16_t csum = csum_finalize(sum);
    if (is_udp && csum == 0) csum = 0xFFFF;

    wr_be16(&l4[cksum_off], csum);
}

/* Call this on the *full Ethernet frame* you are about to write to TAP */
static void eth_fixup_tx_checksums(uint8_t *frame, size_t frame_len) {
    if (frame_len < 14) return;

    size_t l3_off = 14;
    uint16_t et = rd_be16(&frame[12]);

    /* VLAN tag */
    if (et == 0x8100) {
        if (frame_len < 18) return;
        et = rd_be16(&frame[16]);
        l3_off = 18;
    }

    if (et != 0x0800) return; /* IPv4 only */
    if (frame_len <= l3_off) return;

    uint8_t *ip = frame + l3_off;
    size_t ip_len = frame_len - l3_off;

    /* Only IPv4 version */
    if ((ip[0] >> 4) != 4) return;

    ipv4_fix_hdr_checksum(ip, ip_len);
    ipv4_fix_l4_checksum(ip, ip_len);
}


// ---- PHY / MII ----
// Many Cube BSP PHY drivers (LAN8742/DP83848/etc.) read reg 31 "special status".
// So we set BOTH BSR (reg1) and reg31 to link-up-ish values.
static void phy_init_defaults(void)
{
  memset(g_eth.phy_regs, 0, sizeof(g_eth.phy_regs));

  // Standard clause-22 regs
  g_eth.phy_regs[0]  = 0x3100; // BMCR: autoneg enable-ish
  g_eth.phy_regs[1]  = 0x786D; // BMSR: link up + autoneg complete + capabilities

  // IDs (keep whatever you want; most BSPs don't strictly gate on it)
  g_eth.phy_regs[2]  = 0x0007;
  g_eth.phy_regs[3]  = 0xC0F1;

  // Vendor/status regs commonly used by BSP link-state logic
  // REG16 often used as PHYSTS (DP83848-style): bit0=link up in many drivers.
  // REG25 sometimes used as another status/control reg; some BSPs check it too.
  g_eth.phy_regs[16] = 0x0017; // bit0=1 (link), plus some nonzero speed/duplex/autoneg bits
  g_eth.phy_regs[25] = 0x0001; // "link good" (nonzero + bit0)

  // Keep reg31 nonzero too (covers LAN8742-style drivers if they ever read it)
  g_eth.phy_regs[31] = 0x001D;
}

static void mii_start_transaction(void)
{
  // STM32 MACMIIAR: PA[15:11], MR[10:6], MW[1], MB[0]
  uint32_t miiar = g_eth.MACMIIAR;
  uint8_t pa  = (uint8_t)((miiar >> 11) & 0x1Fu);
  uint8_t reg = (uint8_t)((miiar >> 6)  & 0x1Fu);
  bool is_write = ((miiar >> 1) & 1u) != 0;

  uint16_t oldv = 0;
  uint16_t newv = 0;

  if ((pa == 0 || pa == 1) && reg < 32) {
    oldv = g_eth.phy_regs[reg];
    if (is_write) {
      newv = (uint16_t)(g_eth.MACMIIDR & 0xFFFFu);
      g_eth.phy_regs[reg] = newv;
    } else {
      g_eth.MACMIIDR = (uint32_t)g_eth.phy_regs[reg];
      newv = (uint16_t)(g_eth.MACMIIDR & 0xFFFFu);
    }
  } else {
    if (!is_write) g_eth.MACMIIDR = 0;
  }

  if (g_log_level >= 2) {
    LOG2("[eth] MII %s PA=%u REG=%u 0x%04x%s\n",
         is_write ? "W" : "R",
         (unsigned)pa, (unsigned)reg,
         (unsigned)(is_write ? newv : newv),
         is_write ? "" : "");
    if (is_write) {
      LOG2("[eth]     PHY[%u]: %04x -> %04x\n", (unsigned)reg, (unsigned)oldv, (unsigned)newv);
    }
  }

  // Clear busy
  g_eth.MACMIIAR &= ~1u;
}

// ---- reset ----
static void dma_soft_reset(void)
{
  g_eth.DMAOMR = 0;
  g_eth.DMASR  = 0;
  g_eth.DMARPDR = 0;
  g_eth.DMATPDR = 0;
  g_eth.DMARDLAR = 0;
  g_eth.DMATDLAR = 0;

  g_eth.have_rx_base = g_eth.have_tx_base = false;
  g_eth.rx_cur = g_eth.tx_cur = 0;

  g_eth.rx_pool_inited = false;
  g_eth.rx_pool_base = g_eth.rx_pool_next = 0;
  g_eth.rx_pool_bufsz = 0;

  g_eth.dumped_rings_once = false;
  LOG1("[eth] DMA soft reset\n");
}

// If firmware leaves DESC2=0, allocate RX buffers in guest SRAM (simple pool).
static void rx_fixup_ring_buffers(void)
{
  if (!g_eth.rx_pool_enable) return;
  if (!g_eth.have_rx_base || !looks_like_sram_wordptr(g_eth.DMARDLAR)) return;

  EthDesc40 first;
  if (!desc_read((hwaddr)g_eth.DMARDLAR, &first)) return;

  uint32_t cap = rx_bufcap_from_des1(first.w[1]);
  if (cap == 0 || cap > 2048) cap = 1528;
  g_eth.rx_pool_bufsz = cap;

  if (!g_eth.rx_pool_inited) {
    hwaddr hi = (g_eth.DMATDLAR > g_eth.DMARDLAR) ? (hwaddr)g_eth.DMATDLAR : (hwaddr)g_eth.DMARDLAR;
    g_eth.rx_pool_base = align_up(hi + 0x0400u, 32u);
    g_eth.rx_pool_next = g_eth.rx_pool_base;
    g_eth.rx_pool_inited = true;
    LOG1("[eth] RX pool init base=0x%08x bufsz=%u\n", (unsigned)g_eth.rx_pool_base, (unsigned)g_eth.rx_pool_bufsz);
  }

  hwaddr start = (hwaddr)g_eth.DMARDLAR;
  hwaddr cur   = start;

  for (int i = 0; i < 64; i++) {
    EthDesc40 d;
    if (!desc_read(cur, &d)) break;

    if (d.w[2] == 0) {
      hwaddr buf = align_up(g_eth.rx_pool_next, 32u);
      g_eth.rx_pool_next = buf + align_up((hwaddr)g_eth.rx_pool_bufsz, 32u);
      d.w[2] = (uint32_t)buf;
      (void)desc_write(cur, &d);
      LOG1("[eth] RX desc @0x%08x DESC2=0 -> set 0x%08x (cap=%u)\n",
           (unsigned)cur, (unsigned)d.w[2], (unsigned)g_eth.rx_pool_bufsz);
    }

    hwaddr nxt = next_desc_addr_rx(&d, cur);
    if (nxt == 0 || nxt == cur || nxt == start) break;
    cur = nxt;
  }
}

// ---- tiny ethernet header parse for logging/filtering ----
static void log_tap_pkt_brief(const uint8_t *eth, int eth_len)
{
  if (eth_len < 14) return;
  uint16_t type = (uint16_t)((eth[12] << 8) | eth[13]);
  const uint8_t *dst = eth + 0;
  LOG2("[eth] tap rx n=%d dst=%02x:%02x:%02x:%02x:%02x:%02x type=%04x\n",
       eth_len,
       dst[0],dst[1],dst[2],dst[3],dst[4],dst[5],
       (unsigned)type);
}

static void maca0_get(uint8_t mac[6])
{
  uint32_t lr = g_eth.MACA0LR;
  uint32_t hr = g_eth.MACA0HR;
  mac[0] = (uint8_t)(lr & 0xFF);
  mac[1] = (uint8_t)((lr >> 8) & 0xFF);
  mac[2] = (uint8_t)((lr >> 16) & 0xFF);
  mac[3] = (uint8_t)((lr >> 24) & 0xFF);
  mac[4] = (uint8_t)(hr & 0xFF);
  mac[5] = (uint8_t)((hr >> 8) & 0xFF);
}

static bool should_accept_frame(const uint8_t *eth, int eth_len)
{
  if (eth_len < 14) return false;

  // before promisc check:
 if ((eth[0] & 1) && !(eth[0]==0xff && eth[1]==0xff && eth[2]==0xff && eth[3]==0xff && eth[4]==0xff && eth[5]==0xff))
 return false;

  if (g_eth.promisc) return true;

  const uint8_t *dst = eth;
  static const uint8_t bcast[6] = {0xff,0xff,0xff,0xff,0xff,0xff};

  // Always accept broadcast (ARP will be broadcast)
  if (memcmp(dst, bcast, 6) == 0) return true;

  // Accept only unicast to our MAC
  uint8_t mymac[6];
  maca0_get(mymac);
  // If MAC isn't configured yet, be conservative: only broadcast allowed
  bool mac_set = false;
  for (int i = 0; i < 6; i++) if (mymac[i] != 0) { mac_set = true; break; }
  if (!mac_set) return false;

  return memcmp(dst, mymac, 6) == 0;
}

static void tx_scan_and_send(void)
{
  if (!g_eth.have_tx_base) return;
  if (g_eth.tap_fd < 0) return;

  if (!g_eth.ignore_enable) {
    if (!mac_tx_enabled()) return;
    if (!dma_tx_started()) return;
  }

  if (!looks_like_sram_wordptr((uint32_t)g_eth.tx_cur)) g_eth.tx_cur = (hwaddr)g_eth.DMATDLAR;
  if (!looks_like_sram_wordptr((uint32_t)g_eth.tx_cur)) return;

  uint32_t first_des0 = 0;

  uint8_t frame[ETH_MAX_FRAME];
  uint32_t frame_len = 0;
  bool in_frame = false;

  for (int i = 0; i < 64; i++) {
    EthDesc40 d;
    if (!desc_read(g_eth.tx_cur, &d)) break;

    uint32_t des0 = d.w[0];
    uint32_t des1 = d.w[1];

    // Hardware stops at first CPU-owned descriptor
    if ((des0 & DESC_OWN_BIT) == 0) break;

    bool fs = (des0 & TXDESC_FS_BIT) != 0;
    bool ls = (des0 & TXDESC_LS_BIT) != 0;

    uint32_t len1 = des1 & TXDESC_TBS1_MASK;
    uint32_t len2 = (des1 >> 16) & 0x1FFFu;

    bool tch = (des0 & TXDESC_TCH_BIT) != 0;   // NOTE: TCH is in DESC0

    hwaddr buf1 = (hwaddr)(uint64_t)d.w[2];
    hwaddr buf2_or_next = (hwaddr)(uint64_t)d.w[3];

    if (fs) { frame_len = 0; in_frame = true; first_des0 = des0; }
    if (!in_frame) { frame_len = 0; in_frame = true; } // be forgiving

    // Copy buffer 1
    if (len1 && len1 <= ETH_MAX_FRAME && looks_like_sram_addr((uint32_t)buf1)) {
      uint32_t room = ETH_MAX_FRAME - frame_len;
      uint32_t take = (len1 <= room) ? len1 : room;
      if (take && mem_read(buf1, frame + frame_len, (int)take) == 0) {
        frame_len += take;
      }
    }

    // Copy buffer 2 only if NOT chained descriptor mode (otherwise DESC3 is next desc pointer)
    if (!tch && len2 && frame_len < ETH_MAX_FRAME && looks_like_sram_addr((uint32_t)buf2_or_next)) {
      uint32_t room = ETH_MAX_FRAME - frame_len;
      uint32_t take = (len2 <= room) ? len2 : room;
      if (take && mem_read(buf2_or_next, frame + frame_len, (int)take) == 0) {
        frame_len += take;
      }
    }

    // Clear OWN so HAL can progress even if we later drop the frame
    d.w[0] = des0 & ~DESC_OWN_BIT;
    (void)desc_write(g_eth.tx_cur, &d);

    hwaddr nxt = next_desc_addr_tx(&d, g_eth.tx_cur);
    if (!looks_like_sram_wordptr((uint32_t)nxt) || nxt == g_eth.tx_cur) {
      // can't advance safely; if this was LS, we can still try to send
      nxt = 0;
    }

    if (ls) {
    //   if (frame_len > 0) {
    //     tx_apply_hw_csum(first_des0, frame, frame_len);
    //     uint32_t send_len = frame_len;
    //     if (send_len < 60) {
    //     memset(frame + send_len, 0, 60 - send_len);
    //     send_len = 60;
    //     }
    //     api_tap_send(g_eth.tap_fd, frame, (int)send_len);
    //     g_eth.sent_tx_frames++;
    //     LOG1("[eth] TX sent frame_len=%u send_len=%u last_desc=0x%08x\n",
    //         (unsigned)frame_len, (unsigned)send_len, (unsigned)g_eth.tx_cur);
    //   }
    if (frame_len > 0) {
        // Option A: Keep relying on descriptors (risky if firmware is lazy)
        tx_apply_hw_csum(first_des0, frame, frame_len);

        // Option B (RECOMMENDED): Force valid checksums for the host OS
        // This ensures the packet is valid regardless of what the firmware requested.
        eth_fixup_tx_checksums(frame, frame_len);

        uint32_t send_len = frame_len;
        if (send_len < 60) {
            memset(frame + send_len, 0, 60 - send_len);
            send_len = 60;
        }

        api_tap_send(g_eth.tap_fd, frame, (int)send_len);
        g_eth.sent_tx_frames++;
        LOG1("[eth] TX sent frame_len=%u send_len=%u last_desc=0x%08x\n",
            (unsigned)frame_len, (unsigned)send_len, (unsigned)g_eth.tx_cur);
      }

    g_eth.DMASR |= (DMASR_TI | DMASR_NIS);
      in_frame = false;
      frame_len = 0;
    }

    if (!nxt) break;
    g_eth.tx_cur = nxt;
  }
}


static bool deliver_one_rx(const uint8_t *pkt, uint32_t pkt_len)
{
  if (!g_eth.have_rx_base) return false;

  if (!g_eth.ignore_enable) {
    if (!mac_rx_enabled()) return false;
    if (!dma_rx_started()) return false;
  }

  rx_fixup_ring_buffers();

  if (!looks_like_sram_wordptr((uint32_t)g_eth.rx_cur))
    g_eth.rx_cur = (hwaddr)g_eth.DMARDLAR;
  if (!looks_like_sram_wordptr((uint32_t)g_eth.rx_cur))
    return false;

  EthDesc40 d;
  if (!desc_read(g_eth.rx_cur, &d)) return false;

  uint32_t des0 = d.w[0];

  // Real DMA: if CPU owns it (OWN=0), STOP and set RU. Do NOT skip forward.
  if ((des0 & DESC_OWN_BIT) == 0) {
    g_eth.DMASR |= (DMASR_RU | DMASR_AIS);   // RU is “abnormal”
    return false;
  }

  hwaddr buf_addr = (hwaddr)(uint64_t)d.w[2];
  if (!looks_like_sram_addr((uint32_t)buf_addr)) {
    g_eth.DMASR |= (DMASR_RU | DMASR_AIS);
    return false;
  }

  uint32_t cap = rx_bufcap_from_des1(d.w[1]);
  if (cap == 0 || cap > ETH_MAX_FRAME) cap = ETH_MAX_FRAME;

  uint32_t copy_len = (pkt_len <= cap) ? pkt_len : cap;

  if (mem_write(buf_addr, pkt, (int)copy_len) != 0) {
    g_eth.DMASR |= (DMASR_RU | DMASR_AIS);
    return false;
  }

  // HAL expects FL includes CRC (+4); HAL subtracts 4.
  uint32_t fl_with_crc = copy_len + 4u;
  if (fl_with_crc > 0x3FFFu) fl_with_crc = 0x3FFFu;

  uint32_t new_des0 = des0 & ~DESC_OWN_BIT;              // hand to CPU
  new_des0 |= (RXDESC_FS_BIT | RXDESC_LS_BIT);
  new_des0 &= ~RXDESC_FL_MASK;
  new_des0 |= ((fl_with_crc & 0x3FFFu) << RXDESC_FL_SHIFT);

  d.w[0] = new_des0;
  (void)desc_write(g_eth.rx_cur, &d);

  // Clear RU if we just made progress
  g_eth.DMASR &= ~DMASR_RU;
  g_eth.DMASR |= (DMASR_RI | DMASR_NIS);

  g_eth.seen_rx_frames++;
  LOG1("[eth] RX delivered len=%u (FL=%u) desc=0x%08x buf=0x%08x D0=%08x\n",
       (unsigned)copy_len, (unsigned)fl_with_crc, (unsigned)g_eth.rx_cur,
       (unsigned)buf_addr, (unsigned)new_des0);

  hwaddr nxt = next_desc_addr_rx(&d, g_eth.rx_cur);
  if (looks_like_sram_wordptr((uint32_t)nxt) && nxt != g_eth.rx_cur) g_eth.rx_cur = nxt;
  return true;
}

#define ETH_HDR_LEN 14
#define IP_HDR_LEN  20
#define TCP_HDR_LEN 20

/* ----------------- Checksums ----------------- */

// static uint16_t csum16(const void *data, size_t len) {
//     uint32_t sum = 0;
//     const uint16_t *p = data;

//     while (len > 1) {
//         sum += *p++;
//         len -= 2;
//     }
//     if (len)
//         sum += *(uint8_t *)p;

//     while (sum >> 16)
//         sum = (sum & 0xffff) + (sum >> 16);

//     return ~sum;
// }

// static uint16_t tcp_checksum(
//     uint32_t src_ip,
//     uint32_t dst_ip,
//     const uint8_t *tcp,
//     size_t tcp_len
// ) {
//     struct {
//         uint32_t src;
//         uint32_t dst;
//         uint8_t zero;
//         uint8_t proto;
//         uint16_t len;
//     } pseudo;

//     pseudo.src  = src_ip;
//     pseudo.dst  = dst_ip;
//     pseudo.zero = 0;
//     pseudo.proto = 6; /* TCP */
//     pseudo.len  = htons(tcp_len);

//     uint32_t sum = 0;
//     sum += csum16(&pseudo, sizeof(pseudo));
//     sum += csum16(tcp, tcp_len);

//     while (sum >> 16)
//         sum = (sum & 0xffff) + (sum >> 16);

//     return ~sum;
// }

/* ----------------- Frame Builder ----------------- */

size_t build_eth_ipv4_tcp(
    uint8_t *out,
    size_t out_len,
    const uint8_t *payload,
    size_t payload_len
) {
    size_t total_len =
        ETH_HDR_LEN + IP_HDR_LEN + TCP_HDR_LEN + payload_len;

    if (out_len < total_len)
        return 0;

    uint8_t *p = out;

    /* ---------- Ethernet ---------- */
    uint8_t dst_mac[6] = {0x02,0x00,0x00,0x00,0x00,0x02};
    uint8_t src_mac[6] = {0x02,0x00,0x00,0x00,0x00,0x01};

    memcpy(p, dst_mac, 6);
    memcpy(p + 6, src_mac, 6);
    *(uint16_t *)(p + 12) = htons(0x0800); /* IPv4 */
    p += ETH_HDR_LEN;

    /* ---------- IPv4 ---------- */
    memset(p, 0, IP_HDR_LEN);
    p[0] = 0x45; /* v4, ihl=5 */
    p[1] = 0x00;
    *(uint16_t *)(p + 2) = htons(IP_HDR_LEN + TCP_HDR_LEN + payload_len);
    *(uint16_t *)(p + 4) = htons(0x1337);
    *(uint16_t *)(p + 6) = htons(0x4000); /* DF */
    p[8] = 64; /* TTL */
    p[9] = 6;  /* TCP */

    uint32_t src_ip = htonl(0x0a000001); /* 10.0.0.1 */
    uint32_t dst_ip = htonl(0xc0a80802); /* 10.0.0.2 */

    memcpy(p + 12, &src_ip, 4);
    memcpy(p + 16, &dst_ip, 4);

    // *(uint16_t *)(p + 10) = csum16(p, IP_HDR_LEN);
    // p += IP_HDR_LEN;

    /* ---------- TCP ---------- */
    memset(p, 0, TCP_HDR_LEN);
    *(uint16_t *)(p + 0) = htons(12345); /* src port */
    *(uint16_t *)(p + 2) = htons(80);    /* dst port */
    *(uint32_t *)(p + 4) = htonl(1);      /* seq */
    *(uint32_t *)(p + 8) = htonl(1);      /* ack */

    p[12] = (5 << 4);  /* data offset */
    p[13] = 0x18;      /* PSH | ACK */
    *(uint16_t *)(p + 14) = htons(4096); /* window */

    memcpy(p + TCP_HDR_LEN, payload, payload_len);

    // *(uint16_t *)(p + 16) =
    //     tcp_checksum(src_ip, dst_ip, p,
    //                  TCP_HDR_LEN + payload_len);

    eth_fixup_tx_checksums(p, total_len);

    return total_len;
}

static void eth_periodic_poll(void *opaque)
{
  (void)opaque;
  g_eth.poll_ticks++;

  // One-time dump when bases appear
  dump_first_descs_once();

  // TX then RX
  tx_scan_and_send();

  if (g_eth.tap_fd < 0) return;
  if (!g_eth.have_rx_base) return;

  uint8_t buf[ETH_MAX_FRAME];

  for (int i = 0; i < 32; i++) {
    //int n = build_eth_ipv4_tcp(buf, ETH_MAX_FRAME, "GET /leds.cgi?led=2&led=4 HTTP/1.0\r\n\r\n", strlen("GET /leds.cgi?led=2&led=4 HTTP/1.0\r\n\r\n") + 1);
    int n = api_tap_recv_nonblock(g_eth.tap_fd, buf, (int)sizeof(buf));
    if (n <= 0) break;

    g_eth.tap_pkts++;
    g_eth.tap_bytes += (uint32_t)n;

    if (g_log_level >= 2) log_tap_pkt_brief(buf, n);

    if (!should_accept_frame(buf, n)) continue;

    if (!deliver_one_rx(buf, (uint32_t)n)) break;
  }

  // Heartbeat
  if ((g_eth.poll_ticks % 200u) == 0u) { // ~1s if 5ms poll
    LOG1("[eth] alive: tap_pkts=%u tap_bytes=%u rx=%u tx=%u MACCR=%08x DMAOMR=%08x DMASR=%08x rx_cur=%08x tx_cur=%08x\n",
         g_eth.tap_pkts, g_eth.tap_bytes, g_eth.seen_rx_frames, g_eth.sent_tx_frames,
         g_eth.MACCR, g_eth.DMAOMR, g_eth.DMASR,
         (unsigned)g_eth.rx_cur, (unsigned)g_eth.tx_cur);
  }
}

// ---- MMIO dispatch ----
static bool is_in_range(hwaddr addr, hwaddr base, uint32_t span) { return (addr >= base) && (addr < (base + span)); }

static bool handle_mac_read(hwaddr addr, unsigned size, uint64_t *out)
{
  hwaddr off = addr - ETH_MAC_BASE;
  unsigned byte_off = (unsigned)(addr & 3u);
  switch ((uint32_t)off) {
    case MACCR_OFF:    *out = subread_u32(g_eth.MACCR, size, byte_off); return true;
    case MACMIIAR_OFF: *out = subread_u32(g_eth.MACMIIAR, size, byte_off); return true;
    case MACMIIDR_OFF: *out = subread_u32(g_eth.MACMIIDR, size, byte_off); return true;
    case MACIMR_OFF:   *out = subread_u32(g_eth.MACIMR, size, byte_off); return true;
    case MACA0HR_OFF:  *out = subread_u32(g_eth.MACA0HR, size, byte_off); return true;
    case MACA0LR_OFF:  *out = subread_u32(g_eth.MACA0LR, size, byte_off); return true;
    default: return false;
  }
}
static bool handle_mmc_read(hwaddr addr, unsigned size, uint64_t *out)
{
  hwaddr off = addr - ETH_MMC_BASE;
  unsigned byte_off = (unsigned)(addr & 3u);
  switch ((uint32_t)off) {
    case MMCRIMR_OFF: *out = subread_u32(g_eth.MMCRIMR, size, byte_off); return true;
    case MMCTIMR_OFF: *out = subread_u32(g_eth.MMCTIMR, size, byte_off); return true;
    default: return false;
  }
}
static bool handle_dma_read(hwaddr addr, unsigned size, uint64_t *out)
{
  hwaddr off = addr - ETH_DMA_BASE;
  unsigned byte_off = (unsigned)(addr & 3u);
  switch ((uint32_t)off) {
    case DMABMR_OFF:   *out = subread_u32(g_eth.DMABMR, size, byte_off); return true;
    case DMATPDR_OFF:  *out = subread_u32(g_eth.DMATPDR, size, byte_off); return true;
    case DMARPDR_OFF:  *out = subread_u32(g_eth.DMARPDR, size, byte_off); return true;
    case DMARDLAR_OFF: *out = subread_u32(g_eth.DMARDLAR, size, byte_off); return true;
    case DMATDLAR_OFF: *out = subread_u32(g_eth.DMATDLAR, size, byte_off); return true;
    case DMASR_OFF:    *out = subread_u32(g_eth.DMASR, size, byte_off); return true;
    case DMAOMR_OFF:   *out = subread_u32(g_eth.DMAOMR, size, byte_off); return true;
    default: return false;
  }
}

static bool handle_mac_write(hwaddr addr, uint64_t value, unsigned size)
{
  hwaddr off = addr - ETH_MAC_BASE;
  unsigned byte_off = (unsigned)(addr & 3u);

  switch ((uint32_t)off) {
    case MACCR_OFF: {
      uint32_t old = g_eth.MACCR;
      g_eth.MACCR = merge_subwrite_u32(g_eth.MACCR, value, size, byte_off);
      LOG1("[eth] MACCR <= 0x%08x (RE=%u TE=%u)\n",
           g_eth.MACCR, (g_eth.MACCR & MACCR_RE_BIT) ? 1u : 0u, (g_eth.MACCR & MACCR_TE_BIT) ? 1u : 0u);
      if (g_log_level >= 2 && old != g_eth.MACCR) {
        // nothing more for now
      }
      return true;
    }
    case MACIMR_OFF:
      g_eth.MACIMR = merge_subwrite_u32(g_eth.MACIMR, value, size, byte_off);
      return true;

    case MACMIIDR_OFF:
      g_eth.MACMIIDR = merge_subwrite_u32(g_eth.MACMIIDR, value, size, byte_off);
      return true;

    case MACMIIAR_OFF: {
      uint32_t neu = merge_subwrite_u32(g_eth.MACMIIAR, value, size, byte_off);
      g_eth.MACMIIAR = neu;
      // If busy set, complete transaction immediately and clear busy.
      if (neu & 1u) mii_start_transaction();
      return true;
    }

    case MACA0HR_OFF:
      g_eth.MACA0HR = merge_subwrite_u32(g_eth.MACA0HR, value, size, byte_off);
      LOG1("[eth] MACA0HR <= 0x%08x\n", g_eth.MACA0HR);
      return true;

    case MACA0LR_OFF:
      g_eth.MACA0LR = merge_subwrite_u32(g_eth.MACA0LR, value, size, byte_off);
      LOG1("[eth] MACA0LR <= 0x%08x\n", g_eth.MACA0LR);
      return true;

    default:
      return false;
  }
}

static bool handle_mmc_write(hwaddr addr, uint64_t value, unsigned size)
{
  hwaddr off = addr - ETH_MMC_BASE;
  unsigned byte_off = (unsigned)(addr & 3u);
  switch ((uint32_t)off) {
    case MMCRIMR_OFF: g_eth.MMCRIMR = merge_subwrite_u32(g_eth.MMCRIMR, value, size, byte_off); return true;
    case MMCTIMR_OFF: g_eth.MMCTIMR = merge_subwrite_u32(g_eth.MMCTIMR, value, size, byte_off); return true;
    default: return false;
  }
}

static bool handle_dma_write(hwaddr addr, uint64_t value, unsigned size)
{
  hwaddr off = addr - ETH_DMA_BASE;
  unsigned byte_off = (unsigned)(addr & 3u);

  switch ((uint32_t)off) {
    case DMABMR_OFF: {
      uint32_t neu = merge_subwrite_u32(g_eth.DMABMR, value, size, byte_off);
      if (neu & DMABMR_SWR) {
        dma_soft_reset();
        neu &= ~DMABMR_SWR; // auto-clear
      }
      g_eth.DMABMR = neu;
      LOG1("[eth] DMABMR <= 0x%08x\n", g_eth.DMABMR);
      return true;
    }

    case DMAOMR_OFF:
      g_eth.DMAOMR = merge_subwrite_u32(g_eth.DMAOMR, value, size, byte_off);
      LOG1("[eth] DMAOMR <= 0x%08x (SR=%u ST=%u)\n",
           g_eth.DMAOMR, (g_eth.DMAOMR & DMAOMR_SR) ? 1u : 0u, (g_eth.DMAOMR & DMAOMR_ST) ? 1u : 0u);
      return true;

    case DMATDLAR_OFF:
    g_eth.DMATDLAR = merge_subwrite_u32(g_eth.DMATDLAR, value, size, byte_off);
    if (looks_like_sram_wordptr(g_eth.DMATDLAR)) {
        g_eth.have_tx_base = true;

        // Only initialize tx_cur if it isn't valid yet
        if (!looks_like_sram_wordptr((uint32_t)g_eth.tx_cur)) {
        g_eth.tx_cur = (hwaddr)g_eth.DMATDLAR;
        }

        LOG1("[eth] DMATDLAR=0x%08x tx_cur=0x%08x\n",
            (unsigned)g_eth.DMATDLAR, (unsigned)g_eth.tx_cur);
    }
    return true;

    case DMARDLAR_OFF:
      g_eth.DMARDLAR = merge_subwrite_u32(g_eth.DMARDLAR, value, size, byte_off);
      if (looks_like_sram_wordptr(g_eth.DMARDLAR)) {
        g_eth.have_rx_base = true;
        g_eth.rx_cur = (hwaddr)g_eth.DMARDLAR;
        LOG1("[eth] DMARDLAR=0x%08x rx_cur=0x%08x\n", (unsigned)g_eth.DMARDLAR, (unsigned)g_eth.rx_cur);
        rx_fixup_ring_buffers();
      }
      return true;

    case DMATPDR_OFF: {
    g_eth.DMATPDR = merge_subwrite_u32(g_eth.DMATPDR, value, size, byte_off);

    // DMATPDR is a "poll demand" doorbell. Ignore the written value.
    // DO NOT: g_eth.tx_cur = (hwaddr)g_eth.DMATPDR;

    tx_scan_and_send();
    return true;
    }


    case DMARPDR_OFF:
      g_eth.DMARPDR = merge_subwrite_u32(g_eth.DMARPDR, value, size, byte_off);
      // Poll demand; some stacks poke this to recover from RU.
      g_eth.DMASR &= ~DMASR_RU;
      rx_fixup_ring_buffers();
      return true;

    case DMASR_OFF: {
      // W1C bits
      uint32_t w = (uint32_t)(value & 0xFFFFFFFFu);
      g_eth.DMASR &= ~(w & DMASR_W1C_MASK);
      return true;
    }

    default:
      return false;
  }
}

// ---- Public API expected by your loader ----
uint64_t ethernet_read(void *opaque, hwaddr addr, unsigned size)
{
  (void)opaque;
  uint64_t out = 0;

  if (is_in_range(addr, ETH_DMA_BASE, 0x1000)) { if (handle_dma_read(addr, size, &out)) return out; return 0; }
  if (is_in_range(addr, ETH_MMC_BASE, 0x100))  { if (handle_mmc_read(addr, size, &out)) return out; return 0; }
  if (is_in_range(addr, ETH_MAC_BASE, 0x2000)) { if (handle_mac_read(addr, size, &out)) return out; return 0; }
  return 0;
}

void ethernet_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
  (void)opaque;

  if (is_in_range(addr, ETH_DMA_BASE, 0x1000)) { (void)handle_dma_write(addr, value, size); return; }
  if (is_in_range(addr, ETH_MMC_BASE, 0x100))  { (void)handle_mmc_write(addr, value, size); return; }
  if (is_in_range(addr, ETH_MAC_BASE, 0x2000)) { (void)handle_mac_write(addr, value, size); return; }
}

void ethernet_init(ConfigSection* model_info)
{
  (void)model_info;
  memset(&g_eth, 0, sizeof(g_eth));

  // baseline-ish defaults
  g_eth.DMABMR = 0x00020100u;
  g_eth.MACCR  = 0x00008000u;

  phy_init_defaults();

  const char *lvl = getenv("ETH_LOG_LEVEL");
  if (lvl && lvl[0]) g_log_level = atoi(lvl);

  const char *poll = getenv("ETH_POLL_NS");
  uint64_t poll_ns = 5000000ull;
  if (poll && poll[0]) poll_ns = (uint64_t)strtoull(poll, NULL, 10);

  const char *pool_en = getenv("ETH_RX_POOL_ENABLE");
  g_eth.rx_pool_enable = true;
  if (pool_en && pool_en[0] == '0') g_eth.rx_pool_enable = false;

  const char *ign = getenv("ETH_IGNORE_ENABLE");
  g_eth.ignore_enable = (ign && ign[0] == '1');

  const char *pr = getenv("ETH_PROMISC");
  g_eth.promisc = true;
  if (pr && pr[0] == '0') g_eth.promisc = false;

  const char *ifname = getenv("ETH_TAP");
  if (!ifname || !ifname[0]) ifname = "tap0";

  g_eth.tap_fd = api_tap_init(ifname);
  if (g_eth.tap_fd >= 0) LOG1("[eth] TAP initialized: %s (fd=%d)\n", ifname, g_eth.tap_fd);
  else LOG1("[eth] TAP init failed (%s)\n", ifname);

  g_eth.poll_timer_id = qemu_plugin_timer_new_period_ns(eth_periodic_poll, NULL, poll_ns);

  LOG1("[eth] init complete (polling-only, no IRQ). log=%d rx_pool=%d promisc=%d ignore_enable=%d poll_ns=%llu\n",
       g_log_level, g_eth.rx_pool_enable ? 1 : 0, g_eth.promisc ? 1 : 0, g_eth.ignore_enable ? 1 : 0,
       (unsigned long long)poll_ns);
}
