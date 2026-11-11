#ifndef FASTDYN_INAV_SENSORS_H
#define FASTDYN_INAV_SENSORS_H
#include <stdint.h>
#include "phy.h"
/* Encode an MPU6500 burst for the MATEKF405's CW180 mounting. */
void inav_mpu6500_encode(const imu_t *imu, phy_body_frame_t frame, uint8_t out[14]);
#endif
