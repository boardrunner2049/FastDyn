/* Alternative FMU backend for the two mission configs. Keep the same solver,
 * sensor getters and trace writer as the standard plugin. */
#define fmu_init standard_fmu_init
#include "../../virtuals/physics/physics_engines/fmu/fmu.c"
#undef fmu_init

#include <cJSON.h>
#include <glib.h>

typedef struct {
    fmi3ValueReference *refs;
    size_t count;
    double *row;  /* FMU clock followed by flattened Float64 values */
    size_t width;
    FILE *file;
} capture_group_t;

static capture_group_t signals, parameters;
static double motor_pwm[4] = {1500, 1500, 1500, 1500};

static bool open_group(capture_group_t *group, const cJSON *items, const char *path)
{
    group->count = cJSON_GetArraySize(items);
    if (group->count == 0) return false;
    group->refs = g_new(fmi3ValueReference, group->count);
    group->width = 1;
    size_t index = 0;
    const cJSON *item, *dimension;
    cJSON_ArrayForEach(item, items) {
        const cJSON *vr = cJSON_GetObjectItemCaseSensitive(item, "vr");
        const cJSON *shape = cJSON_GetObjectItemCaseSensitive(item, "shape");
        if (!cJSON_IsNumber(vr) || !cJSON_IsArray(shape)) return false;
        group->refs[index++] = (fmi3ValueReference)vr->valuedouble;
        size_t size = 1;
        cJSON_ArrayForEach(dimension, shape) {
            if (!cJSON_IsNumber(dimension) || dimension->valueint < 1 ||
                size > SIZE_MAX / dimension->valueint) return false;
            size *= dimension->valueint;
        }
        if (group->width > SIZE_MAX - size) return false;
        group->width += size;
    }
    group->row = g_new(double, group->width);
    group->file = fopen(path, "wb");
    if (!group->file) { perror(path); return false; }
    /* auto.py reads the last complete row while QEMU is running. */
    return setvbuf(group->file, NULL, _IONBF, 0) == 0;
}

static bool sample(capture_group_t *group)
{
    group->row[0] = fmu_state.time_s;
    if (fmu_state.getFloat64(fmu_state.instance, group->refs, group->count,
                            group->row + 1, group->width - 1) != fmi3OK) {
        fprintf(stderr, "Mission capture: FMI Float64 read failed\n");
        return false;
    }
    if (fwrite(group->row, sizeof(double), group->width, group->file) != group->width) {
        perror("Mission capture write");
        return false;
    }
    return true;
}

static int record_trace(trace_t *trace)
{
    if (!fmu_get_trace(trace)) return 0;
    if (parameters.file) {
        if (!sample(&parameters)) return 0;
        int result = fclose(parameters.file);
        parameters.file = NULL;
        if (result != 0) return 0;
    }
    return sample(&signals);
}

static int set_rover_motor(int channel, int value)
{
    if (channel != 0 && channel != 2) return 1;
    motor_pwm[channel] = CLAMP(value, 900, 2200);
    double left = motor_pwm[0] - 1500.0;
    double right = motor_pwm[2] - 1500.0;
    fmu_state.pwm[0] = 1500.0 + (right - left) / 2.0;
    fmu_state.pwm[2] = 1500.0 + (left + right) / 2.0;
    fmu_state.pwm_dirty = true;
    return 1;
}

static void close_capture(void)
{
    capture_group_t *groups[] = {&signals, &parameters};
    for (size_t i = 0; i < G_N_ELEMENTS(groups); i++) {
        capture_group_t *group = groups[i];
        if (group->file) fclose(group->file);
        g_free(group->row);
        g_free(group->refs);
        *group = (capture_group_t){0};
    }
    fmu_backend_shutdown();
}

static int start_capture(void)
{
    const char *dir = getenv("FASTDYN_WORK_DIR");
    if (!dir) return 0;
    g_autofree char *layout_path = g_build_filename(dir, "layout.json", NULL);
    g_autofree char *signals_path = g_build_filename(dir, "signals.bin", NULL);
    g_autofree char *params_path = g_build_filename(dir, "parameters.bin", NULL);
    g_autofree char *trace_path = g_build_filename(dir, "trace.csv", NULL);
    g_autofree char *json = NULL;
    if (!g_file_get_contents(layout_path, &json, NULL, NULL)) {
        fprintf(stderr, "Mission capture: cannot read %s\n", layout_path);
        return 0;
    }
    cJSON *layout = cJSON_Parse(json);
    bool ready = open_group(&signals, cJSON_GetObjectItem(layout, "signals"), signals_path) &&
                 open_group(&parameters, cJSON_GetObjectItem(layout, "parameters"), params_path);
    const cJSON *vehicle = cJSON_GetObjectItem(layout, "vehicle");
    if (cJSON_IsString(vehicle) && strcmp(vehicle->valuestring, "rover") == 0) {
        for (int i = 0; i < 4; i++) fmu_state.pwm[i] = motor_pwm[i] = 1500.0;
        fmu_backend.set_servo_pwm = set_rover_motor;
    }
    cJSON_Delete(layout);
    if (!ready || !g_setenv("FASTDYN_TRACE_FILE", trace_path, TRUE)) {
        fprintf(stderr, "Mission capture: cannot initialize recording in %s\n", dir);
        close_capture();
        return 0;
    }
    return fmu_backend_init();
}

int fmu_init(int argc, char **argv)
{
    fmu_backend.init = start_capture;
    fmu_backend.get_trace = record_trace;
    fmu_backend.shutdown = close_capture;
    return standard_fmu_init(argc, argv);
}
