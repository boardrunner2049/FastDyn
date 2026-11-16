#include "trace_csv.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#define TRACE_CSV_ENV "FASTDYN_TRACE_FILE"

static FILE *robustness_trace_csv = NULL;
static bool robustness_trace_csv_init_attempted = false;
static bool robustness_trace_csv_disabled = false;

static int init_robustness_trace_csv(const trace_t *trace)
{
    if (robustness_trace_csv_init_attempted) {
        return robustness_trace_csv != NULL || robustness_trace_csv_disabled;
    }
    robustness_trace_csv_init_attempted = true;

    const char *path = getenv(TRACE_CSV_ENV);
    if (path == NULL || path[0] == '\0') {
        robustness_trace_csv_disabled = true;
        return 1;
    }

    robustness_trace_csv = fopen(path, "w");
    if (robustness_trace_csv == NULL) {
        perror("Failed to open robustness trace CSV");
        return 0;
    }

    if (fprintf(robustness_trace_csv,
                "sim_time,lat,lon,z,gps_vel_n,gps_vel_e,gps_vel_d,"
                "gps_sec,gps_nsec,gps_yaw_deg,x_accel,y_accel,z_accel,"
                "roll_rate,pitch_rate,yaw_rate") < 0) {
        goto write_error;
    }
    if (trace->has_kinematics && fputs(",roll_rad,pitch_rad,yaw_rad,"
                                "omega_frd_x,omega_frd_y,omega_frd_z,"
                                "alpha_frd_x,alpha_frd_y,alpha_frd_z,"
                                "pos_n_m,pos_e_m,pos_d_m,"
                                "accel_n_mps2,accel_e_mps2,accel_d_mps2",
                                robustness_trace_csv) == EOF) {
        goto write_error;
    }
    if (trace->has_vehicle_outputs &&
        fputs(",vel_frd_x,vel_frd_y,vel_frd_z,accel_frd_x,accel_frd_y,accel_frd_z,"
              "rotation_11,rotation_12,rotation_13,rotation_21,rotation_22,rotation_23,"
              "rotation_31,rotation_32,rotation_33,mag_x,mag_y,mag_z,"
              "baro_altitude_m,baro_pressure_pa,baro_temperature_c,baro_climb_rate_mps,"
              "motor_cmd_1,motor_cmd_2,motor_cmd_3,motor_cmd_4", robustness_trace_csv) == EOF) {
        goto write_error;
    }
    if (fputc('\n', robustness_trace_csv) == EOF ||
        fflush(robustness_trace_csv) != 0) {
        goto write_error;
    }
    return 1;

write_error:
    perror("Failed to write robustness trace CSV header");
    fclose(robustness_trace_csv);
    robustness_trace_csv = NULL;
    return 0;
}

int write_robustness_trace_csv(double sim_time_ms, const trace_t *trace)
{
    if (trace == NULL || !init_robustness_trace_csv(trace)) {
        return 0;
    }
    if (robustness_trace_csv_disabled) {
        return 1;
    }

    const gps_data_t *gps = &trace->gps;
    if (fprintf(robustness_trace_csv,
                "%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,"
                "%" PRIu64 ",%" PRIu32 ",%.9g",
                sim_time_ms, gps->lat, gps->lon, gps->alt,
                gps->vel_n, gps->vel_e, gps->vel_d, gps->sec, gps->nsec,
                (double)gps->yaw_deg) < 0) {
        goto write_error;
    }

    const imu_t *imu = &trace->imu_batch.imu[0];
    if (fprintf(robustness_trace_csv, ",%.9g,%.9g,%.9g,%.9g,%.9g,%.9g",
                (double)imu->accel_body.x, (double)imu->accel_body.y,
                (double)imu->accel_body.z, (double)imu->gyro.x,
                (double)imu->gyro.y, (double)imu->gyro.z) < 0) {
        goto write_error;
    }
    if (trace->has_kinematics) {
        const double *vectors[] = {trace->rpy_rad, trace->omega_frd, trace->alpha_frd,
                                   trace->pos_ned, trace->accel_ned};
        for (size_t i = 0; i < sizeof(vectors) / sizeof(vectors[0]); i++) {
            const double *v = vectors[i];
            if (fprintf(robustness_trace_csv, ",%.17g,%.17g,%.17g", v[0], v[1], v[2]) < 0) {
                goto write_error;
            }
        }
    }
    if (trace->has_vehicle_outputs) {
        for (size_t i = 0; i < sizeof(trace->vehicle_outputs) / sizeof(trace->vehicle_outputs[0]); i++) {
            if (fprintf(robustness_trace_csv, ",%.17g", trace->vehicle_outputs[i]) < 0) {
                goto write_error;
            }
        }
    }
    if (fputc('\n', robustness_trace_csv) == EOF ||
        fflush(robustness_trace_csv) != 0) {
        goto write_error;
    }
    return 1;

write_error:
    perror("Failed to write robustness trace CSV row");
    fclose(robustness_trace_csv);
    robustness_trace_csv = NULL;
    robustness_trace_csv_init_attempted = true;
    return 0;
}
