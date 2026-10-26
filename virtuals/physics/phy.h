#ifndef PHY_H
#define PHY_H

#include <stdint.h>
#include <stddef.h>

typedef struct gps_data_t {
    double lat;
    double lon;
    double alt;
    double vel_n;
    double vel_e;
    double vel_d;
    uint64_t sec;
    uint32_t nsec;
    float yaw_deg;
} gps_data_t;

// 3D float vector
typedef struct {
    float x;
    float y;
    float z;
} vector3f_t;

// 3D double vector
typedef struct {
    double x;
    double y;
    double z;
} vector3d_t;

// Quaternion
typedef struct {
    double w;
    double x;
    double y;
    double z;
} quaternion_t;

// IMU
typedef struct {
    vector3f_t gyro;
    vector3f_t accel_body;
} imu_t;

// IMU batch
// Contains 17 IMU readings
typedef struct {
    imu_t imu[17];
} imu_batch_t;

typedef enum {
    PHY_BODY_FRAME_FLU = 0,
    PHY_BODY_FRAME_FRD = 1,
} phy_body_frame_t;

// Wind vane apparent
typedef struct {
    float direction;
    float speed;
} wind_vane_apparent_t;

typedef struct {
    uint8_t sync_quality;   // S, !S, quality
    uint8_t angle_lsb;      // angle Q6 low byte
    uint8_t angle_msb;      // angle Q6 high byte
    uint8_t dist_lsb;       // distance Q2 low byte
    uint8_t dist_msb;       // distance Q2 high byte
} rplidar_sample_t;

typedef struct {
    double pressure_pa;
    double temperature_c;
} barometer_t;

/* Backend interface */
typedef struct phy_backend {

    const char *name;
    phy_body_frame_t imu_frame;

    int  (*init)(void);
    void (*shutdown)(void);

    int (*get_imu_batch)(imu_batch_t *imu_batch);

    int (*get_mag_reading)(vector3d_t *mag);

    int (*get_navsat_reading)(gps_data_t *gps_data);

    int (*set_servo_pwm)(int channel, int pwm);

    int (*advance_simulation)(double run_until_time);

    int (*get_joint_state)(double *motor_0_pos, double *motor_2_pos);

    int (*get_altimeter_reading)(double *altitude);

    int (*get_lidar_samples)(rplidar_sample_t *samples, size_t num_samples);

    int (*get_barometer_reading)(barometer_t *barometer);

} phy_backend_t;

typedef struct phy_backend_entry {
    const char *name;
    phy_backend_t *backend;
} phy_backend_entry_t;

/**
 * @brief Select the active physics backend by name.
 *
 * Known names include "gazebo" (and future engines like "casadi").
 * Returns 1 on success, 0 if the name is undefined.
 */
int phy_select_backend(const char *name);
int phy_backend_is(const char *name);
phy_body_frame_t phy_get_imu_frame(void);

/**
 * @brief Generic API for physics backends
 *
 * All functions return 1 on success and 0 on failure.
 * Failure can occur when there is no backend registered
 * or if the backend does not implement the function.
 */
int phy_init(int argc, char ** argv);
// TODO: Allow user to specify the size of the IMU batch and implement buffering in the backend
// Could be a pointer to an array of imu_batch_t structs, along with a size parameter
int phy_get_imu_batch(imu_batch_t *batch);
int phy_get_mag_reading(vector3d_t *mag);
int phy_get_navsat_reading(gps_data_t *gps_data);
int phy_set_servo_pwm(int channel, int pwm);
int phy_advance_simulation(double run_until_time);
int phy_get_joint_state(double *motor_0_pos, double *motor_2_pos);
int phy_get_altimeter_reading(double *altitude);
int phy_get_barometer_reading(barometer_t *barometer);
int phy_get_lidar_samples(rplidar_sample_t *samples, size_t num_samples);

#endif /* PHY_H */
