#pragma once

/*
 * canonical.h — Boardrunner VIO canonical schema
 *
 * Defines the on-wire (in-shm) representation used by every sensor / actuator
 * transported through api_channel_*. Two independent concerns live here:
 *
 *   1) BrShmHeader        — layout of every shm segment we create; wire format
 *                           for the SPSC seqlock. See "Segment layout" below.
 *   2) BoardrunnerXxxSample — payload structs, always in SI units. Chip-specific
 *                           packing (LSB scaling, register byte order, temp
 *                           calibration) is NOT here — it lives in packers/*.
 *
 * Everything here is layout-frozen at BR_SHM_VERSION. Adding fields to a
 * sample struct MUST bump the version; readers refusing an unknown version
 * fall back to the model's static defaults.
 *
 * Nothing in this header depends on FastDyn internals — it is safe to include
 * from Python (via cffi) or from any other host-side writer.
 */

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* -------------------------------------------------------------------------- */
/* Wire constants                                                             */
/* -------------------------------------------------------------------------- */

/* ASCII 'BRVS' — a static-analysable magic, distinct from any random 4 bytes
 * of stale /dev/shm content. Sanity-check on every reader attach. */
#define BR_SHM_MAGIC           0x42525653u   /* 'BRVS' little-endian */
#define BR_SHM_VERSION         1u

/* URI grammar (documented, not enforced by macros — parsing lives in channel.c):
 *
 *   boardrunner://vehicle/<vid>/sensor/<kind>/<inst>
 *   boardrunner://vehicle/<vid>/actuator/<kind>/<channel>
 *
 * <kind> ∈ { imu, baro, mag, gnss, pwm }.  <vid>, <inst>, <channel> are
 * decimal integers with no leading zeros. Example:
 *   boardrunner://vehicle/0/sensor/imu/0
 *   boardrunner://vehicle/0/actuator/pwm/3
 *
 * URI → shm object name: strip "boardrunner://", replace '/' with '_',
 * prepend "/br_". So the URI above maps to:
 *   "/br_vehicle_0_sensor_imu_0"
 *   "/br_vehicle_0_actuator_pwm_3"
 */
#define BR_URI_SCHEME          "boardrunner://"
#define BR_URI_MAX_LEN         128u
#define BR_SHM_NAME_MAX        96u

/* -------------------------------------------------------------------------- */
/* Segment layout                                                             */
/* -------------------------------------------------------------------------- */

/*
 * Every shm segment starts with a BrShmHeader followed by a single payload
 * slot of `payload_size` bytes. The whole segment is rounded up to 4 KiB.
 *
 * SPSC seqlock protocol (writer bumps 0→1 while mid-write, back to 2 when
 * committed; readers retry on odd seq or mismatched before/after):
 *
 *   writer:
 *     atomic_store_release(&h->seq, h->seq | 1);   // enter update
 *     memcpy(h->slot, &sample, h->payload_size);
 *     h->last_write_ns = now();
 *     atomic_store_release(&h->seq, (h->seq | 1) + 1); // even, +2 from prior
 *
 *   reader:
 *     for (retry = 0; retry < RETRY_LIMIT; retry++) {
 *       s0 = atomic_load_acquire(&h->seq);
 *       if (s0 == 0) return NO_WRITER_EVER;      // static-default fallback
 *       if (s0 & 1) continue;                    // writer mid-write
 *       memcpy(&sample, h->slot, h->payload_size);
 *       s1 = atomic_load_acquire(&h->seq);
 *       if (s0 == s1) return OK;
 *     }
 *     return TORN_READ;                          // extremely rare
 *
 * seq==0 is the "no writer has ever published" sentinel and triggers the
 * boot-safety fallback in the model. Writers must never bump seq to an odd
 * value if the payload isn't ready — seq==1 is transient only.
 *
 * Fields are declared as plain uint64_t / uint32_t so the layout is
 * language-agnostic. The transport implementation accesses `seq` via C11
 * atomic ops (which are layout-compatible with the plain integer type on
 * every supported platform).
 */
typedef struct {
    uint32_t magic;             /* BR_SHM_MAGIC */
    uint32_t version;           /* BR_SHM_VERSION */
    uint32_t payload_size;      /* bytes of the payload slot immediately after */
    uint32_t slot_count;        /* v1: always 1. Reserved for a future ring. */
    uint32_t flags;             /* segment-wide flags; currently reserved */
    uint32_t writer_pid;        /* PID of the process that most recently wrote */
    uint64_t seq;               /* SPSC seqlock, atomic-accessed */
    uint64_t last_write_ns;     /* CLOCK_MONOTONIC ns of most recent commit */
    uint32_t reserved[6];       /* 6*4 = 24 bytes to reach exactly 64B */
} BrShmHeader;

/* Compile-time check: header is exactly 64 bytes so payload slot begins on
 * a cache line boundary. Break the build early if we ever break this. */
#ifdef __STDC_VERSION__
#if __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(BrShmHeader) == 64,
               "BrShmHeader must be exactly 64 bytes");
#endif
#endif

/* Total segment size for a given payload, rounded up to a page. Callers use
 * this to size shm_open + ftruncate. */
#define BR_SHM_PAGE_SIZE       4096u
#define BR_SHM_SEGMENT_BYTES(payload) \
    (((sizeof(BrShmHeader) + (payload)) + (BR_SHM_PAGE_SIZE - 1u)) \
     & ~(BR_SHM_PAGE_SIZE - 1u))

/* -------------------------------------------------------------------------- */
/* Sample flags — carried inside every payload struct's `flags` field         */
/* -------------------------------------------------------------------------- */

/* Writer asserts HEALTHY when the source (Gazebo sensor, fake writer, real
 * hardware) is reporting nominally. Readers may treat HEALTHY=0 as a hint
 * to prefer last-known-good over the fresh sample. */
#define BR_SAMPLE_HEALTHY      (1u << 0)

/* Writer has run its per-sensor calibration and the values are corrected
 * (bias / scale / rotation applied). Distinct from HEALTHY. */
#define BR_SAMPLE_CALIBRATED   (1u << 1)

/* Set by the transport (not the writer) when last_write_ns is older than a
 * transport-configurable staleness threshold. Advisory only. */
#define BR_SAMPLE_STALE        (1u << 2)

/* Bits 3..31 reserved for future flags. */

/* -------------------------------------------------------------------------- */
/* IMU sample — SI units, sensor frame (chip axes, no vehicle rotation)       */
/* -------------------------------------------------------------------------- */

/*
 * Sensor frame conventions:
 *   - Right-handed axes as printed on the chip datasheet (X-forward on most
 *     Invensense parts). Vehicle-frame rotation is applied downstream by
 *     ArduPilot per the AHRS_ORIENTATION parameter, not by us.
 *   - Accel is proper acceleration (specific force). At rest, level, the Z
 *     axis reads +9.81 m/s² if chip Z points up, -9.81 if it points down.
 *   - Gyro is angular rate in the sensor frame; positive by the right-hand
 *     rule about each axis.
 *   - temp_c is the die temperature the chip reports (used by ArduPilot for
 *     bias correction, so it is NOT purely diagnostic).
 *
 * seq is a monotonic sample counter set by the writer (independent of the
 * shm seqlock). timestamp_ns is CLOCK_MONOTONIC from the writer process.
 */
typedef struct {
    uint64_t seq;               /* writer-side monotonic sample counter */
    uint64_t timestamp_ns;      /* writer's CLOCK_MONOTONIC ns */
    float    ax_mps2;
    float    ay_mps2;
    float    az_mps2;
    float    gx_radps;
    float    gy_radps;
    float    gz_radps;
    float    temp_c;
    uint32_t flags;             /* BR_SAMPLE_* bits */
} BoardrunnerImuSample;

/* -------------------------------------------------------------------------- */
/* Baro sample — pressure + temp                                              */
/* -------------------------------------------------------------------------- */

typedef struct {
    uint64_t seq;
    uint64_t timestamp_ns;
    float    pressure_pa;       /* absolute static pressure, Pa */
    float    temp_c;            /* die temperature */
    uint32_t flags;
    uint32_t _pad;              /* keep 8-byte alignment for future extension */
} BoardrunnerBaroSample;

/* -------------------------------------------------------------------------- */
/* Magnetometer sample — magnetic field in Tesla, sensor frame                */
/* -------------------------------------------------------------------------- */

typedef struct {
    uint64_t seq;
    uint64_t timestamp_ns;
    float    mx_tesla;
    float    my_tesla;
    float    mz_tesla;
    float    temp_c;            /* many mag chips share a temp channel */
    uint32_t flags;
    uint32_t _pad;
} BoardrunnerMagSample;

/* -------------------------------------------------------------------------- */
/* GNSS sample — WGS84 fix + velocity + fix quality                           */
/* -------------------------------------------------------------------------- */

/* Fix types match the ArduPilot GPS_FIX_TYPE_* / MAVLink GPS_FIX_TYPE enum
 * so that ublox / mavlink packers do not have to remap. */
enum {
    BR_GNSS_FIX_NONE           = 0,
    BR_GNSS_FIX_2D             = 2,
    BR_GNSS_FIX_3D             = 3,
    BR_GNSS_FIX_DGPS           = 4,
    BR_GNSS_FIX_RTK_FLOAT      = 5,
    BR_GNSS_FIX_RTK_FIXED      = 6,
};

typedef struct {
    uint64_t seq;
    uint64_t timestamp_ns;
    double   lat_deg;           /* WGS84 */
    double   lon_deg;
    float    alt_m;             /* MSL */
    float    vel_n_mps;         /* NED velocity components */
    float    vel_e_mps;
    float    vel_d_mps;
    float    hdop;
    float    vdop;
    uint32_t flags;
    uint32_t num_sats;
    uint32_t fix_type;          /* BR_GNSS_FIX_* */
    uint32_t _pad;
} BoardrunnerGnssSample;

/* -------------------------------------------------------------------------- */
/* Actuator (PWM) sample — servo/motor command surface                        */
/* -------------------------------------------------------------------------- */

/*
 * Actuator direction is REVERSED versus sensors: firmware WRITES its servo
 * command to the shm segment (via the IOMCU model), an endpoint READS it and
 * relays into Gazebo. Semantics of api_channel_read/write are unchanged;
 * the BrRole (WRITER vs READER) is picked correctly by each side.
 *
 * We publish one segment per PWM channel so a physics endpoint can subscribe
 * to individual servos independently — no atomic-16-channel-update needed
 * (ArduPilot writes them sequentially anyway).
 */
typedef struct {
    uint64_t seq;
    uint64_t timestamp_ns;
    uint32_t channel;           /* which PWM channel this segment carries */
    uint32_t pulse_us;          /* microseconds; 900..2100 typical */
    uint32_t flags;
    uint32_t _pad;
} BoardrunnerActuatorSample;

#ifdef __cplusplus
} /* extern "C" */
#endif
