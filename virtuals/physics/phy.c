#include "phy.h"
#include <config.h>
#include <stdio.h>
#include <string.h>
#include <utils.h>
#include "physics_engines/stub/stub.h"
#if ENABLE_LIBGZ
#include "physics_engines/gazebo/gazebo.h"
#endif
#if ENABLE_FMU
#include "physics_engines/fmu/fmu.h"
#endif
#if ENABLE_FLIGHT_CONTROLLERS
#include "flight_controllers/fc.h"
#endif

static phy_backend_t *active_backend = NULL;

static phy_backend_entry_t backends[] = {
    // default if none specified
    { "stub", &stub_backend },
#if ENABLE_FMU
    { "fmu", &fmu_backend},
#endif
#if ENABLE_LIBGZ
    { "gazebo", &gazebo_backend},
#endif
    // Future engines can be added here, e.g.:
    // { "casadi", &casadi_backend},
};

int phy_select_backend(const char *name)
{
    if (!name) {
        fprintf(stderr, "phy_select_backend: name cannot be NULL\n");
        return 0;
    }

    for (size_t i = 0; i < sizeof(backends) / sizeof(backends[0]); i++) {
        if (strcmp(name, backends[i].name) == 0) {
            active_backend = backends[i].backend;
            return 1;
        }
    }

    fprintf(stderr, "phy_select_backend: undefined backend '%s'\n", name);
    return 0;
}

int phy_backend_is(const char *name)
{
    return active_backend != NULL && name != NULL && strcmp(active_backend->name, name) == 0;
}

phy_body_frame_t phy_get_imu_frame(void)
{
    return active_backend != NULL ? active_backend->imu_frame : PHY_BODY_FRAME_FLU;
}

int phy_get_trace(trace_t *trace)
{
    if (trace == NULL ||
        !phy_get_imu_batch(&(trace->imu_batch)) ||
        !phy_get_navsat_reading(&(trace->gps))) {
        return 0;
    }

    return 1;
}

int phy_get_imu_batch(imu_batch_t *imu_batch)
{
    if (!active_backend || !active_backend->get_imu_batch) {
        return 0;
    }

    return active_backend->get_imu_batch(imu_batch);
}

int phy_get_mag_reading(vector3d_t *mag)
{
    if (!active_backend || !active_backend->get_mag_reading) {
        return 0;
    }

    return active_backend->get_mag_reading(mag);
}

int phy_get_navsat_reading(gps_data_t *gps_data)
{
    if (!active_backend || !active_backend->get_navsat_reading) {
        return 0;
    }

    return active_backend->get_navsat_reading(gps_data);
}

int phy_set_servo_pwm(int channel, int pwm)
{
    if (!active_backend || !active_backend->set_servo_pwm) {
        return 0;
    }

    return active_backend->set_servo_pwm(channel, pwm);
}

int phy_advance_simulation(double run_until_time)
{
    if (!active_backend || !active_backend->advance_simulation) {
        return 0;
    }

    return active_backend->advance_simulation(run_until_time);
}

int phy_get_joint_state(double *motor_0_pos, double *motor_2_pos)
{
    if (!active_backend || !active_backend->get_joint_state) {
        return 0;
    }

    return active_backend->get_joint_state(motor_0_pos, motor_2_pos);
}

int phy_get_altimeter_reading(double *altitude)
{
    if (!active_backend || !active_backend->get_altimeter_reading) {
        return 0;
    }

    return active_backend->get_altimeter_reading(altitude);
}

int phy_get_lidar_samples(rplidar_sample_t *samples, size_t num_samples)
{
    if (!active_backend || !active_backend->get_lidar_samples) {
        return 0;
    }

    return active_backend->get_lidar_samples(samples, num_samples);
}

int phy_init(int argc, char **argv) {
    // FIX: Hacky
#if ENABLE_FMU
    fmu_init(argc, argv);
    if (utils_get_arg("fmu", argc, argv) != NULL) {
        phy_select_backend("fmu");
    }
#endif

#if ENABLE_FLIGHT_CONTROLLERS
    printf("Initializing flight controllers\n");
    fc_init(argc, argv);
#endif

#if ENABLE_LIBGZ
    // virtual_gz_altimeter_init(argc, argv);
    // TODO: Add a flag to determine which physics engine
    int result = active_backend ? 1 : phy_select_backend("gazebo");
    if (result == 0) {
        fprintf(stderr, "Failed to select Gazebo backend\n");
    }
#endif

    // If no backends, use stub
    if (active_backend == NULL && !phy_select_backend("stub")) {
        fprintf(stderr, "Failed to select physics backend stub\n");
        return 0;
    }

    if (!active_backend || !active_backend->init) {
        return 0;
    }

    return active_backend->init();
}
