#pragma once

/*
 * channel.h — Boardrunner VIO generic channel API
 *
 * One transport-agnostic surface for every sensor and actuator carried between
 * FastDyn peripheral models and host-side endpoint scripts. Currently backed
 * exclusively by POSIX shm (see boardrunner_vio/src/transport/shm.c). The
 * design admits a second transport later (UDP for cross-host, tcp for
 * containers) without any signature change.
 *
 * Usage from a peripheral model (reader side, hot path):
 *
 *   static BrChannel *g_ch;
 *
 *   void slave_spi_set_cs(int level) {
 *       if (level == 0 && g_ch == NULL) {
 *           g_ch = api_channel_attach_from_config(
 *               model_info, "data_source",
 *               BR_ROLE_READER, sizeof(BoardrunnerImuSample));
 *       }
 *       ...
 *   }
 *
 *   static void refresh_sample(State *s) {
 *       BoardrunnerImuSample si;
 *       if (!BR_IMU_READ(g_ch, &si)) {
 *           // No writer yet, or config had no data_source: keep today's
 *           // static-default behaviour so boot is bit-exact.
 *           boardrunner_imu_get_default_si("icm20602", &si);
 *       }
 *       invensense_pack_be16(&si, &s->scale, INVENSENSE_LAYOUT_ATG, out_regs);
 *   }
 *
 * Usage from a host-side endpoint (writer side, off the hot path):
 *
 *   BrChannel *ch = api_channel_attach(
 *       "boardrunner://vehicle/0/sensor/imu/0",
 *       BR_ROLE_WRITER, sizeof(BoardrunnerImuSample));
 *   for (;;) {
 *       BoardrunnerImuSample s = gz_imu_to_si(next_gz_message());
 *       BR_IMU_WRITE(ch, &s);
 *   }
 *
 * All functions are thread-safe with respect to each other but a single
 * BrChannel* is not safe to share between threads on the same side (each
 * reader/writer thread should attach its own).
 */

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include <device.h>                 /* ConfigSection */
#include <boardrunner/canonical.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque handle. Internal layout lives in transport/shm.c. */
typedef struct BrChannel BrChannel;

typedef enum {
    BR_ROLE_READER = 0,             /* attaches for reads; will not mutate seq */
    BR_ROLE_WRITER = 1,             /* attaches for writes; owns seq updates */
} BrRole;

/*
 * Attach to a channel by URI. Creates the shm segment if it does not exist
 * yet — either role may be the one to create it, since our boot-safety story
 * requires the model (reader) to attach before the endpoint (writer) is
 * guaranteed to be up.
 *
 * `payload_size` must match on both sides. If a reader attaches with a size
 * that disagrees with an existing header's payload_size, attach returns NULL
 * (version / schema mismatch — the reader should fall back to static
 * defaults and log a diagnostic).
 *
 * Returns NULL on failure. Never blocks.
 */
BrChannel *api_channel_attach(const char *uri,
                              BrRole role,
                              size_t payload_size);

/*
 * Attach by looking up a TOML key on the model's ConfigSection. Typical use
 * inside a slave model:
 *
 *   [[Device.spi4.connections]]
 *   name          = "icm20602_spi.c"
 *   data_source   = "boardrunner://vehicle/0/sensor/imu/0"
 *
 *   api_channel_attach_from_config(cfg, "data_source",
 *                                  BR_ROLE_READER, sizeof(BoardrunnerImuSample));
 *
 * Returns NULL if the key is absent, empty, or fails schema check. Callers
 * MUST handle NULL by falling back to their static-default sample source so
 * today's bit-exact boot behaviour is preserved when TOML has no data_source.
 */
BrChannel *api_channel_attach_from_config(ConfigSection *cfg,
                                          const char *key,
                                          BrRole role,
                                          size_t payload_size);

/*
 * Read the latest committed sample. Copies `n` bytes (which MUST equal the
 * payload_size passed at attach) into `out`.
 *
 * Returns true if a live sample was copied. Returns false if:
 *   - `ch` is NULL (caller passed a failed attach), or
 *   - the writer has never published (seq == 0), or
 *   - the seqlock exceeded the retry limit (torn read — extremely rare).
 *
 * On false, `out` is untouched. Callers should treat all false returns
 * identically for boot safety: use the static-default sample.
 *
 * Wait-free, ~50 ns hot path (single memcpy plus two atomic loads).
 */
bool api_channel_read(BrChannel *ch, void *out, size_t n);

/*
 * Publish a new sample. `n` must equal the payload_size passed at attach.
 * Bumps the seqlock. Timestamps `last_write_ns` on the segment header.
 *
 * Returns true on success, false if `ch` is NULL or the role is READER.
 */
bool api_channel_write(BrChannel *ch, const void *in, size_t n);

/*
 * Detach from the channel. Does NOT shm_unlink the segment — endpoint and
 * model may attach and detach many times over the segment's life, and the
 * segment persists in /dev/shm until an explicit tool cleans it up (or the
 * host reboots). This is intentional: the model MUST NOT delete an segment
 * out from under a still-attached writer.
 *
 * Safe to call with ch == NULL.
 */
void api_channel_detach(BrChannel *ch);

/* -------------------------------------------------------------------------- */
/* Typed convenience macros — enforce payload size at the call site           */
/* -------------------------------------------------------------------------- */

#define BR_IMU_READ(ch, sample_ptr) \
    api_channel_read((ch), (sample_ptr), sizeof(BoardrunnerImuSample))
#define BR_IMU_WRITE(ch, sample_ptr) \
    api_channel_write((ch), (sample_ptr), sizeof(BoardrunnerImuSample))

#define BR_BARO_READ(ch, sample_ptr) \
    api_channel_read((ch), (sample_ptr), sizeof(BoardrunnerBaroSample))
#define BR_BARO_WRITE(ch, sample_ptr) \
    api_channel_write((ch), (sample_ptr), sizeof(BoardrunnerBaroSample))

#define BR_MAG_READ(ch, sample_ptr) \
    api_channel_read((ch), (sample_ptr), sizeof(BoardrunnerMagSample))
#define BR_MAG_WRITE(ch, sample_ptr) \
    api_channel_write((ch), (sample_ptr), sizeof(BoardrunnerMagSample))

#define BR_GNSS_READ(ch, sample_ptr) \
    api_channel_read((ch), (sample_ptr), sizeof(BoardrunnerGnssSample))
#define BR_GNSS_WRITE(ch, sample_ptr) \
    api_channel_write((ch), (sample_ptr), sizeof(BoardrunnerGnssSample))

#define BR_ACTUATOR_READ(ch, sample_ptr) \
    api_channel_read((ch), (sample_ptr), sizeof(BoardrunnerActuatorSample))
#define BR_ACTUATOR_WRITE(ch, sample_ptr) \
    api_channel_write((ch), (sample_ptr), sizeof(BoardrunnerActuatorSample))

#ifdef __cplusplus
} /* extern "C" */
#endif
