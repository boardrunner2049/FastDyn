#pragma once

/*
 * shm_internal.h — internal interface between channel.c and shm.c
 *
 * NOT installed in include/. The api_channel_* surface in channel.h is the
 * only external contract. This header exists so that if a second transport
 * (UDP, tcp) is added later, channel.c can dispatch to it through the same
 * shape.
 */

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include <boardrunner/canonical.h>
#include <boardrunner/channel.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct BrShmChannel BrShmChannel;

/* Open (or create) a shm segment for the given URI. Returns NULL on failure.
 * `payload_size` and `role` follow the api_channel_attach contract. */
BrShmChannel* br_shm_attach(const char* uri, BrRole role, size_t payload_size);

/* Detach and munmap. Never shm_unlink — see channel.h comment. Safe on NULL. */
void br_shm_detach(BrShmChannel* ch);

/* Wait-free seqlock read. Copies payload_size bytes on success. Returns
 * false if writer has never published (seq==0) or the retry limit expired. */
bool br_shm_read(BrShmChannel* ch, void* out);

/* Bump seqlock, memcpy payload, commit. Returns false if ch is a reader. */
bool br_shm_write(BrShmChannel* ch, const void* in);

/* Turn a URI ("boardrunner://vehicle/0/sensor/imu/0") into the shm object
 * name ("/br_vehicle_0_sensor_imu_0"). Returns false if URI is malformed
 * or the resulting name overflows the buffer. */
bool br_uri_to_shm_name(const char* uri, char* out_name, size_t out_cap);

#ifdef __cplusplus
} /* extern "C" */
#endif
