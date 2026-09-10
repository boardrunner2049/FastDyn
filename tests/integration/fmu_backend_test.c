/* Exercise the real plugin/FMU boundary without running firmware. */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../../virtuals/physics/phy.h"

extern int fmu_init(int argc, char **argv);
extern phy_backend_t fmu_backend;

/* The backend only needs argument lookup from FastDyn's QEMU utilities. */
char *utils_get_arg(const char *key, int argc, char **argv)
{
    size_t length = strlen(key);
    for (int i = 0; i < argc; i++) {
        if (strncmp(argv[i], key, length) == 0 && argv[i][length] == '=') {
            return argv[i] + length + 1;
        }
    }
    return NULL;
}

static void check_stationary(phy_backend_t *backend, const gps_data_t *origin)
{
    gps_data_t position;
    imu_batch_t imu;
    assert(backend->get_navsat_reading(&position));
    assert(backend->get_imu_batch(&imu));
    assert(isfinite(position.lat) && isfinite(position.lon) && isfinite(position.alt));
    assert(fabs(position.lat - origin->lat) < 0.000002);
    assert(fabs(position.lon - origin->lon) < 0.000002);
    assert(fabs(position.alt - origin->alt) < 0.25);
    assert(fabs(position.vel_n) < 0.1 && fabs(position.vel_e) < 0.1);
    assert(fabs(imu.imu[0].gyro.x) < 0.1 && fabs(imu.imu[0].gyro.y) < 0.1);
    assert(imu.imu[0].accel_body.z < -9.0 && imu.imu[0].accel_body.z > -10.5);
}

int main(int argc, char **argv)
{
    assert(argc >= 3);
    phy_backend_t *backend = &fmu_backend;
    assert(fmu_init(argc - 2, argv + 2) == 0 && backend->init());
    gps_data_t origin;
    assert(backend->get_navsat_reading(&origin));
    assert(backend->advance_simulation(40.0));
    check_stationary(backend, &origin);
    for (int channel = 0; channel < 4; channel++) {
        assert(backend->set_servo_pwm(channel, 0));
    }
    assert(backend->advance_simulation(45.0));
    check_stationary(backend, &origin);
    if (strcmp(argv[1], "copter") == 0) {
        for (int channel = 0; channel < 4; channel++) {
            assert(backend->set_servo_pwm(channel, 1700));
        }
    } else {
        assert(backend->set_servo_pwm(2, 1750));
    }
    assert(backend->advance_simulation(47.0));
    gps_data_t moving;
    assert(backend->get_navsat_reading(&moving));
    assert(isfinite(moving.lat) && isfinite(moving.lon) && isfinite(moving.alt));
    if (strcmp(argv[1], "copter") == 0) {
        assert(moving.alt > origin.alt + 0.5);
    } else {
        assert(moving.vel_n > 1.0);
    }
    backend->shutdown();
    printf("%s: neutral startup, disabled PWM, finite sensors, and actuator response passed\n", argv[1]);
    return 0;
}
