/**
 * @brief Shared Betaflight STM32F405 time, UART and motor boundary callbacks.
 *
 * Every callback here replaces or observes one Betaflight function at symbol
 * level. Hardware sensor bus callbacks are in hardware.c; the original virtual
 * sensor callbacks remain available for the separate FASTDYNF405 prototype.
 *
 * ABI contract (checked against the ELF DWARF by courbet/profiles/betaflight):
 *
 * @requirements.betaflight
 * serialPort_s:        size 60,  identifier @56 (int8)
 * gyroDev_s:           size 348, scale @168 (float), gyroADCRaw @208 (int16[3]), dataReady @293
 * accDev_s:            size 68,  acc_1G @12 (u16), ADCRaw @14 (int16[3]), dataReady @23
 * motorDevice_s:       size 12,  count @4 (u8)
 * gpsSolutionData_s:   size 72,  llh @0 {lat,lon,altCm i32}, dop @12 {pdop,hdop,vdop u16},
 *                      acc @20 {hAcc,vAcc,sAcc,headAcc u32}, velned @36 {velN,velE,velD i16},
 *                      speed3d @42, groundSpeed @44, groundCourse @46 (u16), numSat @48,
 *                      numSatInView @49 (u8), time @52 (u32)
 * @end_requirements
 *
 * @file virtuals/physics/flight_controllers/betaflight/betaflight.c
 */

#include <errno.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/types.h>

#include "virtuals.h"
#include "phy.h"
#include "betaflight.h"
#include "hardware.h"

/* ------------------------------------------------------------------------- */
/* Constants                                                                  */
/* ------------------------------------------------------------------------- */

/* The FastDyn cortexm machine wires SYSCLK at 168 MHz (qemu/hw/arm/cortexm.c),
 * which is also the STM32F405 core clock Betaflight assumes. */
#define BF_CPU_HZ                 168000000u
#define BF_US_TICKS               (BF_CPU_HZ / 1000000u)

/* Betaflight raw sensor scales (see docs/BetaflightRehosting.md §2.5). */
#define BF_GYRO_SCALE_2000DPS     (2000.0f / 32768.0f)   /* accgyro.h GYRO_SCALE_2000DPS */
#define BF_GYRO_LSB_PER_DPS       (1.0f / BF_GYRO_SCALE_2000DPS)
#define BF_ACC_1G                 256.0f                 /* acceleration_init.c default acc_1G */
#define BF_GRAVITY                9.80665f
#define BF_RAD_TO_DEG             57.29577951308232f

/* Struct offsets (DWARF, pinned by the profile). */
#define OFF_SERIALPORT_IDENTIFIER 56
#define OFF_GYRO_SCALE            168
#define OFF_GYRO_ADCRAW           208
#define OFF_ACC_ADCRAW            14
#define OFF_MOTORDEVICE_COUNT     4
#define OFF_GPS_LLH               0
#define OFF_GPS_DOP               12
#define OFF_GPS_ACC               20
#define OFF_GPS_VELNED            36
#define OFF_GPS_SPEED3D           42
#define OFF_GPS_GROUNDSPEED       44
#define OFF_GPS_GROUNDCOURSE      46
#define OFF_GPS_NUMSAT            48
#define OFF_GPS_NUMSATINVIEW      49
#define OFF_GPS_TIME              52

#define BF_GPS_PERIOD_NS          100000000ll   /* 10 Hz virtual GPS updates */

/* Betaflight serialPortIdentifier_e values in this build (verified with gdb on
 * the FASTDYNF405 ELF; override with FASTDYN_BF_UART_IDS="<mavlink>,<msp>"). */
#define BF_DEFAULT_ID_MAVLINK_UART 51   /* SERIAL_PORT_USART1 */
#define BF_DEFAULT_ID_MSP_UART     52   /* SERIAL_PORT_USART2 */

/* ------------------------------------------------------------------------- */
/* Small helpers                                                              */
/* ------------------------------------------------------------------------- */

static void bf_return(uint32_t value)
{
    qemu_set_register(value, ARM_V7M_R0);
    qemu_set_register(qemu_get_register(ARM_V7M_LR), ARM_V7M_PC);
}

static void bf_return_void(void)
{
    qemu_set_register(qemu_get_register(ARM_V7M_LR), ARM_V7M_PC);
}

static bool bf_debug(void)
{
    static int cached = -1;
    if (cached < 0) {
        const char *v = getenv("FASTDYN_BF_DEBUG");
        cached = (v && *v && strcmp(v, "0") != 0) ? 1 : 0;
    }
    return cached == 1;
}

static int bf_read_u8(uint32_t addr, uint8_t *out)
{
    return qemu_plugin_read_memory(addr, out, 1);
}

static int bf_read_u32(uint32_t addr, uint32_t *out)
{
    return qemu_plugin_read_memory(addr, (uint8_t *)out, 4);
}

static int bf_write_u16(uint32_t addr, uint16_t v)
{
    return qemu_plugin_write_memory(addr, (uint8_t *)&v, 2);
}

static int bf_write_i16(uint32_t addr, int16_t v)
{
    return qemu_plugin_write_memory(addr, (uint8_t *)&v, 2);
}

static int bf_write_u32(uint32_t addr, uint32_t v)
{
    return qemu_plugin_write_memory(addr, (uint8_t *)&v, 4);
}

static int bf_write_i32(uint32_t addr, int32_t v)
{
    return qemu_plugin_write_memory(addr, (uint8_t *)&v, 4);
}

static int bf_write_f32(uint32_t addr, float v)
{
    return qemu_plugin_write_memory(addr, (uint8_t *)&v, 4);
}

static int16_t bf_clamp_i16(float v)
{
    if (v > 32767.0f) return 32767;
    if (v < -32768.0f) return -32768;
    return (int16_t)lrintf(v);
}

/* Parse "*0xaaaa,0xbbbb,..." (the generator resolves symbols to hex). */
static int bf_parse_addresses(const char *udata, uint32_t *out, int max)
{
    int n = 0;
    if (!udata) return 0;
    const char *p = udata;
    if (*p == '*') p++;
    while (*p && n < max) {
        char *end = NULL;
        unsigned long v = strtoul(p, &end, 0);
        if (end == p) break;
        out[n++] = (uint32_t)v;
        p = end;
        while (*p == ',' || *p == ' ') p++;
    }
    return n;
}

static int64_t bf_now_ns(void)
{
    int64_t ns = qemu_plugin_get_virtual_timer();
    return ns < 0 ? 0 : ns;
}

static void bf_debug_heartbeat(int64_t now);   /* defined with the UART bridge state */

/* ------------------------------------------------------------------------- */
/* Time                                                                       */
/* ------------------------------------------------------------------------- */

/** micros()/microsISR() -> uint32 microseconds of virtual time. Rule: `micros bf_micros32 *` */
static void bf_micros32(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    int64_t now = bf_now_ns();
    if (bf_debug()) bf_debug_heartbeat(now);
    bf_return((uint32_t)((uint64_t)now / 1000ull));
}

/** millis() -> uint32 milliseconds. Rule: `millis bf_millis *` */
static void bf_millis(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_return((uint32_t)((uint64_t)bf_now_ns() / 1000000ull));
}

/** getCycleCounter() -> DWT CYCCNT substitute: virtual time x 168 MHz, mod 2^32.
 *  Rule: `getCycleCounter bf_cycle_counter *` */
static void bf_cycle_counter(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint64_t cycles = ((uint64_t)bf_now_ns() * (uint64_t)BF_US_TICKS) / 1000ull;
    bf_return((uint32_t)cycles);
}

/** cycleCounterInit(): replace it and seed the statics the scheduler derives
 *  cycle periods from, so clockMicrosToCycles() matches bf_cycle_counter.
 *  Rule: `cycleCounterInit bf_cycle_counter_init *usTicks,usTicksInv,cpuClockFrequency` */
static void bf_cycle_counter_init(unsigned int cpu_index, void *udata)
{
    (void)cpu_index;
    uint32_t addr[3];
    int supplied = bf_parse_addresses((const char *)udata, addr, 3);
    if (supplied < 1) {
        fprintf(stderr, "betaflight: bf_cycle_counter_init needs *usTicks,usTicksInv,cpuClockFrequency\n");
        bf_return_void();
        return;
    }
    bf_write_u32(addr[0], BF_US_TICKS);
    if (supplied >= 2 && addr[1]) bf_write_f32(addr[1], 1.0e6f / (float)BF_CPU_HZ);
    if (supplied >= 3 && addr[2]) bf_write_u32(addr[2], BF_CPU_HZ);
    fprintf(stderr, "betaflight: cycle counter seeded (usTicks=%u, %u Hz)\n", BF_US_TICKS, BF_CPU_HZ);
    bf_return_void();
}

/* ------------------------------------------------------------------------- */
/* Settings storage (flash sector 1 is ROM in QEMU; write through debug path)  */
/* ------------------------------------------------------------------------- */

/** configWriteWord(uintptr_t address, uint32_t *buffer) -> CONFIG_RESULT_SUCCESS(0).
 *  Rule: `configWriteWord bf_config_write_word *` */
static void bf_config_write_word(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t address = qemu_get_register(ARM_V7M_R0);
    uint32_t buffer = qemu_get_register(ARM_V7M_R1);
    uint32_t word = 0;
    static uint32_t words_written;
    if (bf_read_u32(buffer, &word) != 0 || qemu_plugin_write_memory(address, (uint8_t *)&word, 4) != 0) {
        fprintf(stderr, "betaflight: config write failed at 0x%08x\n", address);
        bf_return((uint32_t)-1); /* CONFIG_RESULT_FAILURE */
        return;
    }
    if (words_written++ == 0 || bf_debug()) {
        fprintf(stderr, "betaflight: config word #%u -> 0x%08x = 0x%08x\n", words_written, address, word);
    }
    bf_return(0);
}

/* ------------------------------------------------------------------------- */
/* Persistent objects (RTC backup registers; unmodelled MMIO would read 0)     */
/* ------------------------------------------------------------------------- */

#define BF_PERSISTENT_SLOTS 20
static uint32_t bf_persistent[BF_PERSISTENT_SLOTS];

/** persistentObjectRead(id) -> value. Rule: `persistentObjectRead bf_persistent_read *` */
static void bf_persistent_read(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t id = qemu_get_register(ARM_V7M_R0);
    bf_return(id < BF_PERSISTENT_SLOTS ? bf_persistent[id] : 0u);
}

/** persistentObjectWrite(id, value). Rule: `persistentObjectWrite bf_persistent_write *` */
static void bf_persistent_write(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t id = qemu_get_register(ARM_V7M_R0);
    uint32_t value = qemu_get_register(ARM_V7M_R1);
    if (id < BF_PERSISTENT_SLOTS) {
        if (bf_debug() && bf_persistent[id] != value) {
            fprintf(stderr, "betaflight: persistent[%u] = 0x%08x\n", id, value);
        }
        bf_persistent[id] = value;
    }
    bf_return_void();
}

/* ------------------------------------------------------------------------- */
/* UART <-> UDP bridge (raw bytes; the guest owns MAVLink/MSP framing)         */
/* ------------------------------------------------------------------------- */

typedef struct {
    const char *name;
    const char *port_env;
    int default_port;
    int identifier;            /* Betaflight serialPortIdentifier_e */
    int sock;
    struct sockaddr_in peer;
    bool peer_known;
    unsigned char rx[65536];
    size_t rx_head, rx_tail;
    unsigned char tx[2048];
    size_t tx_len;
    int64_t last_flush_ns;
    uint32_t instance;         /* guest serialPort_t* once seen in any hook */
    uint64_t bytes_in, bytes_out;
} bf_link_t;

#define OFF_SERIALPORT_RXCALLBACK     44
#define OFF_SERIALPORT_RXCALLBACKDATA 48
#define BF_TX_FLUSH_INTERVAL_NS       2000000ll   /* 2 ms: ~one datagram per telemetry burst */

static bf_link_t bf_links[3] = {
    { .name = "mavlink", .port_env = "FASTDYN_MAVLINK_FIRMWARE_PORT", .default_port = 15551,
      .identifier = BF_DEFAULT_ID_MAVLINK_UART, .sock = -1 },
    { .name = "msp", .port_env = "FASTDYN_BF_MSP_PORT", .default_port = 15560,
      .identifier = BF_DEFAULT_ID_MSP_UART, .sock = -1 },
    { .name = "gps", .identifier = 53, .sock = -1 },
};

/* Debug: one line every 5 s of virtual time with bridge byte counters. */
static void bf_debug_heartbeat(int64_t now)
{
    static int64_t last_mark = -1;
    int64_t mark = now / 5000000000ll;
    if (mark == last_mark) return;
    last_mark = mark;
    gps_data_t g; memset(&g, 0, sizeof(g));
    int have_gps = phy_get_navsat_reading(&g);
    fprintf(stderr, "betaflight: virtual t=%.1f s  mavlink in/out=%llu/%llu  msp in/out=%llu/%llu  "
            "fmu t=%.2f alt=%.2f vd=%.2f\n",
            now / 1e9, (unsigned long long)bf_links[0].bytes_in, (unsigned long long)bf_links[0].bytes_out,
            (unsigned long long)bf_links[1].bytes_in, (unsigned long long)bf_links[1].bytes_out,
            have_gps ? g.sec + g.nsec / 1e9 : -1.0, have_gps ? g.alt : 0.0, have_gps ? g.vel_d : 0.0);
}

static void bf_links_configure_once(void)
{
    static bool done;
    if (done) return;
    done = true;
    const char *ids = getenv("FASTDYN_BF_UART_IDS");
    if (ids && *ids) {
        int a = 0, b = 0, c = 53;
        if (sscanf(ids, "%d,%d,%d", &a, &b, &c) >= 2) {
            bf_links[0].identifier = a;
            bf_links[1].identifier = b;
            bf_links[2].identifier = c;
        } else {
            fprintf(stderr, "betaflight: ignoring malformed FASTDYN_BF_UART_IDS='%s'\n", ids);
        }
    }
}

static void bf_link_open(bf_link_t *l)
{
    if (l->sock >= 0) return;
    const char *value = getenv(l->port_env);
    char *end = NULL;
    long port = value && *value ? strtol(value, &end, 10) : l->default_port;
    if (port < 1 || port > 65535 || (end && *end)) {
        fprintf(stderr, "betaflight: invalid %s\n", l->port_env);
        exit(EXIT_FAILURE);
    }
    l->sock = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    struct sockaddr_in local = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
        .sin_port = htons((uint16_t)port),
    };
    if (l->sock < 0 || bind(l->sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
        perror("betaflight: UART bridge bind");
        exit(EXIT_FAILURE);
    }
    fprintf(stderr, "betaflight: UART bridge %s <-> UDP 127.0.0.1:%ld\n", l->name, port);
}

static void bf_link_flush(bf_link_t *l)
{
    if (!l->tx_len) return;
    if (l == &bf_links[2]) { l->tx_len = 0; return; }
    bf_link_open(l);
    if (l->peer_known) {
        ssize_t sent = sendto(l->sock, l->tx, l->tx_len, 0,
                              (struct sockaddr *)&l->peer, sizeof(l->peer));
        if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != ECONNREFUSED) {
            perror("betaflight: UART bridge sendto");
        }
    }
    /* Before a peer sends us anything, TX has nowhere to go: drop it like a
     * disconnected cable (PX4 bridge does the same). */
    l->bytes_out += l->tx_len;
    l->tx_len = 0;
}

static void bf_link_poll_rx(bf_link_t *l)
{
    if (l == &bf_links[2]) {
        if (l->rx_tail >= l->rx_head) {
            l->rx_head = betaflight_hardware_gps_bytes(l->rx, sizeof(l->rx));
            l->rx_tail = 0;
        }
        return;
    }
    bf_link_open(l);
    if (l->rx_tail < l->rx_head) return;      /* still draining the last datagram */
    struct sockaddr_in peer;
    socklen_t size = sizeof(peer);
    ssize_t count = recvfrom(l->sock, l->rx, sizeof(l->rx), 0, (struct sockaddr *)&peer, &size);
    if (count < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return;
        perror("betaflight: UART bridge recvfrom");
        exit(EXIT_FAILURE);
    }
    if (!count) return;
    if (!l->peer_known || peer.sin_port != l->peer.sin_port) {
        fprintf(stderr, "betaflight: %s peer 127.0.0.1:%u (first datagram %zd bytes, t=%.3f)\n",
                l->name, ntohs(peer.sin_port), count, bf_now_ns() / 1e9);
    }
    l->peer = peer;
    l->peer_known = true;
    l->rx_head = (size_t)count;
    l->rx_tail = 0;
    l->bytes_in += (uint64_t)count;
}

/* Debug: report the first few hits of each hook per port identifier. */
static void bf_hook_dbg(const char *hook, uint32_t instance, int id, bf_link_t *l)
{
    if (!bf_debug()) return;
    static struct { const char *hook; int id; unsigned hits; } seen[32];
    static size_t used;
    size_t i;
    for (i = 0; i < used; i++) if (seen[i].hook == hook && seen[i].id == id) break;
    if (i == used) {
        if (used == sizeof(seen) / sizeof(seen[0])) return;
        seen[used].hook = hook; seen[used].id = id; seen[used].hits = 0; used++;
    }
    if (++seen[i].hits <= 3) {
        fprintf(stderr, "betaflight: hook %s port@0x%08x id=%d -> %s (t=%.3f)\n",
                hook, instance, id, l ? l->name : "(unmapped)", bf_now_ns() / 1e9);
    }
}

static bf_link_t *bf_link_for_port_dbg(uint32_t instance, const char *hook)
{
    bf_links_configure_once();
    int8_t id = 0;
    if (bf_read_u8(instance + OFF_SERIALPORT_IDENTIFIER, (uint8_t *)&id) != 0) {
        bf_hook_dbg(hook, instance, -128, NULL);
        return NULL;
    }
    bf_link_t *l = NULL;
    for (size_t i = 0; i < sizeof(bf_links) / sizeof(bf_links[0]); i++) {
        if (bf_links[i].identifier == id) { l = &bf_links[i]; break; }
    }
    if (l && !l->instance) l->instance = instance;
    bf_hook_dbg(hook, instance, id, l);
    return l;
}
#define bf_link_for_port(instance) bf_link_for_port_dbg((instance), __func__)

/** uartWrite(serialPort_t *instance, uint8_t ch). Rule: `uartWrite bf_uart_write *` */
static void bf_uart_write(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    if (l) {
        l->tx[l->tx_len++] = (unsigned char)qemu_get_register(ARM_V7M_R1);
        if (l->tx_len >= sizeof(l->tx) - 1) bf_link_flush(l);
    }
    bf_return_void();
}

/** uartWriteBuf(serialPort_t *instance, const void *data, int count). Rule: `uartWriteBuf bf_uart_write_buf *` */
static void bf_uart_write_buf(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    uint32_t ptr = qemu_get_register(ARM_V7M_R1);
    int32_t count = (int32_t)qemu_get_register(ARM_V7M_R2);
    if (l && count > 0 && count < 65536) {
        while (count > 0) {
            size_t room = sizeof(l->tx) - l->tx_len;
            size_t n = (size_t)count < room ? (size_t)count : room;
            if (qemu_plugin_read_memory(ptr, l->tx + l->tx_len, n) != 0) break;
            l->tx_len += n; ptr += n; count -= (int32_t)n;
            if (l->tx_len >= sizeof(l->tx) - 1) bf_link_flush(l);
        }
    }
    bf_return_void();
}

/** uartEndWrite(serialPort_t *instance): flush point for MSP frames. Rule: `uartEndWrite bf_uart_end_write *` */
static void bf_uart_end_write(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    if (l) bf_link_flush(l);
    bf_return_void();
}

/** uartTotalRxBytesWaiting(const serialPort_t *instance) -> uint32. Rule: `uartTotalRxBytesWaiting bf_uart_rx_waiting *` */
static void bf_uart_rx_waiting(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    if (!l) { bf_return(0); return; }
    bf_link_flush(l);          /* the firmware polls RX often: cheap TX latency bound */
    bf_link_poll_rx(l);
    bf_return((uint32_t)(l->rx_head - l->rx_tail));
}

/** uartRead(serialPort_t *instance) -> uint8. Rule: `uartRead bf_uart_read *` */
static void bf_uart_read(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    if (!l) { bf_return(0); return; }
    if (l->rx_tail >= l->rx_head) bf_link_poll_rx(l);
    uint32_t ch = 0;
    if (l->rx_tail < l->rx_head) ch = l->rx[l->rx_tail++];
    bf_return(ch);
}

/** uartTotalTxBytesFree(const serialPort_t *instance) -> uint32. Rule: `uartTotalTxBytesFree bf_uart_tx_free *` */
static void bf_uart_tx_free(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    if (l) bf_link_flush(l);
    bf_return(l ? 4096u : 0u);
}

/** Serial pump, hooked at `run+2` (the `bl scheduler` inside run()'s endless
 *  loop, where LR and R0-R3 are dead). Flushes pending TX at a bounded rate and
 *  delivers inbound bytes the way the UART RX ISR would: for a port that
 *  registered an rxCallback(uint16_t data, void *rxCallbackData), redirect the
 *  guest into that callback with LR pointing back at this PC, so the callback
 *  returns here and the next byte is injected before scheduler() runs again.
 *  Ports without a callback (MSP) are drained by the firmware through
 *  uartTotalRxBytesWaiting()/uartRead() instead.
 *  A CFI ('*') hook always has to move PC (QEMU exits the TB before the hooked
 *  instruction runs), so when nothing is injected this emulates the 4-byte
 *  `bl scheduler` itself. The hook address is passed in explicitly: inside a
 *  callback, reading the PC register mid-translation-block yields the TB start,
 *  not the hooked instruction. Rule: `run+2 bf_serial_pump *scheduler,run+2` */
static void bf_serial_pump(unsigned int cpu_index, void *udata)
{
    (void)cpu_index;
    bf_links_configure_once();
    int64_t now = bf_now_ns();
    uint32_t args[3] = {0, 0, 0};
    if (bf_parse_addresses((const char *)udata, args, 3) < 2 || !args[0] || !args[1]) {
        fprintf(stderr, "betaflight: bf_serial_pump needs *scheduler,<hook pc> as argument\n");
        exit(EXIT_FAILURE);
    }
    uint32_t scheduler_addr = args[0] & ~1u;
    uint32_t here = args[1] & ~1u;
    static bool calibration_started;
    if (args[2] && !calibration_started) {
        calibration_started = true;
        fprintf(stderr, "bf-hw: starting real accelerometer calibration on stationary plant\n");
        qemu_set_register(here | 1u, ARM_V7M_LR);
        qemu_set_register(args[2] | 1u, ARM_V7M_PC);
        return;
    }
    for (size_t i = 0; i < sizeof(bf_links) / sizeof(bf_links[0]); i++) {
        bf_link_t *l = &bf_links[i];
        if (!l->instance) continue;
        if (l->tx_len && (l->tx_len >= 512 || now - l->last_flush_ns >= BF_TX_FLUSH_INTERVAL_NS)) {
            bf_link_flush(l);
            l->last_flush_ns = now;
        }
        if (l->rx_tail >= l->rx_head) bf_link_poll_rx(l);
        if (l->rx_tail >= l->rx_head) continue;
        uint32_t cb = 0, cb_data = 0;
        if (bf_read_u32(l->instance + OFF_SERIALPORT_RXCALLBACK, &cb) != 0 || !cb) continue;
        bf_read_u32(l->instance + OFF_SERIALPORT_RXCALLBACKDATA, &cb_data);
        uint32_t byte = l->rx[l->rx_tail++];
        qemu_set_register(byte, ARM_V7M_R0);
        qemu_set_register(cb_data, ARM_V7M_R1);
        qemu_set_register(here | 1u, ARM_V7M_LR);
        qemu_set_register(cb | 1u, ARM_V7M_PC);
        return;   /* one byte per pass; the callback returns to `here` */
    }
    /* Nothing to inject: perform `bl scheduler` (4-byte Thumb-2 encoding). */
    qemu_set_register((here + 4u) | 1u, ARM_V7M_LR);
    qemu_set_register(scheduler_addr | 1u, ARM_V7M_PC);
}

/** isUartTransmitBufferEmpty(const serialPort_t *instance) -> bool. Rule: `isUartTransmitBufferEmpty bf_uart_tx_empty *` */
static void bf_uart_tx_empty(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_link_t *l = bf_link_for_port(qemu_get_register(ARM_V7M_R0));
    if (l) bf_link_flush(l);
    bf_return(1);
}

/* ------------------------------------------------------------------------- */
/* Sensors (Betaflight virtual drivers fed from the plant)                    */
/* ------------------------------------------------------------------------- */

/* Axis/sign mapping from the FMU body frame (FRD, Copter.mo) into Betaflight's
 * raw sensor frame. The default is the pure FRD->FLU rotation (x, -y, -z) for
 * both sensors; override while validating against MAVLink ATTITUDE with
 * FASTDYN_BF_ACC_SIGNS="1,-1,-1" / FASTDYN_BF_GYRO_SIGNS="1,-1,-1". */
static void bf_signs(const char *env, float out[3], const float def[3])
{
    out[0] = def[0]; out[1] = def[1]; out[2] = def[2];
    const char *v = getenv(env);
    if (v && *v) {
        float a, b, c;
        if (sscanf(v, "%f,%f,%f", &a, &b, &c) == 3) { out[0] = a; out[1] = b; out[2] = c; }
        else fprintf(stderr, "betaflight: ignoring malformed %s='%s'\n", env, v);
    }
}

static bool bf_latest_imu(imu_t *out)
{
    imu_batch_t batch;
    if (!phy_get_imu_batch(&batch)) return false;
    *out = batch.imu[16];   /* fmu_get_imu_batch fills all 17 with the current sample */
    return true;
}

/** virtualGyroInit(gyroDev_t *gyro): observe-only; fix the raw scale so int16
 *  raw units are 1/16.384 dps like a real 2000 dps part. Rule: `virtualGyroInit bf_gyro_init` */
static void bf_gyro_init(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t gyro = qemu_get_register(ARM_V7M_R0);
    bf_write_f32(gyro + OFF_GYRO_SCALE, BF_GYRO_SCALE_2000DPS);
    fprintf(stderr, "betaflight: virtual gyro @0x%08x scale set to 2000 dps\n", gyro);
}

/** virtualGyroRead(gyroDev_t *gyro) -> bool. Rule: `virtualGyroRead bf_gyro_read *` */
static void bf_gyro_read(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    static float signs[3]; static bool have_signs;
    if (!have_signs) { const float d[3] = {1.f, -1.f, -1.f}; bf_signs("FASTDYN_BF_GYRO_SIGNS", signs, d); have_signs = true; }
    uint32_t gyro = qemu_get_register(ARM_V7M_R0);
    imu_t imu;
    if (!bf_latest_imu(&imu)) { bf_return(0); return; }
    const float dps[3] = { imu.gyro.x * BF_RAD_TO_DEG, imu.gyro.y * BF_RAD_TO_DEG, imu.gyro.z * BF_RAD_TO_DEG };
    int16_t raw[3];
    for (int i = 0; i < 3; i++) {
        raw[i] = bf_clamp_i16(signs[i] * dps[i] * BF_GYRO_LSB_PER_DPS);
        bf_write_i16(gyro + OFF_GYRO_ADCRAW + 2 * i, raw[i]);
    }
    if (bf_debug()) {
        static unsigned calls, printed_moving;
        bool moving = fabsf(dps[0]) > 5.f || fabsf(dps[1]) > 5.f || fabsf(dps[2]) > 5.f;
        if ((calls++ % 5000) == 0 || (moving && printed_moving++ < 12)) {
            float zero[3] = {0}, adc[3] = {0}, scale = 0;
            qemu_plugin_read_memory(gyro + 172, (uint8_t *)zero, sizeof(zero));
            qemu_plugin_read_memory(gyro + 184, (uint8_t *)adc, sizeof(adc));
            qemu_plugin_read_memory(gyro + OFF_GYRO_SCALE, (uint8_t *)&scale, sizeof(scale));
            fprintf(stderr, "betaflight: gyro t=%.3f fmu dps=%.2f/%.2f/%.2f raw=%d/%d/%d zero=%.1f/%.1f/%.1f adc=%.2f/%.2f/%.2f scale=%.4f acc=%.2f/%.2f/%.2f\n",
                    bf_now_ns() / 1e9, dps[0], dps[1], dps[2], raw[0], raw[1], raw[2], zero[0], zero[1], zero[2],
                    adc[0], adc[1], adc[2], scale, imu.accel_body.x, imu.accel_body.y, imu.accel_body.z);
        }
    }
    bf_return(1);
}

/** virtualAccRead(accDev_t *acc) -> bool. Rule: `virtualAccRead bf_acc_read *` */
static void bf_acc_read(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    static float signs[3]; static bool have_signs;
    if (!have_signs) { const float d[3] = {1.f, -1.f, -1.f}; bf_signs("FASTDYN_BF_ACC_SIGNS", signs, d); have_signs = true; }
    uint32_t acc = qemu_get_register(ARM_V7M_R0);
    imu_t imu;
    if (!bf_latest_imu(&imu)) { bf_return(0); return; }
    const float ms2[3] = { imu.accel_body.x, imu.accel_body.y, imu.accel_body.z };
    for (int i = 0; i < 3; i++) {
        bf_write_i16(acc + OFF_ACC_ADCRAW + 2 * i, bf_clamp_i16(signs[i] * ms2[i] / BF_GRAVITY * BF_ACC_1G));
    }
    bf_return(1);
}

/** virtualBaroCalculate(int32_t *pressure, int32_t *temperature). Rule: `virtualBaroCalculate bf_baro_calculate *` */
static void bf_baro_calculate(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t p_ptr = qemu_get_register(ARM_V7M_R0);
    uint32_t t_ptr = qemu_get_register(ARM_V7M_R1);
    barometer_t baro = { .pressure_pa = 101325.0, .temperature_c = 25.0 };
    if (!phy_get_barometer_reading(&baro)) {
        baro.pressure_pa = 101325.0; baro.temperature_c = 25.0;
    }
    if (p_ptr) bf_write_i32(p_ptr, (int32_t)lrint(baro.pressure_pa));
    if (t_ptr) bf_write_i32(t_ptr, (int32_t)lrint(baro.temperature_c * 100.0));
    bf_return_void();
}

/** getVirtualGPSUpdateCount() -> uint32 that advances every 100 ms of virtual time.
 *  Rule: `getVirtualGPSUpdateCount bf_gps_update_count *` */
static void bf_gps_update_count(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    bf_return((uint32_t)(bf_now_ns() / BF_GPS_PERIOD_NS));
}

/** getVirtualGPS(gpsSolutionData_t *sol). Rule: `getVirtualGPS bf_gps_fill *` */
static void bf_gps_fill(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t sol = qemu_get_register(ARM_V7M_R0);
    gps_data_t g;
    memset(&g, 0, sizeof(g));
    if (!phy_get_navsat_reading(&g)) { bf_return_void(); return; }
    double vn = g.vel_n, ve = g.vel_e, vd = g.vel_d;
    double ground = sqrt(vn * vn + ve * ve);
    double speed3d = sqrt(ground * ground + vd * vd);
    double course = atan2(ve, vn) * 180.0 / M_PI;
    if (course < 0) course += 360.0;
    bf_write_i32(sol + OFF_GPS_LLH + 0, (int32_t)llround(g.lat * 1e7));
    bf_write_i32(sol + OFF_GPS_LLH + 4, (int32_t)llround(g.lon * 1e7));
    bf_write_i32(sol + OFF_GPS_LLH + 8, (int32_t)llround(g.alt * 100.0));
    bf_write_u16(sol + OFF_GPS_DOP + 0, 100); /* pdop 1.00 */
    bf_write_u16(sol + OFF_GPS_DOP + 2, 100);
    bf_write_u16(sol + OFF_GPS_DOP + 4, 100);
    bf_write_u32(sol + OFF_GPS_ACC + 0, 500);  /* hAcc mm */
    bf_write_u32(sol + OFF_GPS_ACC + 4, 500);
    bf_write_u32(sol + OFF_GPS_ACC + 8, 400);
    bf_write_u32(sol + OFF_GPS_ACC + 12, 1000);
    bf_write_i16(sol + OFF_GPS_VELNED + 0, bf_clamp_i16((float)(vn * 100.0)));
    bf_write_i16(sol + OFF_GPS_VELNED + 2, bf_clamp_i16((float)(ve * 100.0)));
    bf_write_i16(sol + OFF_GPS_VELNED + 4, bf_clamp_i16((float)(vd * 100.0)));
    bf_write_u16(sol + OFF_GPS_SPEED3D, (uint16_t)lrint(speed3d * 100.0));
    bf_write_u16(sol + OFF_GPS_GROUNDSPEED, (uint16_t)lrint(ground * 100.0));
    bf_write_u16(sol + OFF_GPS_GROUNDCOURSE, (uint16_t)lrint(course * 10.0));
    uint8_t sats = 12;
    qemu_plugin_write_memory(sol + OFF_GPS_NUMSAT, &sats, 1);
    qemu_plugin_write_memory(sol + OFF_GPS_NUMSATINVIEW, &sats, 1);
    bf_write_u32(sol + OFF_GPS_TIME, (uint32_t)(bf_now_ns() / 1000000ll));
    bf_return_void();
}

/* ------------------------------------------------------------------------- */
/* Motors                                                                     */
/* ------------------------------------------------------------------------- */

/** motorWriteAll(float *values): observe-only. Values are microseconds with
 *  MOTOR_PROTOCOL_PWM. Betaflight QUADX order (RR, FR, RL, FL) is remapped to
 *  the Copter.mo/ArduPilot order (FR, RL, FL, RR).
 *  Rule: `motorWriteAll bf_motor_write_all motorDevice` */
static void bf_motor_write_all(unsigned int cpu_index, void *udata)
{
    (void)cpu_index;
    static const int bf_to_fmu[4] = { 3, 0, 1, 2 };
    static uint16_t last_pwm[8];
    uint32_t values = qemu_get_register(ARM_V7M_R0);
    uint32_t motor_args[2] = {0, OFF_MOTORDEVICE_COUNT};
    uint8_t count = 4;
    if (bf_parse_addresses((const char *)udata, motor_args, 2) >= 1 && motor_args[0]) {
        uint8_t c = 0;
        if (bf_read_u8(motor_args[0] + motor_args[1], &c) == 0 && c > 0 && c <= 8) count = c;
    }
    for (uint8_t i = 0; i < count; i++) {
        float v = 0.0f;
        if (qemu_plugin_read_memory(values + 4u * i, (uint8_t *)&v, 4) != 0) break;
        if (!(v == v)) v = 1000.0f;                 /* NaN guard */
        if (v < 1000.0f) v = 1000.0f;
        if (v > 2000.0f) v = 2000.0f;
        int channel = i < 4 ? bf_to_fmu[i] : i;
        uint16_t pwm = (uint16_t)lrintf(v);
        if (bf_debug() && (last_pwm[i] == 0 || abs((int)pwm - (int)last_pwm[i]) >= 25)) {
            fprintf(stderr, "betaflight: t=%.3f motor%u -> fmu ch%d = %u us\n", bf_now_ns() / 1e9, i + 1, channel, pwm);
            last_pwm[i] = pwm;
        }
        if (!phy_set_servo_pwm(channel, pwm)) {
            fprintf(stderr, "betaflight: phy_set_servo_pwm(%d,%u) failed\n", channel, pwm);
        }
    }
}

/* ------------------------------------------------------------------------- */
/* Diagnostics                                                                */
/* ------------------------------------------------------------------------- */

/** failureMode(failureMode_e mode): make a silent LED loop visible and fatal.
 *  Rule: `failureMode bf_failure_mode *` */
static void bf_failure_mode(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    fprintf(stderr, "betaflight: failureMode(%u) at %.3f s virtual -- firmware gave up\n",
            qemu_get_register(ARM_V7M_R0), bf_now_ns() / 1e9);
    fflush(stderr);
    exit(EXIT_FAILURE);
}

/** systemFaultAction(): on F4 every Cortex-M fault handler branches straight
 *  here (fault_handlers.c `#else` branch), so SP still points at the exception
 *  frame {r0,r1,r2,r3,r12,lr,pc,xpsr} and LR holds EXC_RETURN. Dump the frame
 *  and the SCB fault registers, then stop so a fault is diagnosed instead of
 *  silently rebooting. Rule: `systemFaultAction bf_fault_trace` */
static void bf_fault_trace(unsigned int cpu_index, void *udata)
{
    (void)cpu_index; (void)udata;
    uint32_t sp = qemu_get_register(ARM_V7M_SP);
    uint32_t exc_return = qemu_get_register(ARM_V7M_LR);
    uint32_t frame[8] = {0}, scb[4] = {0};
    qemu_plugin_read_memory(sp, (uint8_t *)frame, sizeof(frame));
    qemu_plugin_read_memory(0xE000ED28u, (uint8_t *)&scb[0], 4);   /* CFSR */
    qemu_plugin_read_memory(0xE000ED2Cu, (uint8_t *)&scb[1], 4);   /* HFSR */
    qemu_plugin_read_memory(0xE000ED38u, (uint8_t *)&scb[2], 4);   /* BFAR */
    qemu_plugin_read_memory(0xE000ED34u, (uint8_t *)&scb[3], 4);   /* MMFAR */
    uint32_t ipsr = frame[7];
    fprintf(stderr,
            "betaflight: FAULT (systemFaultAction) at %.6f s virtual, EXC_RETURN=0x%08x\n"
            "  CFSR=0x%08x HFSR=0x%08x BFAR=0x%08x MMFAR=0x%08x\n"
            "  frame@0x%08x: r0=0x%08x r1=0x%08x r2=0x%08x r3=0x%08x r12=0x%08x\n"
            "  stacked LR=0x%08x PC=0x%08x xPSR=0x%08x\n",
            bf_now_ns() / 1e9, exc_return, scb[0], scb[1], scb[2], scb[3], sp,
            frame[0], frame[1], frame[2], frame[3], frame[4], frame[5], frame[6], ipsr);
    fflush(stderr);
    if (!getenv("FASTDYN_BF_CONTINUE_ON_FAULT")) exit(EXIT_FAILURE);
}

/** Observe-only trace helper: `<symbol> bf_trace <tag>` prints the tag + R0 for
 *  the first BF_TRACE_LIMIT hits of each PC (hot functions stay quiet after that). */
#define BF_TRACE_LIMIT 3
static void bf_trace(unsigned int cpu_index, void *udata)
{
    (void)cpu_index;
    static struct { uint32_t pc; uint32_t hits; } seen[96];
    static size_t used;
    uint32_t pc = qemu_get_register(ARM_V7M_PC);
    size_t i;
    for (i = 0; i < used; i++) if (seen[i].pc == pc) break;
    if (i == used) {
        if (used == sizeof(seen) / sizeof(seen[0])) return;
        seen[used].pc = pc; seen[used].hits = 0; used++;
    }
    if (++seen[i].hits > BF_TRACE_LIMIT) return;
    fprintf(stderr, "betaflight: trace %s pc=0x%08x r0=0x%08x t=%.6f%s\n",
            udata ? (const char *)udata : "", pc, qemu_get_register(ARM_V7M_R0),
            bf_now_ns() / 1e9, seen[i].hits == BF_TRACE_LIMIT ? " (muted from now on)" : "");
}

/* ------------------------------------------------------------------------- */

int betaflight_init_virtuals(int argc, char **argv)
{
    (void)argc; (void)argv;
    betaflight_hardware_init_virtuals();
    virtual_register("bf_micros32", bf_micros32);
    virtual_register("bf_millis", bf_millis);
    virtual_register("bf_cycle_counter", bf_cycle_counter);
    virtual_register("bf_cycle_counter_init", bf_cycle_counter_init);

    virtual_register("bf_config_write_word", bf_config_write_word);
    virtual_register("bf_persistent_read", bf_persistent_read);
    virtual_register("bf_persistent_write", bf_persistent_write);

    virtual_register("bf_uart_write", bf_uart_write);
    virtual_register("bf_uart_write_buf", bf_uart_write_buf);
    virtual_register("bf_uart_end_write", bf_uart_end_write);
    virtual_register("bf_uart_rx_waiting", bf_uart_rx_waiting);
    virtual_register("bf_uart_read", bf_uart_read);
    virtual_register("bf_uart_tx_free", bf_uart_tx_free);
    virtual_register("bf_uart_tx_empty", bf_uart_tx_empty);
    virtual_register("bf_serial_pump", bf_serial_pump);

    virtual_register("bf_gyro_init", bf_gyro_init);
    virtual_register("bf_gyro_read", bf_gyro_read);
    virtual_register("bf_acc_read", bf_acc_read);
    virtual_register("bf_baro_calculate", bf_baro_calculate);
    virtual_register("bf_gps_update_count", bf_gps_update_count);
    virtual_register("bf_gps_fill", bf_gps_fill);

    virtual_register("bf_motor_write_all", bf_motor_write_all);

    virtual_register("bf_failure_mode", bf_failure_mode);
    virtual_register("bf_fault_trace", bf_fault_trace);
    virtual_register("bf_trace", bf_trace);
    return 0;
}
