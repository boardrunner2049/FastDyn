/*
 * transport/shm.c — POSIX shm backing for api_channel_*
 *
 * SPSC seqlock, one payload slot per segment, wait-free on the reader.
 *
 * Segment lifecycle:
 *   - First attacher (either role) creates the shm object (O_CREAT).
 *   - Zero-filled by POSIX, so magic == 0 means "not yet initialised".
 *   - Whoever wins a CAS on magic performs header initialisation and stores
 *     BR_SHM_MAGIC atomically last. Losers spin until magic == BR_SHM_MAGIC.
 *   - Segments persist in /dev/shm until an explicit tool cleans them up —
 *     the model must not shm_unlink an active segment out from under a peer.
 *
 * Threading: a single BrShmChannel* is intended to be used by exactly one
 * thread on its side (reader OR writer). The seqlock guarantees safety
 * across processes; not across concurrent writers to the same segment.
 */

#include "shm_internal.h"

#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* -------------------------------------------------------------------------- */

#define BR_SEQLOCK_READ_RETRIES  32u

/* Sentinel magic used during header CAS-init to indicate "another attacher
 * is currently initialising"; distinct from both 0 (uninitialised) and
 * BR_SHM_MAGIC (ready). */
#define BR_SHM_MAGIC_INITIALISING  0x494E4954u  /* 'INIT' */

struct BrShmChannel {
    void*         base;          /* mmap base */
    size_t        map_size;      /* whole mapping (header + slot + padding) */
    size_t        payload_size;  /* copy size for read/write */
    BrRole        role;
    char          shm_name[BR_SHM_NAME_MAX];
    BrShmHeader*  header;        /* == base */
    uint8_t*      slot;          /* == base + sizeof(BrShmHeader) */
};

/* -------------------------------------------------------------------------- */
/* URI parsing                                                                */
/* -------------------------------------------------------------------------- */

bool br_uri_to_shm_name(const char* uri, char* out_name, size_t out_cap)
{
    if (uri == NULL || out_name == NULL || out_cap < 4u) {
        return false;
    }

    size_t scheme_len = strlen(BR_URI_SCHEME);
    if (strncmp(uri, BR_URI_SCHEME, scheme_len) != 0) {
        return false;
    }

    const char* rest = uri + scheme_len;
    if (*rest == '\0') {
        return false;
    }

    /* "/br_" prefix then the URI tail with '/' → '_'. Reject any non-ASCII
     * or shell-unfriendly characters. POSIX requires the name start with '/'
     * and portable names limit to "/[A-Za-z0-9_.-]+". */
    if (out_cap < 5u) {
        return false;
    }
    out_name[0] = '/';
    out_name[1] = 'b';
    out_name[2] = 'r';
    out_name[3] = '_';

    size_t oi = 4u;
    for (const char* p = rest; *p != '\0'; p++) {
        if (oi + 1u >= out_cap) {
            return false;
        }
        char c = *p;
        if (c == '/') {
            out_name[oi++] = '_';
        } else if ((c >= 'a' && c <= 'z') ||
                   (c >= 'A' && c <= 'Z') ||
                   (c >= '0' && c <= '9') ||
                   c == '_' || c == '-' || c == '.') {
            out_name[oi++] = c;
        } else {
            return false;
        }
    }
    out_name[oi] = '\0';
    return oi > 4u;  /* need at least one URI-tail character */
}

/* -------------------------------------------------------------------------- */
/* Header CAS-init                                                            */
/* -------------------------------------------------------------------------- */

/* Atomically transition magic 0 → BR_SHM_MAGIC_INITIALISING. If we win, we
 * initialise the header and store BR_SHM_MAGIC. If we lose, spin briefly
 * until magic == BR_SHM_MAGIC. Bounded by a small retry budget so a dead
 * initialising peer doesn't wedge us forever — after the budget expires we
 * fall through and read what's there (which for a truly dead peer will still
 * be MAGIC_INITIALISING, and the reader path will treat seq==0 as "no
 * writer" → falls back to static defaults). */
static bool shm_ensure_header_ready(BrShmChannel* ch, size_t payload_size)
{
    _Atomic uint32_t* magic_p = (_Atomic uint32_t*)&ch->header->magic;

    for (int spin = 0; spin < 1000; spin++) {
        uint32_t observed = atomic_load_explicit(magic_p, memory_order_acquire);

        if (observed == BR_SHM_MAGIC) {
            /* Ready. Sanity-check the payload size on this side. */
            if (ch->header->payload_size != payload_size) {
                fprintf(stderr,
                        "boardrunner: shm '%s' payload_size mismatch "
                        "(header=%u, attach=%zu)\n",
                        ch->shm_name,
                        (unsigned)ch->header->payload_size,
                        payload_size);
                return false;
            }
            if (ch->header->version != BR_SHM_VERSION) {
                fprintf(stderr,
                        "boardrunner: shm '%s' version mismatch "
                        "(header=%u, expected=%u)\n",
                        ch->shm_name,
                        (unsigned)ch->header->version,
                        (unsigned)BR_SHM_VERSION);
                return false;
            }
            return true;
        }

        if (observed == 0u) {
            uint32_t expected = 0u;
            if (atomic_compare_exchange_strong_explicit(
                    magic_p, &expected, BR_SHM_MAGIC_INITIALISING,
                    memory_order_acq_rel, memory_order_acquire)) {
                /* We won: initialise the header, then commit magic last. */
                ch->header->version       = BR_SHM_VERSION;
                ch->header->payload_size  = (uint32_t)payload_size;
                ch->header->slot_count    = 1u;
                ch->header->flags         = 0u;
                ch->header->writer_pid    = 0u;
                ch->header->seq           = 0u;  /* sentinel: no writer yet */
                ch->header->last_write_ns = 0u;
                memset(ch->header->reserved, 0, sizeof(ch->header->reserved));

                atomic_store_explicit(magic_p, BR_SHM_MAGIC,
                                      memory_order_release);
                return true;
            }
            /* Someone else won; loop and observe again. */
            continue;
        }

        /* observed == BR_SHM_MAGIC_INITIALISING or some other value; spin. */
        struct timespec ts = { .tv_sec = 0, .tv_nsec = 100000 }; /* 100µs */
        nanosleep(&ts, NULL);
    }

    fprintf(stderr,
            "boardrunner: shm '%s' header never became ready (magic=0x%08x)\n",
            ch->shm_name,
            atomic_load_explicit(magic_p, memory_order_relaxed));
    return false;
}

/* -------------------------------------------------------------------------- */
/* Attach / detach                                                            */
/* -------------------------------------------------------------------------- */

BrShmChannel* br_shm_attach(const char* uri, BrRole role, size_t payload_size)
{
    if (payload_size == 0u || payload_size > (BR_SHM_PAGE_SIZE - sizeof(BrShmHeader))) {
        fprintf(stderr, "boardrunner: bad payload_size %zu\n", payload_size);
        return NULL;
    }

    char shm_name[BR_SHM_NAME_MAX];
    if (!br_uri_to_shm_name(uri, shm_name, sizeof(shm_name))) {
        fprintf(stderr, "boardrunner: malformed URI '%s'\n", uri ? uri : "(null)");
        return NULL;
    }

    size_t map_size = BR_SHM_SEGMENT_BYTES(payload_size);

    /* Open with O_CREAT so first attacher on either side creates it.
     * Mode 0600 keeps segments per-user; endpoints and models share the
     * same UID so this is sufficient and avoids leaking to other users. */
    int fd = shm_open(shm_name, O_RDWR | O_CREAT, 0600);
    if (fd < 0) {
        fprintf(stderr, "boardrunner: shm_open('%s') failed: %s\n",
                shm_name, strerror(errno));
        return NULL;
    }

    /* Ensure segment is at least map_size bytes. ftruncate to a size that
     * already matches is a cheap no-op; racing attaches both call it and
     * both end up with the correct size. */
    struct stat st;
    if (fstat(fd, &st) != 0) {
        fprintf(stderr, "boardrunner: fstat('%s') failed: %s\n",
                shm_name, strerror(errno));
        close(fd);
        return NULL;
    }
    if ((size_t)st.st_size < map_size) {
        if (ftruncate(fd, (off_t)map_size) != 0) {
            fprintf(stderr, "boardrunner: ftruncate('%s', %zu) failed: %s\n",
                    shm_name, map_size, strerror(errno));
            close(fd);
            return NULL;
        }
    }

    void* base = mmap(NULL, map_size, PROT_READ | PROT_WRITE,
                      MAP_SHARED, fd, 0);
    /* fd may be closed after mmap; the mapping keeps the segment referenced. */
    close(fd);
    if (base == MAP_FAILED) {
        fprintf(stderr, "boardrunner: mmap('%s', %zu) failed: %s\n",
                shm_name, map_size, strerror(errno));
        return NULL;
    }

    BrShmChannel* ch = calloc(1, sizeof(*ch));
    if (ch == NULL) {
        munmap(base, map_size);
        return NULL;
    }
    ch->base         = base;
    ch->map_size     = map_size;
    ch->payload_size = payload_size;
    ch->role         = role;
    ch->header       = (BrShmHeader*)base;
    ch->slot         = (uint8_t*)base + sizeof(BrShmHeader);
    strncpy(ch->shm_name, shm_name, sizeof(ch->shm_name) - 1u);

    if (!shm_ensure_header_ready(ch, payload_size)) {
        munmap(base, map_size);
        free(ch);
        return NULL;
    }

    return ch;
}

void br_shm_detach(BrShmChannel* ch)
{
    if (ch == NULL) {
        return;
    }
    if (ch->base != NULL && ch->base != MAP_FAILED) {
        (void)munmap(ch->base, ch->map_size);
    }
    free(ch);
}

/* -------------------------------------------------------------------------- */
/* Seqlock read / write                                                       */
/* -------------------------------------------------------------------------- */

static uint64_t monotonic_ns(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0u;
    }
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

bool br_shm_read(BrShmChannel* ch, void* out)
{
    if (ch == NULL || out == NULL) {
        return false;
    }

    _Atomic uint64_t* seq_p = (_Atomic uint64_t*)&ch->header->seq;

    for (unsigned retry = 0; retry < BR_SEQLOCK_READ_RETRIES; retry++) {
        uint64_t s0 = atomic_load_explicit(seq_p, memory_order_acquire);

        /* seq == 0: no writer has ever committed. Boot-safety fallback path. */
        if (s0 == 0u) {
            return false;
        }
        /* Odd seq: writer is mid-write. Spin (cheap) and retry. */
        if ((s0 & 1u) != 0u) {
            continue;
        }

        /* Copy payload optimistically. */
        memcpy(out, ch->slot, ch->payload_size);

        /* Barrier: ensure the payload read happens before we re-observe seq. */
        atomic_thread_fence(memory_order_acquire);
        uint64_t s1 = atomic_load_explicit(seq_p, memory_order_acquire);
        if (s0 == s1) {
            return true;
        }
        /* Writer committed between our two seq reads — retry. */
    }
    return false;  /* torn read after retries; caller falls back to defaults */
}

bool br_shm_write(BrShmChannel* ch, const void* in)
{
    if (ch == NULL || in == NULL) {
        return false;
    }
    if (ch->role != BR_ROLE_WRITER) {
        return false;
    }

    _Atomic uint64_t* seq_p = (_Atomic uint64_t*)&ch->header->seq;

    /* Enter update: bump to next odd. On first-ever write (seq==0), this
     * transitions 0→1 which the reader recognises as "writer mid-first-write"
     * — the reader retries; there is no OK path where the reader observes an
     * odd seq. */
    uint64_t s = atomic_load_explicit(seq_p, memory_order_relaxed);
    uint64_t next_odd = (s & 1u) ? s : (s + 1u);
    atomic_store_explicit(seq_p, next_odd, memory_order_release);

    /* Payload copy. */
    memcpy(ch->slot, in, ch->payload_size);
    ch->header->last_write_ns = monotonic_ns();
    if (ch->header->writer_pid == 0u) {
        ch->header->writer_pid = (uint32_t)getpid();
    }

    /* Commit: bump to next even. */
    atomic_store_explicit(seq_p, next_odd + 1u, memory_order_release);
    return true;
}
