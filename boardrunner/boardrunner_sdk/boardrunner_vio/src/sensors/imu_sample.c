#include <boardrunner/imu_sample.h>

#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *device_name;
    BoardrunnerImuSampleLSB sample;
} BoardrunnerImuDefault;

static const BoardrunnerImuDefault k_imu_defaults[] = {
    /*
     * CubeBlack startup defaults, in raw sensor frame. These are chosen so the
     * configured ArduPilot rotations see a stationary 1g vector after packing.
     */
    { "mpu9250",  { 0, 0,  2048, 0, 0, 0, 300 } },
    { "icm20602", { 0, 0, -2048, 0, 0, 0, 300 } },
    { "icm20948", { 0, 0, -2048, 0, 0, 0, 300 } },
};

static void sanitize_device_name(const char *in, char *out, size_t out_len)
{
    size_t pos = 0;

    if (out_len == 0) {
        return;
    }

    if (in == NULL) {
        out[0] = '\0';
        return;
    }

    while (*in != '\0' && pos + 1 < out_len) {
        unsigned char ch = (unsigned char)*in++;

        if (isalnum(ch)) {
            out[pos++] = (char)toupper(ch);
        } else {
            out[pos++] = '_';
        }
    }

    out[pos] = '\0';
}

static bool parse_i16_env(const char *name, int16_t *out)
{
    const char *raw;
    char *end = NULL;
    long value;

    if (name == NULL || out == NULL) {
        return false;
    }

    raw = getenv(name);
    if (raw == NULL || raw[0] == '\0') {
        return false;
    }

    errno = 0;
    value = strtol(raw, &end, 0);
    if (errno != 0 || end == raw || *end != '\0') {
        return false;
    }
    if (value < INT16_MIN) {
        value = INT16_MIN;
    } else if (value > INT16_MAX) {
        value = INT16_MAX;
    }

    *out = (int16_t)value;
    return true;
}

static void apply_field_override(const char *device_key, const char *field, int16_t *value)
{
    char env_name[96];

    if (value == NULL || field == NULL) {
        return;
    }

    snprintf(env_name, sizeof(env_name), "BOARDRUNNER_IMU_%s", field);
    (void)parse_i16_env(env_name, value);

    if (device_key != NULL && device_key[0] != '\0') {
        snprintf(env_name, sizeof(env_name), "BOARDRUNNER_IMU_%s_%s", device_key, field);
        (void)parse_i16_env(env_name, value);
    }
}

static BoardrunnerImuSampleLSB default_sample_for_device(const char *device_name)
{
    size_t i;

    for (i = 0; i < sizeof(k_imu_defaults) / sizeof(k_imu_defaults[0]); i++) {
        if (device_name != NULL && strcmp(device_name, k_imu_defaults[i].device_name) == 0) {
            return k_imu_defaults[i].sample;
        }
    }

    return k_imu_defaults[0].sample;
}

void boardrunner_imu_get_sample(const char *device_name, BoardrunnerImuSampleLSB *sample)
{
    char device_key[48];

    if (sample == NULL) {
        return;
    }

    *sample = default_sample_for_device(device_name);
    sanitize_device_name(device_name, device_key, sizeof(device_key));

    apply_field_override(device_key, "AX", &sample->ax_lsb);
    apply_field_override(device_key, "AY", &sample->ay_lsb);
    apply_field_override(device_key, "AZ", &sample->az_lsb);
    apply_field_override(device_key, "GX", &sample->gx_lsb);
    apply_field_override(device_key, "GY", &sample->gy_lsb);
    apply_field_override(device_key, "GZ", &sample->gz_lsb);
    apply_field_override(device_key, "TEMP", &sample->temp_lsb);
}
