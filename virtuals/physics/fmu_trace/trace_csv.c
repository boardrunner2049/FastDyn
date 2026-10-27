#include "trace_csv.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#define TRACE_CSV_ENV "FASTDYN_TRACE_FILE"

static FILE *robustness_trace_csv = NULL;
static bool robustness_trace_csv_init_attempted = false;
static bool robustness_trace_csv_disabled = false;

static int init_robustness_trace_csv(void)
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

    // header declaration (uses same names as STL formulas)
    //if (fprintf(robustness_trace_csv,
    //            "sim_time_ms,gps_lat,gps_lon,gps_alt,gps_vel_n,gps_vel_e,gps_vel_d,"
    //            "gps_sec,gps_nsec,gps_yaw_deg") < 0) {
    //    goto write_error;
    //}
    if (fprintf(robustness_trace_csv,
                "sim_time,lat,lon,z,gps_vel_n,gps_vel_e,gps_vel_d,"
                "gps_sec,gps_nsec,gps_yaw_deg") < 0) {
        goto write_error;
    }
    if (fprintf(robustness_trace_csv,
                // translational accelerations
                ",x_accel,y_accel,z_accel,"
                // angular velocities
                "roll_rate,pitch_rate,yaw_rate") < 0) { // role rate is rate of rotating about x axis
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
    if (trace == NULL || !init_robustness_trace_csv()) {
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
