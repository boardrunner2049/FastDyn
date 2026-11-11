#include "sensors.h"
#include <math.h>
#include <string.h>

static int16_t saturate(float v)
{
    if (!isfinite(v)) return 0;
    if (v >= 32767.0f) return 32767;
    if (v <= -32768.0f) return -32768;
    return (int16_t)lroundf(v);
}

static void put_be16(uint8_t *p, int16_t v)
{
    p[0] = (uint16_t)v >> 8;
    p[1] = (uint8_t)v;
}

void inav_mpu6500_encode(const imu_t *v, phy_body_frame_t frame, uint8_t out[14])
{
    /* INAV uses +1g at rest. The MATEKF405 mounts the MPU6500 with a
     * 180-degree yaw, which INAV applies after reading the raw sensor. */
    const float ysign = frame == PHY_BODY_FRAME_FRD ? 1.0f : -1.0f;
    const float a[3] = {v->accel_body.x, ysign * v->accel_body.y,
                        -ysign * v->accel_body.z};
    const float g[3] = {-v->gyro.x, -ysign * v->gyro.y, ysign * v->gyro.z};
    memset(out, 0, 14);
    for (unsigned i = 0; i < 3; ++i) {
        put_be16(out + 2*i, saturate(a[i] * (2048.0f / 9.80665f)));
        put_be16(out + 8 + 2*i, saturate(g[i] * (16.4f * 180.0f / 3.14159265358979323846f)));
    }
}
