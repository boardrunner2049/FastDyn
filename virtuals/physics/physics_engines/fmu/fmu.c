#include "fmu.h"

#include <dlfcn.h>
#include <errno.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef uint32_t fmi3ValueReference;
typedef double fmi3Float64;
typedef bool fmi3Boolean;
typedef const char *fmi3String;
typedef void *fmi3Instance;
typedef void *fmi3InstanceEnvironment;

typedef enum {
    fmi3OK,
    fmi3Warning,
    fmi3Discard,
    fmi3Error,
    fmi3Fatal
} fmi3Status;

typedef void (*fmi3LogMessageCallback)(
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3Status status,
    fmi3String category,
    fmi3String message);

typedef fmi3Instance (*fmi3InstantiateCoSimulation_ft)(
    fmi3String instanceName,
    fmi3String instantiationToken,
    fmi3String resourcePath,
    fmi3Boolean visible,
    fmi3Boolean loggingOn,
    fmi3Boolean eventModeUsed,
    fmi3Boolean earlyReturnAllowed,
    const fmi3ValueReference requiredIntermediateVariables[],
    size_t nRequiredIntermediateVariables,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage,
    void *intermediateUpdate);

typedef void (*fmi3FreeInstance_ft)(fmi3Instance instance);
typedef fmi3Status (*fmi3EnterInitializationMode_ft)(
    fmi3Instance instance,
    fmi3Boolean toleranceDefined,
    fmi3Float64 tolerance,
    fmi3Float64 startTime,
    fmi3Boolean stopTimeDefined,
    fmi3Float64 stopTime);
typedef fmi3Status (*fmi3ExitInitializationMode_ft)(fmi3Instance instance);
typedef fmi3Status (*fmi3DoStep_ft)(
    fmi3Instance instance,
    fmi3Float64 currentCommunicationPoint,
    fmi3Float64 communicationStepSize,
    fmi3Boolean noSetFMUStatePriorToCurrentPoint,
    fmi3Boolean *eventHandlingNeeded,
    fmi3Boolean *terminateSimulation,
    fmi3Boolean *earlyReturn,
    fmi3Float64 *lastSuccessfulTime);
typedef fmi3Status (*fmi3GetFloat64_ft)(
    fmi3Instance instance,
    const fmi3ValueReference valueReferences[],
    size_t nValueReferences,
    fmi3Float64 values[],
    size_t nValues);
typedef fmi3Status (*fmi3SetFloat64_ft)(
    fmi3Instance instance,
    const fmi3ValueReference valueReferences[],
    size_t nValueReferences,
    const fmi3Float64 values[],
    size_t nValues);
typedef fmi3Status (*fmi3Terminate_ft)(fmi3Instance instance);

typedef struct {
    fmi3ValueReference pwm;
    fmi3ValueReference gps;
    fmi3ValueReference yaw_deg;
    fmi3ValueReference gyro;
    fmi3ValueReference mag;
    fmi3ValueReference accel;
    fmi3ValueReference vel_ned;
    fmi3ValueReference baro_altitude_m;
    fmi3ValueReference baro_pressure_pa;
    fmi3ValueReference baro_temperature_c;
    fmi3ValueReference baro_climb_rate_mps;
    fmi3ValueReference lat0;
    fmi3ValueReference lon0;
    fmi3ValueReference ground_alt_wgs84;
    fmi3ValueReference earth_radius_m;
    fmi3ValueReference truth_position;
    fmi3ValueReference truth_velocity;
    fmi3ValueReference truth_quaternion;
    fmi3ValueReference truth_rate;
} fmu_value_refs_t;

typedef struct {
    int argc;
    char **argv;
    void *handle;
    fmi3Instance instance;
    fmi3InstantiateCoSimulation_ft instantiateCoSimulation;
    fmi3FreeInstance_ft freeInstance;
    fmi3EnterInitializationMode_ft enterInitializationMode;
    fmi3ExitInitializationMode_ft exitInitializationMode;
    fmi3DoStep_ft doStep;
    fmi3GetFloat64_ft getFloat64;
    fmi3SetFloat64_ft setFloat64;
    fmi3Terminate_ft terminate;
    double time_s;
    double pwm[4];
    bool pwm_dirty;
    bool initialized;
    bool has_truth;
    fmu_value_refs_t vr;
} fmu_state_t;

static fmu_state_t fmu_state = {
    .argc = 0,
    .argv = NULL,
    .handle = NULL,
    .instance = NULL,
    .time_s = 0.0,
    .pwm = {1000.0, 1000.0, 1000.0, 1000.0},
    .pwm_dirty = true,
    .initialized = false,
    .vr = {
        .pwm = 61,
        .gps = 72,
        .yaw_deg = 73,
        .gyro = 76,
        .mag = 77,
        .accel = 78,
        .vel_ned = 85,
        .baro_altitude_m = 0,
        .baro_pressure_pa = 0,
        .baro_temperature_c = 0,
        .baro_climb_rate_mps = 0,
        .lat0 = 128,
        .lon0 = 129,
        .ground_alt_wgs84 = 130,
        .earth_radius_m = 0,
    },
};

typedef struct {
    bool checked;
    bool enabled;
    unsigned long long advance_calls;
    unsigned long long do_steps;
    double advance_wall_s;
    double do_step_wall_s;
    double sim_advanced_s;
    double init_wall_s;
    double last_report_wall_s;
} fmu_profile_t;

static fmu_profile_t fmu_profile = {0};

static bool env_flag_enabled(const char *name)
{
    const char *raw = getenv(name);
    return raw != NULL &&
           raw[0] != '\0' &&
           strcmp(raw, "0") != 0 &&
           strcmp(raw, "false") != 0 &&
           strcmp(raw, "off") != 0 &&
           strcmp(raw, "no") != 0;
}

static double wall_time_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1.0e9;
}

static bool fmu_profile_enabled(void)
{
    if (!fmu_profile.checked) {
        fmu_profile.enabled = env_flag_enabled("FASTDYN_FMU_PROFILE");
        fmu_profile.checked = true;
    }
    return fmu_profile.enabled;
}

static void fmu_profile_report(bool force)
{
    if (!fmu_profile_enabled()) {
        return;
    }

    double now = wall_time_s();
    if (!force && fmu_profile.last_report_wall_s > 0.0 &&
        now - fmu_profile.last_report_wall_s < 5.0) {
        return;
    }

    double avg_do_step_us = fmu_profile.do_steps > 0
        ? fmu_profile.do_step_wall_s * 1.0e6 / (double)fmu_profile.do_steps
        : 0.0;
    double realtime_factor = fmu_profile.advance_wall_s > 0.0
        ? fmu_profile.sim_advanced_s / fmu_profile.advance_wall_s
        : 0.0;
    double wall_since_init_s = fmu_profile.init_wall_s > 0.0 ? now - fmu_profile.init_wall_s : 0.0;
    double sim_wall_rtf = wall_since_init_s > 0.0 ? fmu_state.time_s / wall_since_init_s : 0.0;

    printf("FMU timing: sim=%.3fs wall_since_init=%.3fs sim_wall_rtf=%.2fx advanced=%.3fs wall_advance=%.3fs fmu_compute_rtf=%.2fx advance_calls=%llu do_steps=%llu avg_do_step=%.1fus\n",
           fmu_state.time_s,
           wall_since_init_s,
           sim_wall_rtf,
           fmu_profile.sim_advanced_s,
           fmu_profile.advance_wall_s,
           realtime_factor,
           fmu_profile.advance_calls,
           fmu_profile.do_steps,
           avg_do_step_us);
    fflush(stdout);
    fmu_profile.last_report_wall_s = now;
}

static void fmu_log_message(
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3Status status,
    fmi3String category,
    fmi3String message)
{
    (void)instanceEnvironment;
    if (status >= fmi3Warning) {
        fprintf(stderr, "FMU[%s]: %s\n", category ? category : "log", message ? message : "");
    }
}

static int load_symbol(void **out, const char *name)
{
    *out = dlsym(fmu_state.handle, name);
    if (*out == NULL) {
        fprintf(stderr, "FMU missing symbol %s: %s\n", name, dlerror());
        return 0;
    }
    return 1;
}

static const char *get_exact_arg(const char *key)
{
    size_t len = strlen(key);
    for (int i = 0; i < fmu_state.argc; i++) {
        if (strncmp(fmu_state.argv[i], key, len) == 0 && fmu_state.argv[i][len] == '=') {
            return fmu_state.argv[i] + len + 1;
        }
    }
    return NULL;
}

static int set_pwm_inputs(void)
{
    const fmi3ValueReference vr[] = {fmu_state.vr.pwm};
    fmi3Status status = fmu_state.setFloat64(fmu_state.instance, vr, 1, fmu_state.pwm, 4);
    if (status != fmi3OK) {
        fprintf(stderr, "FMU fmi3SetFloat64(pwm) failed with status %d\n", (int)status);
        return 0;
    }
    fmu_state.pwm_dirty = false;
    return 1;
}

static int get_fmu_vr_arg(const char *name, fmi3ValueReference *value)
{
    char key[256];
    int written = snprintf(key, sizeof(key), "fmu_vr_%s", name);
    if (written <= 0 || (size_t)written >= sizeof(key)) {
        fprintf(stderr, "FMU value reference name too long: %s\n", name);
        return -1;
    }

    const char *raw = get_exact_arg(key);
    if (raw == NULL || raw[0] == '\0') {
        return 0;
    }

    errno = 0;
    char *end = NULL;
    unsigned long parsed = strtoul(raw, &end, 10);
    if (errno != 0 || end == raw || *end != '\0') {
        fprintf(stderr, "FMU value reference %s has invalid value '%s'\n", name, raw);
        return -1;
    }
    *value = (fmi3ValueReference)parsed;
    return 1;
}

static int configure_value_references(void)
{
    struct {
        const char *name;
        fmi3ValueReference *vr;
    } entries[] = {
        {"pwm", &fmu_state.vr.pwm},
        {"gps", &fmu_state.vr.gps},
        {"yaw_deg", &fmu_state.vr.yaw_deg},
        {"gyro", &fmu_state.vr.gyro},
        {"mag", &fmu_state.vr.mag},
        {"accel", &fmu_state.vr.accel},
        {"vel_ned", &fmu_state.vr.vel_ned},
        {"baro_altitude_m", &fmu_state.vr.baro_altitude_m},
        {"baro_pressure_pa", &fmu_state.vr.baro_pressure_pa},
        {"baro_temperature_c", &fmu_state.vr.baro_temperature_c},
        {"baro_climb_rate_mps", &fmu_state.vr.baro_climb_rate_mps},
        {"lat0", &fmu_state.vr.lat0},
        {"lon0", &fmu_state.vr.lon0},
        {"ground_alt_wgs84", &fmu_state.vr.ground_alt_wgs84},
        {"earth_radius_m", &fmu_state.vr.earth_radius_m},
    };

    for (size_t i = 0; i < sizeof(entries) / sizeof(entries[0]); i++) {
        int status = get_fmu_vr_arg(entries[i].name, entries[i].vr);
        if (status < 0) {
            return 0;
        }
        if (status > 0 && env_flag_enabled("FASTDYN_DEBUG_FMU_VR")) {
            printf("FMU value reference override: %s=%u\n", entries[i].name, *entries[i].vr);
        }
    }
    struct {
        const char *name;
        fmi3ValueReference *vr;
    } diagnostics[] = {
        {"truth.p_w", &fmu_state.vr.truth_position},
        {"truth.v_w", &fmu_state.vr.truth_velocity},
        {"truth.q_wb", &fmu_state.vr.truth_quaternion},
        {"truth.omega_b", &fmu_state.vr.truth_rate},
    };
    fmu_state.has_truth = true;
    for (size_t i = 0; i < sizeof(diagnostics) / sizeof(diagnostics[0]); i++) {
        int status = get_fmu_vr_arg(diagnostics[i].name, diagnostics[i].vr);
        if (status < 0) {
            return 0;
        }
        fmu_state.has_truth = fmu_state.has_truth && status > 0;
    }
    return 1;
}

static int set_configured_parameters(void)
{
    const char prefix[] = "fmu_param_";
    const size_t prefix_len = sizeof(prefix) - 1;
    size_t count = 0;

    for (int i = 0; i < fmu_state.argc; i++) {
        const char *arg = fmu_state.argv[i];
        if (strncmp(arg, prefix, prefix_len) != 0) {
            continue;
        }
        const char *equals = strchr(arg + prefix_len, '=');
        if (equals == NULL || equals == arg + prefix_len) {
            continue;
        }
        count++;
    }

    if (count == 0) {
        return 1;
    }

    fmi3ValueReference *refs = calloc(count, sizeof(*refs));
    fmi3Float64 *values = calloc(count, sizeof(*values));
    if (refs == NULL || values == NULL) {
        fprintf(stderr, "FMU failed to allocate parameter override buffers\n");
        free(refs);
        free(values);
        return 0;
    }

    size_t out = 0;
    for (int i = 0; i < fmu_state.argc; i++) {
        const char *arg = fmu_state.argv[i];
        if (strncmp(arg, prefix, prefix_len) != 0) {
            continue;
        }
        const char *name_start = arg + prefix_len;
        const char *equals = strchr(name_start, '=');
        if (equals == NULL || equals == name_start) {
            continue;
        }

        size_t name_len = (size_t)(equals - name_start);
        if (name_len >= 256) {
            fprintf(stderr, "FMU parameter name too long: %.*s\n", (int)name_len, name_start);
            free(refs);
            free(values);
            return 0;
        }

        char name[256];
        memcpy(name, name_start, name_len);
        name[name_len] = '\0';

        errno = 0;
        char *end = NULL;
        double value = strtod(equals + 1, &end);
        if (errno != 0 || end == equals + 1 || *end != '\0' || !isfinite(value)) {
            fprintf(stderr, "FMU parameter %s has invalid numeric value '%s'\n", name, equals + 1);
            free(refs);
            free(values);
            return 0;
        }

        fmi3ValueReference vr = 0;
        int status = get_fmu_vr_arg(name, &vr);
        if (status < 0) {
            free(refs);
            free(values);
            return 0;
        }
        if (status == 0) {
            fprintf(stderr, "FMU parameter %s was configured but no fmu_vr_%s value reference was provided\n",
                    name, name);
            free(refs);
            free(values);
            return 0;
        }

        refs[out] = vr;
        values[out] = value;
        out++;
        printf("FMU parameter override: %s=%g\n", name, value);
    }

    fmi3Status status = fmu_state.setFloat64(fmu_state.instance, refs, count, values, count);
    free(refs);
    free(values);
    if (status != fmi3OK) {
        fprintf(stderr, "FMU fmi3SetFloat64(parameter overrides) failed with status %d\n", (int)status);
        return 0;
    }
    return 1;
}

static int get_values(fmi3ValueReference vr, double *values, size_t n_values)
{
    if (!fmu_state.initialized) {
        return 0;
    }
    fmi3Status status = fmu_state.getFloat64(fmu_state.instance, &vr, 1, values, n_values);
    if (status != fmi3OK) {
        fprintf(stderr, "FMU fmi3GetFloat64(vr=%u) failed with status %d\n", vr, (int)status);
        return 0;
    }
    return 1;
}

static void fmu_backend_shutdown(void);

static int fmu_backend_init(void)
{
    double init_start = fmu_profile_enabled() ? wall_time_s() : 0.0;
    // The Python launcher resolves all metadata from the same FMU package.
    const char *so_path = get_exact_arg("fmu");
    const char *fmu_name = get_exact_arg("fmu_name");
    const char *token = get_exact_arg("fmu_token");
    const char *resources = get_exact_arg("fmu_resources");
    if (so_path == NULL || so_path[0] == '\0') {
        fprintf(stderr, "FMU backend enabled but plugin argument fmu= is missing\n");
        return 0;
    }
    if (!configure_value_references()) {
        return 0;
    }

    if (token == NULL || token[0] == '\0') {
        fprintf(stderr, "FMU backend requires fmu_token= from modelDescription.xml\n");
        return 0;
    }

    fmu_state.handle = dlopen(so_path, RTLD_NOW | RTLD_LOCAL);
    if (fmu_state.handle == NULL) {
        fprintf(stderr, "FMU dlopen failed for %s: %s\n", so_path, dlerror());
        return 0;
    }

    if (!load_symbol((void **)&fmu_state.instantiateCoSimulation, "fmi3InstantiateCoSimulation") ||
        !load_symbol((void **)&fmu_state.freeInstance, "fmi3FreeInstance") ||
        !load_symbol((void **)&fmu_state.enterInitializationMode, "fmi3EnterInitializationMode") ||
        !load_symbol((void **)&fmu_state.exitInitializationMode, "fmi3ExitInitializationMode") ||
        !load_symbol((void **)&fmu_state.doStep, "fmi3DoStep") ||
        !load_symbol((void **)&fmu_state.getFloat64, "fmi3GetFloat64") ||
        !load_symbol((void **)&fmu_state.setFloat64, "fmi3SetFloat64")) {
        goto fail;
    }
    fmu_state.terminate = (fmi3Terminate_ft)dlsym(fmu_state.handle, "fmi3Terminate");

    fmu_state.instance = fmu_state.instantiateCoSimulation(
        fmu_name ? fmu_name : "fastdyn",
        token,
        resources && resources[0] ? resources : NULL,
        0,
        0,
        0,
        0,
        NULL,
        0,
        NULL,
        fmu_log_message,
        NULL);
    if (fmu_state.instance == NULL) {
        fprintf(stderr, "FMU fmi3InstantiateCoSimulation failed for %s with token %s\n", so_path, token);
        goto fail;
    }

    if (fmu_state.enterInitializationMode(fmu_state.instance, 0, 0.0, 0.0, 0, 0.0) != fmi3OK ||
        !set_configured_parameters() ||
        !set_pwm_inputs() ||
        fmu_state.exitInitializationMode(fmu_state.instance) != fmi3OK) {
        fprintf(stderr, "FMU initialization failed for %s\n", so_path);
        goto fail;
    }

    fmu_state.time_s = 0.0;
    fmu_state.initialized = true;
    printf("FMU backend loaded: %s\n", so_path);
    if (fmu_profile_enabled()) {
        double now = wall_time_s();
        fmu_profile.init_wall_s = now;
        printf("FMU timing: init_wall=%.3fs\n", now - init_start);
        fflush(stdout);
        fmu_profile.last_report_wall_s = now;
    }
    return 1;

fail:
    fmu_backend_shutdown();
    return 0;
}

static void fmu_backend_shutdown(void)
{
    if (fmu_state.instance != NULL) {
        if (fmu_state.initialized && fmu_state.terminate != NULL) {
            fmu_state.terminate(fmu_state.instance);
        }
        fmu_state.freeInstance(fmu_state.instance);
        fmu_state.instance = NULL;
    }
    if (fmu_state.handle != NULL) {
        dlclose(fmu_state.handle);
        fmu_state.handle = NULL;
    }
    fmu_state.initialized = false;
}

static int fmu_get_imu_batch(imu_batch_t *imu_batch)
{
    double accel[3] = {0.0, 0.0, -9.80665};
    double gyro[3] = {0.0, 0.0, 0.0};
    if (!get_values(fmu_state.vr.accel, accel, 3) || !get_values(fmu_state.vr.gyro, gyro, 3)) {
        return 0;
    }

    for (int i = 0; i < 17; i++) {
        imu_batch->imu[i].accel_body.x = (float)accel[0];
        imu_batch->imu[i].accel_body.y = (float)accel[1];
        imu_batch->imu[i].accel_body.z = (float)accel[2];
        imu_batch->imu[i].gyro.x = (float)gyro[0];
        imu_batch->imu[i].gyro.y = (float)gyro[1];
        imu_batch->imu[i].gyro.z = (float)gyro[2];
    }
    return 1;
}

static int fmu_get_mag_reading(vector3d_t *mag)
{
    double values[3] = {0.0, 0.0, 0.0};
    if (!get_values(fmu_state.vr.mag, values, 3)) {
        return 0;
    }
    mag->x = values[0];
    mag->y = values[1];
    mag->z = values[2];
    return 1;
}

static int fmu_get_navsat_reading(gps_data_t *gps_data)
{
    double gps[3] = {40.414929, -86.932387, 0.0};
    double vel[3] = {0.0, 0.0, 0.0};
    double yaw_deg = 0.0;
    if (!get_values(fmu_state.vr.gps, gps, 3) ||
        !get_values(fmu_state.vr.vel_ned, vel, 3) ||
        !get_values(fmu_state.vr.yaw_deg, &yaw_deg, 1)) {
        return 0;
    }

    gps_data->lat = gps[0];
    gps_data->lon = gps[1];
    gps_data->alt = gps[2];
    gps_data->vel_n = vel[0];
    gps_data->vel_e = vel[1];
    gps_data->vel_d = vel[2];
    gps_data->sec = (uint64_t)floor(fmu_state.time_s);
    gps_data->nsec = (uint32_t)((fmu_state.time_s - floor(fmu_state.time_s)) * 1.0e9);
    gps_data->yaw_deg = (float)yaw_deg;
    return 1;
}

static int fmu_set_servo_pwm(int channel, int pwm)
{
    if (channel < 0 || channel >= 4) {
        return 1;
    }
    if (pwm < 900) {
        pwm = 900;
    } else if (pwm > 2200) {
        pwm = 2200;
    }
    static int last_pwm[4] = {0};
    if (last_pwm[channel] == 0 || abs(pwm - last_pwm[channel]) >= 25) {
        if (env_flag_enabled("FASTDYN_DEBUG_PWM")) {
            printf("FMU PWM input: channel=%d pwm=%d\n", channel, pwm);
            fflush(stdout);
        }
        last_pwm[channel] = pwm;
    }
    fmu_state.pwm[channel] = (double)pwm;
    fmu_state.pwm_dirty = true;
    return 1;
}

static int fmu_advance_simulation(double run_until_time)
{
    if (!fmu_state.initialized) {
        return 0;
    }
    if (run_until_time <= fmu_state.time_s) {
        return 1;
    }

    bool prof = fmu_profile_enabled();
    double advance_start_wall = prof ? wall_time_s() : 0.0;
    double advance_start_sim = fmu_state.time_s;

    while (fmu_state.time_s + 1.0e-9 < run_until_time) {
        double h = run_until_time - fmu_state.time_s;
        if (h > 0.002) {
            h = 0.002;
        }

        fmi3Boolean event_needed = 0;
        fmi3Boolean terminate = 0;
        fmi3Boolean early_return = 0;
        fmi3Float64 last_successful = fmu_state.time_s;
        double step_start_wall = prof ? wall_time_s() : 0.0;
        if (fmu_state.pwm_dirty && !set_pwm_inputs()) {
            return 0;
        }
        fmi3Status status = fmu_state.doStep(
            fmu_state.instance,
            fmu_state.time_s,
            h,
            1,
            &event_needed,
            &terminate,
            &early_return,
            &last_successful);
        if (prof) {
            fmu_profile.do_step_wall_s += wall_time_s() - step_start_wall;
            fmu_profile.do_steps++;
        }
        if (status != fmi3OK || terminate) {
            fprintf(stderr, "FMU fmi3DoStep failed at t=%.6f h=%.6f status=%d terminate=%d\n",
                    fmu_state.time_s, h, (int)status, (int)terminate);
            return 0;
        }
        fmu_state.time_s = last_successful > fmu_state.time_s ? last_successful : fmu_state.time_s + h;
        if (event_needed || early_return) {
            break;
        }
    }
    if (prof) {
        fmu_profile.advance_calls++;
        fmu_profile.advance_wall_s += wall_time_s() - advance_start_wall;
        fmu_profile.sim_advanced_s += fmu_state.time_s - advance_start_sim;
        fmu_profile_report(false);
    }
    return 1;
}

static int fmu_get_altimeter_reading(double *altitude)
{
    double baro_altitude_m = 0.0;
    if (!get_values(fmu_state.vr.baro_altitude_m, &baro_altitude_m, 1)) {
        return 0;
    }
    *altitude = baro_altitude_m;
    return 1;
}

static int fmu_get_barometer_reading(barometer_t *barometer)
{
    return get_values(fmu_state.vr.baro_pressure_pa, &barometer->pressure_pa, 1) &&
           get_values(fmu_state.vr.baro_temperature_c, &barometer->temperature_c, 1);
}

static int fmu_get_lidar_samples(rplidar_sample_t *samples, size_t num_samples)
{
    (void)samples;
    (void)num_samples;
    return 0;
}

int fmu_init(int argc, char **argv)
{
    fmu_state.argc = argc;
    fmu_state.argv = argv;
    return 0;
}

phy_backend_t fmu_backend = {
    .name = "fmu",
    .imu_frame = PHY_BODY_FRAME_FRD,
    .init = fmu_backend_init,
    .shutdown = fmu_backend_shutdown,
    .get_imu_batch = fmu_get_imu_batch,
    .get_mag_reading = fmu_get_mag_reading,
    .get_navsat_reading = fmu_get_navsat_reading,
    .set_servo_pwm = fmu_set_servo_pwm,
    .advance_simulation = fmu_advance_simulation,
    .get_altimeter_reading = fmu_get_altimeter_reading,
    .get_barometer_reading = fmu_get_barometer_reading,
    .get_lidar_samples = fmu_get_lidar_samples,
};
