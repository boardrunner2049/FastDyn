/* INAV MATEKF405 MPU6500 simulation bridge. */
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <math.h>
#include <limits.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#include "sensors.h"
#include <string.h>
#include "virtuals.h"
#include "phy.h"

/* Advance FMI on the emulated clock, without injecting a second SysTick:
 * INAV configures QEMU's Cortex-M SysTick during systemInit. */
static void inav_tick(void *opaque)
{
    (void)opaque;
    if (phy_backend_is("fmu")) {
        int64_t ns = qemu_plugin_get_virtual_timer();
        if (ns >= 0 && !phy_advance_simulation((double)ns / 1e9)) {
            fprintf(stderr, "INAV: FMU step failed at %.6f s\n", (double)ns / 1e9);
            exit(EXIT_FAILURE);
        }
    }
}

static void inav_start_clock(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    static int started;
    if (!started) {
        started = 1;
        qemu_plugin_timer_new_period_ns(inav_tick, NULL, 1000000);
    }
}

static void inav_return(uint32_t value)
{
    qemu_set_register(value, ARM_V7M_R0);
    qemu_set_register(qemu_get_register(ARM_V7M_LR), ARM_V7M_PC);
}

/* INAV on this STM32 target uses a 64-bit timeUs_t: return both words. */
static void inav_micros64(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    int64_t ns = qemu_plugin_get_virtual_timer();
    if (ns < 0) return;
    uint64_t us = (uint64_t)ns / 1000;
    qemu_set_register((uint32_t)(us >> 32), ARM_V7M_R1);
    inav_return((uint32_t)us);
}

static void inav_bus_write(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    inav_return(1);
}

static void inav_bus_transfer(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    inav_return(0);
}

static void inav_bus_read(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    uint32_t reg = qemu_get_register(ARM_V7M_R1);
    uint32_t dst = qemu_get_register(ARM_V7M_R2);
    if (qemu_get_register(ARM_V7M_R0) != 0x2001d1f0 || reg != 0x75 || !dst) {
        inav_return(0); return;
    }
    uint8_t value = 0x70;
    inav_return(qemu_plugin_write_memory(dst, &value, 1) == 0);
}

static void inav_bus_read_buf(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    uint32_t reg = qemu_get_register(ARM_V7M_R1);
    uint32_t dst = qemu_get_register(ARM_V7M_R2);
    uint32_t len = qemu_get_register(ARM_V7M_R3);
    if (qemu_get_register(ARM_V7M_R0) != 0x2001d1f0 ||
        (reg & 0x7f) != 0x3b || len != 14 || !dst) {
        inav_return(0); return;
    }
    imu_batch_t batch;
    memset(&batch, 0, sizeof(batch));
    if (!phy_get_imu_batch(&batch)) { inav_return(0); return; }
    uint8_t bytes[14];
    inav_mpu6500_encode(&batch.imu[0], phy_get_imu_frame(), bytes);
    if (qemu_plugin_write_memory(dst, bytes, sizeof(bytes)) != 0) { inav_return(0); return; }
    static int announced;
    if (!announced) { announced = 1; puts("INAV bridge: MPU6500 sensor connected"); fflush(stdout); }
    inav_return(1);
}

/* The offsets below are the DWARF-verified MATEKF405 ABI for the pinned
 * firmware. Its profile must be regenerated for any other ELF. */
static int16_t clamp16(double value)
{
    if (!isfinite(value)) return 0;
    if (value > INT16_MAX) return INT16_MAX;
    if (value < INT16_MIN) return INT16_MIN;
    return (int16_t)lround(value);
}

static int32_t clamp32(double value)
{
    if (!isfinite(value)) return 0;
    if (value > INT32_MAX) return INT32_MAX;
    if (value < INT32_MIN) return INT32_MIN;
    return (int32_t)llround(value);
}

static void inav_mag(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    uint32_t dev = qemu_get_register(ARM_V7M_R0);
    vector3d_t mag;
    if (!dev || !phy_get_mag_reading(&mag)) { inav_return(0); return; }
    double sign = phy_get_imu_frame() == PHY_BODY_FRAME_FRD ? 1.0 : -1.0;
    int16_t values[3] = {clamp16(mag.x * 1024), clamp16(sign * mag.y * 1024),
                         clamp16(sign * mag.z * 1024)};
    if (qemu_plugin_write_memory(dev + 54, (uint8_t *)values, sizeof(values)) != 0) {
        inav_return(0); return;
    }
    static int announced;
    if (!announced) { announced = 1; puts("INAV bridge: magnetometer connected"); fflush(stdout); }
    inav_return(1);
}

static void inav_baro(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    barometer_t sample;
    if (!phy_get_barometer_reading(&sample)) { inav_return(0); return; }
    uint32_t pressure_addr = qemu_get_register(ARM_V7M_R1);
    uint32_t temperature_addr = qemu_get_register(ARM_V7M_R2);
    int32_t pressure = clamp32(sample.pressure_pa);
    int32_t temperature = clamp32(sample.temperature_c * 100.0);
    if (pressure_addr && qemu_plugin_write_memory(pressure_addr, (uint8_t *)&pressure, 4) != 0) {
        inav_return(0); return;
    }
    if (temperature_addr && qemu_plugin_write_memory(temperature_addr, (uint8_t *)&temperature, 4) != 0) {
        inav_return(0); return;
    }
    static int announced;
    if (!announced) { announced = 1; puts("INAV bridge: barometer connected"); fflush(stdout); }
    inav_return(1);
}

static uint32_t parse_address(void *data)
{
    if (!data) return 0;
    char *end;
    unsigned long value = strtoul((const char *)data, &end, 0);
    return end != data && !*end && value <= UINT32_MAX ? (uint32_t)value : 0;
}

static void inav_gps(unsigned int cpu, void *data)
{
    (void)cpu;
    uint32_t addr = parse_address(data);
    gps_data_t gps;
    if (!addr) return;
    uint8_t solution[64];
    if (qemu_plugin_read_memory(addr, solution, sizeof(solution)) != 0) return;
    if (!phy_get_navsat_reading(&gps)) {
        solution[2] = solution[3] = solution[4] = 0;
        solution[9] = solution[10] = 0;
        qemu_plugin_write_memory(addr, solution, sizeof(solution));
        return;
    }
    solution[2] = solution[3] = solution[4] = 1; /* velocity and EPE valid */
    solution[5] = solution[6] = solution[7] = solution[8] = 0;
    solution[9] = 2; /* GPS_FIX_3D */
    solution[10] = 12;
    int32_t llh[3] = {clamp32(gps.lat * 1e7), clamp32(gps.lon * 1e7), clamp32(gps.alt * 100)};
    int16_t vel[3] = {clamp16(gps.vel_n * 100), clamp16(gps.vel_e * 100), clamp16(gps.vel_d * 100)};
    int16_t speed = clamp16(hypot(gps.vel_n, gps.vel_e) * 100);
    double course = atan2(gps.vel_e, gps.vel_n) * (1800.0 / 3.14159265358979323846);
    if (course < 0) course += 3600;
    int16_t heading = clamp16(course);
    uint16_t accuracy = 100;
    memcpy(solution + 12, llh, sizeof(llh));
    memcpy(solution + 24, vel, sizeof(vel));
    memcpy(solution + 30, &speed, 2);
    memcpy(solution + 32, &heading, 2);
    memcpy(solution + 34, &accuracy, 2);
    memcpy(solution + 36, &accuracy, 2);
    memcpy(solution + 52, &accuracy, 2);
    if (qemu_plugin_write_memory(addr, solution, sizeof(solution)) != 0) return;
    static int announced;
    if (!announced) { announced = 1; puts("INAV bridge: GPS connected"); fflush(stdout); }
}

/* Copter.mo's motor moment map is expressed in FLU. Convert its pitch and
 * yaw moments to INAV's FRD axes and initialize the otherwise empty mixer. */
static void inav_mixer(unsigned int cpu, void *data)
{
    (void)cpu;
    uint32_t address = parse_address(data);
    if (!address) return;
    float mix[4][4] = {
        {1, -1,  1,  1}, {1, 1, -1,  1},
        {1,  1,  1, -1}, {1, -1, -1, -1},
    };
    qemu_plugin_write_memory(address, (uint8_t *)mix, sizeof(mix));
}

static void inav_motors(unsigned int cpu, void *data)
{
    (void)cpu;
    uint32_t address = parse_address(data);
    if (!address) return;
    int16_t pwm[4];
    if (qemu_plugin_read_memory(address, (uint8_t *)pwm, sizeof(pwm)) != 0) return;
    for (int i = 0; i < 4; ++i) phy_set_servo_pwm(i, pwm[i]);
    static int announced;
    if (!announced) { announced = 1; puts("INAV bridge: motor outputs connected"); fflush(stdout); }
}

/* The receiver stays in failsafe unless fresh, complete packets arrive.
 * Wire format: "FDRC" followed by eight little-endian PWM uint16 values
 * in AETR/AUX1..4 order. Bind loopback only. */
static uint16_t rc_channels[8];
static int rc_socket = -2;
static void inav_rc_status(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    if (rc_socket == -2) {
        rc_socket = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
        if (rc_socket >= 0) {
            struct sockaddr_in local = {0};
            local.sin_family = AF_INET;
            local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            local.sin_port = htons(14590);
            if (bind(rc_socket, (struct sockaddr *)&local, sizeof(local)) != 0) {
                perror("INAV receiver bind"); close(rc_socket); rc_socket = -1;
            }
        }
    }
    uint8_t packet[21];
    int complete = 0;
    if (rc_socket >= 0) {
        ssize_t count;
        for (unsigned packets = 0; packets < 32 &&
             (count = recv(rc_socket, packet, sizeof(packet), MSG_DONTWAIT)) >= 0; ++packets) {
            if (count != 20 || memcmp(packet, "FDRC", 4)) continue;
            uint16_t candidate[8];
            int valid = 1;
            for (int i = 0; i < 8; ++i) {
                candidate[i] = packet[4 + 2*i] | (uint16_t)packet[5 + 2*i] << 8;
                if (candidate[i] < 900 || candidate[i] > 2100) valid = 0;
            }
            if (valid) { memcpy(rc_channels, candidate, sizeof(candidate)); complete = 1; }
        }
    }
    inav_return(complete); /* RX_FRAME_COMPLETE / RX_FRAME_PENDING */
}

static void inav_rc_read(unsigned int cpu, void *data)
{
    (void)cpu; (void)data;
    unsigned channel = qemu_get_register(ARM_V7M_R1);
    inav_return(channel < 8 ? rc_channels[channel] : 0);
}

int inav_init_virtuals(int argc, char **argv)
{
    (void)argc; (void)argv;
    virtual_register("inav_start_clock", inav_start_clock);
    virtual_register("inav_rc_status", inav_rc_status);
    virtual_register("inav_rc_read", inav_rc_read);
    virtual_register("inav_motors", inav_motors);
    virtual_register("inav_mixer", inav_mixer);
    virtual_register("inav_mag", inav_mag);
    virtual_register("inav_baro", inav_baro);
    virtual_register("inav_gps", inav_gps);
    virtual_register("inav_micros64", inav_micros64);
    virtual_register("inav_bus_write", inav_bus_write);
    virtual_register("inav_bus_transfer", inav_bus_transfer);
    virtual_register("inav_bus_read", inav_bus_read);
    virtual_register("inav_bus_read_buf", inav_bus_read_buf);
    return 0;
}
