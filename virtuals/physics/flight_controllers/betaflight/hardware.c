/* MATEKF405CTR hardware boundary. The guest owns sensor detection, decoding,
 * calibration, compensation and control. Offsets are checked by the profile. */
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "virtuals.h"
#include "phy.h"
#include "hardware.h"

static uint8_t mpu[128];
static bool initialized;
static uint64_t spi_samples, bmp_samples;
static double min_pressure = 1e9, max_pressure;
static double max_gyro_dps;
static void report(void) {
    if (spi_samples) fprintf(stderr,
        "bf-hw: summary MPU6000_SPI_reads=%llu BMP280_I2C_reads=%llu pressure_range=%.1f..%.1fPa max_gyro=%.1fdps\n",
        (unsigned long long)spi_samples, (unsigned long long)bmp_samples,
        min_pressure, max_pressure, max_gyro_dps);
}
static int64_t now_ns(void) { return qemu_plugin_get_virtual_timer(); }
static void ret(uint32_t value) {
    qemu_set_register(value, ARM_V7M_R0);
    qemu_set_register(qemu_get_register(ARM_V7M_LR), ARM_V7M_PC);
}
static void read_mem(uint32_t addr, void *data, size_t size) {
    if (qemu_plugin_read_memory(addr, data, size)) {
        fprintf(stderr, "bf-hw: invalid guest read 0x%08x (%zu)\n", addr, size);
        exit(EXIT_FAILURE);
    }
}
static void write_mem(uint32_t addr, const void *data, size_t size) {
    if (qemu_plugin_write_memory(addr, (uint8_t *)data, size)) {
        fprintf(stderr, "bf-hw: invalid guest write 0x%08x (%zu)\n", addr, size);
        exit(EXIT_FAILURE);
    }
}
static uint32_t u32(uint32_t addr) { uint32_t v; read_mem(addr, &v, 4); return v; }
static void put8(uint32_t addr, uint8_t v) { write_mem(addr, &v, 1); }
static void put32(uint32_t addr, uint32_t v) { write_mem(addr, &v, 4); }
static void init_mpu(void) {
    memset(mpu, 0, sizeof(mpu));
    mpu[0x75] = 0x68; /* WHO_AM_I, accgyro_mpu.h */
    mpu[0x0c] = 0x58; /* MPU6000_REV_D8, accgyro_spi_mpu6000.c */
    mpu[0x6b] = 0x40;
    initialized = true;
}
static int16_t quantize(double value) {
    if (!isfinite(value)) { fprintf(stderr, "bf-hw: nonfinite sensor sample\n"); exit(EXIT_FAILURE); }
    if (value > 32767) value = 32767;
    if (value < -32768) value = -32768;
    return (int16_t)lrint(value);
}
static void be16(uint8_t *p, int16_t v) { p[0] = (uint16_t)v >> 8; p[1] = v; }
static void sample_mpu(void) {
    imu_batch_t batch;
    if (!phy_get_imu_batch(&batch)) { fprintf(stderr, "bf-hw: IMU unavailable\n"); exit(EXIT_FAILURE); }
    imu_t *s = &batch.imu[16];
    /* FRD -> Betaflight body (x,-y,-z), then inverse CW270 board rotation.
     * CW270 maps raw (x,y,z) to body (-y,x,z). */
    double a[3] = {-s->accel_body.y, -s->accel_body.x, -s->accel_body.z};
    double g[3] = {-s->gyro.y, -s->gyro.x, -s->gyro.z};
    double acc_lsb = 16384.0 / (1u << ((mpu[0x1c] >> 3) & 3));
    double gyro_lsb = 131.072 / (1u << ((mpu[0x1b] >> 3) & 3));
    for (int i = 0; i < 3; i++) {
        be16(mpu + 0x3b + 2*i, quantize(a[i] / 9.80665 * acc_lsb));
        be16(mpu + 0x43 + 2*i, quantize(g[i] * 57.29577951308232 * gyro_lsb));
        double dps = fabs(g[i] * 57.29577951308232);
        if (dps > max_gyro_dps) max_gyro_dps = dps;
    }
    be16(mpu + 0x41, quantize((25.0 - 36.53) * 340.0));
    mpu[0x3a] = 1;
}

/* spiSequence(dev, segments): synchronous bus substitution; original SPI
 * wrappers and MPU6000 detection/init/read functions continue in the guest.
 * No EXTI is generated, so the driver naturally selects its polling mode. */
static void hw_spi_sequence(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    if (!initialized) init_mpu();
    uint32_t dev = qemu_get_register(ARM_V7M_R0), seg = qemu_get_register(ARM_V7M_R1);
    uint32_t bus = u32(dev), spi = u32(bus + 4);
    bool is_mpu = spi == 0x40013000u;
    bool command = false, reading = false;
    uint8_t reg = 0;
    for (int index = 0; index < 16; index++, seg += 20) {
        uint32_t tx = u32(seg), rx = u32(seg + 4), len = u32(seg + 8), cb = u32(seg + 16);
        uint8_t cs;
        read_mem(seg + 12, &cs, 1);
        if (!len) break;
        if (len > 512 || cb) { fprintf(stderr, "bf-hw: unsupported SPI segment len=%u callback=%x\n", len, cb); exit(EXIT_FAILURE); }
        uint8_t in[512] = {0}, out[512];
        if (tx) read_mem(tx, in, len);
        for (uint32_t i = 0; i < len; i++) {
            out[i] = 0xff;
            if (!is_mpu) continue;
            if (!command) {
                reg = in[i] & 0x7f; reading = (in[i] & 0x80) != 0; command = true;
                if (reading && reg >= 0x3a && reg <= 0x48) {
                    sample_mpu();
                    if (++spi_samples == 1) fprintf(stderr, "bf-hw: MPU6000 raw SPI samples active (physical driver, polling)\n");
                }
            } else {
                if (reading) out[i] = mpu[reg];
                else if (reg == 0x6b && (in[i] & 0x80)) init_mpu();
                else mpu[reg] = in[i];
                reg = (reg + 1) & 0x7f;
            }
        }
        if (rx) write_mem(rx, out, len);
        if (cs) command = false;
    }
    put32(bus + 32, 0); /* BUS_SPI_FREE; spiWait() remains real. */
    ret(0);
}

/* Bosch reference coefficients, also used by Betaflight's BMP280 unit tests.
 * Invert the same transfer function to encode plant pressure as a raw ADC.
 * The ARM driver reads coefficients and performs its own integer compensation. */
static const int32_t coeff[12] = {27504,26435,-1000,36477,-10685,3024,2855,140,-7,15500,-14600,6000};
static int32_t temperature_fine(int32_t adc) {
    int32_t v1 = (((adc >> 3) - (coeff[0] * 2)) * coeff[1]) >> 11;
    int32_t d = (adc >> 4) - coeff[0];
    return v1 + (((d*d >> 12) * coeff[2]) >> 14);
}
static double pressure_value(int32_t adc, int32_t fine) {
    double v1 = fine / 2.0 - 64000.0;
    double v2 = v1*v1*coeff[8]/32768.0 + v1*coeff[7]*2.0;
    v2 = v2/4.0 + coeff[6]*65536.0;
    v1 = (coeff[5]*v1*v1/524288.0 + coeff[4]*v1)/524288.0;
    v1 = (1.0 + v1/32768.0)*coeff[3];
    double p = (1048576.0 - adc - v2/4096.0)*6250.0/v1;
    return p + (coeff[11]*p*p/2147483648.0 + p*coeff[10]/32768.0 + coeff[9])/16.0;
}
static void sample_bmp(uint8_t regs[256]) {
    barometer_t b;
    if (!phy_get_barometer_reading(&b)) { fprintf(stderr, "bf-hw: barometer unavailable\n"); exit(EXIT_FAILURE); }
    if (!isfinite(b.pressure_pa) || !isfinite(b.temperature_c) || b.pressure_pa <= 0) {
        fprintf(stderr, "bf-hw: invalid barometer sample\n"); exit(EXIT_FAILURE);
    }
    if (b.pressure_pa < min_pressure) min_pressure = b.pressure_pa;
    if (b.pressure_pa > max_pressure) max_pressure = b.pressure_pa;
    int low = 0, high = 1048575;
    while (low < high) {
        int mid = (low + high)/2;
        double t = (temperature_fine(mid)*5.0 + 128)/25600.0;
        if (t < b.temperature_c) low = mid + 1; else high = mid;
    }
    int32_t t = low, fine = temperature_fine(t);
    low = 0; high = 1048575;
    while (low < high) {
        int mid = (low + high)/2;
        if (pressure_value(mid, fine) > b.pressure_pa) low = mid + 1; else high = mid;
    }
    int32_t p = low;
    regs[0xf7] = p >> 12; regs[0xf8] = p >> 4; regs[0xf9] = p << 4;
    regs[0xfa] = t >> 12; regs[0xfb] = t >> 4; regs[0xfc] = t << 4;
    if (++bmp_samples == 1) fprintf(stderr, "bf-hw: BMP280 raw I2C samples active (ARM compensation retained)\n");
}
static bool bmp_device(uint32_t dev, bool legacy) {
    if (legacy) {
        uint8_t addr; read_mem(dev + 5, &addr, 1);
        uint8_t bus; read_mem(dev + 4, &bus, 1);
        return bus == 0 && addr == 0x76;
    }
    uint32_t bus = u32(dev);
    uint8_t addr; read_mem(dev + 4, &addr, 1);
    return bus && u32(bus + 4) == 0 && addr == 0x76;
}
static void hw_i2c_read(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    uint32_t dev = qemu_get_register(ARM_V7M_R0), reg = qemu_get_register(ARM_V7M_R1);
    uint32_t dest = qemu_get_register(ARM_V7M_R2), len = qemu_get_register(ARM_V7M_R3);
    if (!bmp_device(dev, data == (void *)1) || len > 256 || reg + len > 256) { ret(0); return; }
    uint8_t regs[256] = {0}; regs[0xd0] = 0x58;
    for (int i = 0; i < 12; i++) { regs[0x88 + 2*i] = coeff[i]; regs[0x89 + 2*i] = (uint16_t)coeff[i] >> 8; }
    if (reg <= 0xfc && reg + len > 0xf7) sample_bmp(regs);
    if (reg == 0xd0) fprintf(stderr, "bf-hw: BMP280 identity read via I2C1 address 0x76\n");
    write_mem(dest, regs + reg, len); ret(1);
}
static void hw_i2c_write(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    ret(bmp_device(qemu_get_register(ARM_V7M_R0), data == (void *)1));
}
static void hw_i2c_legacy_read(unsigned cpu, void *data) {
    (void)data; hw_i2c_read(cpu, (void *)1);
}
static void hw_i2c_legacy_write(unsigned cpu, void *data) {
    (void)data; hw_i2c_write(cpu, (void *)1);
}

/* 4.2's busDevice stores SPI/I2C directly, before extDevice/segments existed.
 * Retain the original MPU6000 register read/write driver above this transport. */
static bool legacy_mpu(uint32_t dev) { return u32(dev + 4) == 0x40013000u; }
static void hw_spi_legacy_read(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    uint32_t dev=qemu_get_register(ARM_V7M_R0), reg=qemu_get_register(ARM_V7M_R1)&0x7f;
    uint32_t dest=qemu_get_register(ARM_V7M_R2), len=qemu_get_register(ARM_V7M_R3);
    if (!legacy_mpu(dev) || len > 128 || reg + len > 128) { ret(0); return; }
    if (!initialized) init_mpu();
    if (reg >= 0x3a && reg <= 0x48) {
        sample_mpu();
        if (++spi_samples == 1) fprintf(stderr, "bf-hw: MPU6000 raw SPI samples active (legacy physical driver)\n");
    }
    write_mem(dest, mpu + reg, len); ret(1);
}
static void hw_spi_legacy_byte(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    if (!initialized) init_mpu();
    ret(legacy_mpu(qemu_get_register(ARM_V7M_R0)) ? mpu[qemu_get_register(ARM_V7M_R1)&0x7f] : 0xff);
}
static void hw_spi_legacy_write(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    if (!initialized) init_mpu();
    if (!legacy_mpu(qemu_get_register(ARM_V7M_R0))) { ret(0); return; }
    uint32_t reg=qemu_get_register(ARM_V7M_R1)&0x7f, value=qemu_get_register(ARM_V7M_R2);
    if (reg == 0x6b && (value & 0x80)) init_mpu(); else mpu[reg]=value;
    ret(1);
}
static void hw_spi_legacy_transfer(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    uint32_t dev=qemu_get_register(ARM_V7M_R0), tx=qemu_get_register(ARM_V7M_R1);
    uint32_t rx=qemu_get_register(ARM_V7M_R2), len=qemu_get_register(ARM_V7M_R3);
    if (!legacy_mpu(dev) || !tx || !rx || !len || len > 128) { ret(0); return; }
    uint8_t in[128], out[128]; read_mem(tx,in,len); memset(out,0xff,len);
    if (!initialized) init_mpu();
    uint8_t reg=in[0]&0x7f;
    if (!(in[0]&0x80) || reg+len-1 > 128) { ret(0); return; }
    if (reg >= 0x3a && reg <= 0x48) {
        sample_mpu();
        if (++spi_samples == 1) fprintf(stderr, "bf-hw: MPU6000 raw SPI samples active (legacy physical driver)\n");
    }
    memcpy(out+1,mpu+reg,len-1); write_mem(rx,out,len); ret(1);
}
static void hw_i2c_busy(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    uint32_t error = qemu_get_register(ARM_V7M_R1);
    if (error) put8(error, 0);
    ret(0);
}

/* STM32 SPL flash transport used by releases before configWriteWord existed.
 * ARM config serialization and its CRC remain in the firmware. */
static void hw_flash_word(unsigned cpu, void *data) {
    (void)cpu; (void)data;
    put32(qemu_get_register(ARM_V7M_R0), qemu_get_register(ARM_V7M_R1));
    ret(9); /* FLASH_COMPLETE in STM32F4 StdPeriph FLASH_Status. */
}

/* Apply explicit test-run parameters before featureInit snapshots the feature
 * mask. This changes guest configuration, not the compiled hardware firmware.
 * Every pointer and consumed field is validated against this ELF's DWARF. */
static void hw_config(unsigned cpu, void *data) {
    (void)cpu;
    uint32_t a[10]; const char *p = data;
    for (int i = 0; i < 10; i++) {
        char *end; a[i] = strtoul(p, &end, 0);
        if (end == p) { fprintf(stderr, "bf-hw: missing configuration addresses\n"); exit(EXIT_FAILURE); }
        p = *end == ',' ? end + 1 : end;
    }
    put32(a[0], 128 | 1024 | 8); /* GPS, telemetry, serial RX; OSD/storage not needed. */
    put8(a[1] + 8, 16); put8(a[1] + 50, 51); /* MAVLink RX, UART1. */
    put8(a[2] + 2, 0); put8(a[2] + 4, 1); /* PWM, continuous update. */
    put8(a[3] + 1, 20); put8(a[3] + 2, 52); put8(a[3] + 3, 0xff); /* Required USB MSP plus UART2. */
    put8(a[4], 1); put8(a[4] + 2, 0); put8(a[4] + 3, 0); /* UBX, no autoconfig/autobaud. */
    put8(a[4] + 6, 10); put8(a[4] + 78, 53); /* GPS UART3, 10 Hz. */
    uint8_t modes[12] = {0,0,32,48,0,0, 1,1,32,48,0,0};
    write_mem(a[5], modes, sizeof(modes)); /* AUX1 ARM, AUX2 ANGLE, 1700..2100 us. */
    put8(a[6] + 4, 0); put8(a[7] + 5, 0); /* SD off, blackbox NONE. */
    put8(a[8] + 3, 0); put8(a[8] + 4, 0); /* voltage/current source NONE. */
    put8(a[9], 8); /* pid_process_denom=8: 8 kHz MPU gyro, 1 kHz PID. */
    static bool shown;
    if (!shown) { fprintf(stderr, "bf-hw: runtime parameters applied: MAVLink UART1, MSP UART2, UBX UART3, PWM, AUX ARM/ANGLE\n"); shown = true; }
}

/* Version-sweep parameter writes. Symbol addresses are resolved by the profile
 * generator; consumed field offsets are checked against the pinned ELF DWARF.
 * Each entry is address/width/value, applied before featureInit. */
static void hw_parameters(unsigned cpu, void *data) {
    (void)cpu;
    FILE *input = fopen(data, "r");
    if (!input) { perror("bf-hw: parameter file"); exit(EXIT_FAILURE); }
    char line[128];
    while (fgets(line, sizeof(line), input)) {
        const char *p = line;
        char *end;
        uint32_t address = strtoul(p, &end, 0);
        if (end == p || *end != '/') goto invalid;
        p = end + 1;
        unsigned width = strtoul(p, &end, 0);
        if (end == p || *end != '/') goto invalid;
        p = end + 1;
        uint32_t value = strtoul(p, &end, 0);
        if (end == p || (*end && *end != '\n') ||
                (width != 1 && width != 2 && width != 4)) goto invalid;
        write_mem(address, &value, width);
    }
    fclose(input);
    fprintf(stderr, "bf-hw: version-sweep runtime parameters applied\n");
    return;
invalid:
    fprintf(stderr, "bf-hw: invalid parameter write: %s\n", (const char *)data);
    exit(EXIT_FAILURE);
}

static void le32(uint8_t *p, int32_t v) { memcpy(p, &v, 4); }
static size_t ubx_frame(uint8_t *out, uint8_t cls, uint8_t id, const uint8_t *p, size_t len) {
    out[0] = 0xb5; out[1] = 0x62; out[2] = cls; out[3] = id; out[4] = len; out[5] = len >> 8;
    memcpy(out + 6, p, len); uint8_t a = 0, b = 0;
    for (size_t i = 2; i < len + 6; i++) { a += out[i]; b += a; }
    out[len + 6] = a; out[len + 7] = b; return len + 8;
}
size_t betaflight_hardware_gps_bytes(uint8_t *out, size_t capacity) {
    static int64_t previous = -100000000;
    int64_t now = now_ns();
    if (capacity < 148 || now - previous < 100000000) return 0;
    previous = now;
    gps_data_t g;
    if (!phy_get_navsat_reading(&g)) return 0;
    const char *legacy = getenv("FASTDYN_BF_GPS_LEGACY");
    if (legacy && strcmp(legacy, "1") == 0) {
        /* 4.2 predates NAV-PVT support: original gps.c consumes NAV-SOL,
         * NAV-POSLLH and NAV-VELNED, with velocity in cm/s. */
        uint8_t sol[52]={0}, pos[28]={0}, vel[36]={0};
        le32(sol,now/1000000); sol[8]=0x8c; sol[9]=9; sol[10]=3; sol[11]=0x0d;
        le32(sol+24,150); le32(sol+40,100); sol[44]=100; sol[47]=12;
        le32(pos,now/1000000); le32(pos+4,llround(g.lon*1e7)); le32(pos+8,llround(g.lat*1e7));
        le32(pos+12,llround(g.alt*1000)); le32(pos+16,llround(g.alt*1000)); le32(pos+20,100); le32(pos+24,150);
        le32(vel,now/1000000); le32(vel+4,llround(g.vel_n*100)); le32(vel+8,llround(g.vel_e*100));
        le32(vel+12,llround(g.vel_d*100)); le32(vel+16,llround(sqrt(g.vel_n*g.vel_n+g.vel_e*g.vel_e+g.vel_d*g.vel_d)*100));
        le32(vel+20,llround(hypot(g.vel_n,g.vel_e)*100));
        double course=atan2(g.vel_e,g.vel_n)*57.29577951308232;
        if (course<0) course+=360;
        le32(vel+24,llround(course*1e5)); le32(vel+28,100); le32(vel+32,100000);
        size_t n=ubx_frame(out,1,6,sol,sizeof(sol));
        n+=ubx_frame(out+n,1,2,pos,sizeof(pos));
        return n+ubx_frame(out+n,1,0x12,vel,sizeof(vel));
    }
    uint8_t p[92] = {0}, ver[40] = {0};
    le32(p, now / 1000000); p[4] = 0xea; p[5] = 7; p[6] = 11; p[7] = 10; p[11] = 7;
    p[20] = 3; p[21] = 1; p[23] = 12;
    le32(p + 24, llround(g.lon*1e7)); le32(p + 28, llround(g.lat*1e7));
    le32(p + 32, llround(g.alt*1000)); le32(p + 36, llround(g.alt*1000));
    le32(p + 40, 100); le32(p + 44, 150);
    le32(p + 48, llround(g.vel_n*1000)); le32(p + 52, llround(g.vel_e*1000)); le32(p + 56, llround(g.vel_d*1000));
    le32(p + 60, llround(hypot(g.vel_n,g.vel_e)*1000));
    double course = atan2(g.vel_e, g.vel_n)*57.29577951308232;
    if (course < 0) course += 360;
    le32(p + 64, llround(course*1e5)); le32(p + 68, 100); le32(p + 72, 100000); p[76] = 100;
    size_t n = ubx_frame(out, 1, 7, p, sizeof(p));
    memcpy(ver, "FastDyn UBX", 11); memcpy(ver + 30, "00080000", 8);
    return n + ubx_frame(out + n, 0x0a, 4, ver, sizeof(ver));
}

int betaflight_hardware_init_virtuals(void) {
    atexit(report);
    virtual_register("bf_hw_spi_sequence", hw_spi_sequence);
    virtual_register("bf_hw_i2c_read", hw_i2c_read);
    virtual_register("bf_hw_i2c_write", hw_i2c_write);
    virtual_register("bf_hw_i2c_busy", hw_i2c_busy);
    virtual_register("bf_hw_config", hw_config);
    virtual_register("bf_hw_parameters", hw_parameters);
    virtual_register("bf_hw_flash_word", hw_flash_word);
    virtual_register("bf_hw_i2c_legacy_read", hw_i2c_legacy_read);
    virtual_register("bf_hw_i2c_legacy_write", hw_i2c_legacy_write);
    virtual_register("bf_hw_spi_legacy_read", hw_spi_legacy_read);
    virtual_register("bf_hw_spi_legacy_byte", hw_spi_legacy_byte);
    virtual_register("bf_hw_spi_legacy_write", hw_spi_legacy_write);
    virtual_register("bf_hw_spi_legacy_transfer", hw_spi_legacy_transfer);
    return 0;
}
