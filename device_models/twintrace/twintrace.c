// device_models/twintrace.c

#include <device.h>
#include <utils.h>
#include <core.h>

#include <config.h>
#include "../common/hw_session.h"

#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

// Your ConfigSection contains:
//   twintrace_mode_t twintrace_mode;
//   const char* twintrace_bin;

enum {
    TT_R = 0,
    TT_W = 1,
    TT_IRQ_TAKEN = 2,
    TT_IRQ_SERVED = 3,
};

// -----------------------
// Packed format structs
// -----------------------
typedef struct __attribute__((packed)) {
    uint8_t  magic[4];       // "TTTR"
    uint32_t version;        // 1
    uint32_t record_size;    // 40
    uint64_t count;          // number of records
} tt_hdr_t;

typedef struct __attribute__((packed)) {
    uint64_t icount;
    uint64_t pc;
    uint64_t addr;
    uint64_t value;
    uint32_t size;
    uint32_t type;           // 0=READ, 1=WRITE
} tt_rec_t;

_Static_assert(sizeof(tt_hdr_t) == 20, "tt_hdr_t must be 20 bytes");
_Static_assert(sizeof(tt_rec_t) == 40, "tt_rec_t must be 40 bytes");

// -----------------------
// Record mode: shared hw_session (see device_models/common/hw_session.h).
// Recording needs a probe, so it exists only in builds with libhw; replay
// works in every build.
// -----------------------
#if ENABLE_LIBHW
static hw_session_t *s_session;
#endif

// -----------------------
// Replay mode: mapped tape
// -----------------------
typedef struct {
    int fd;
    size_t map_len;
    uint8_t *map;

    tt_hdr_t hdr;          // copied
    const uint8_t *rec_bytes;  // points to record array (bytes)
    uint64_t idx;

    bool strict_addr;
    bool strict_size;
    bool strict_pc;
    bool strict_value_on_write;
} tt_replay_t;

static tt_replay_t g_rep;
static pthread_mutex_t rep_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t rep_cv = PTHREAD_COND_INITIALIZER;
static pthread_t rep_irq_thread;

static twintrace_mode_t g_mode = TT_OFF;
static const char *g_bin_path = NULL;

static bool twintrace_ignore_irq(int line)
{
    return line == 15;
}

// -----------------------
// Helpers
// -----------------------
static void die_sys(const char *msg, const char *path) {
    fprintf(stderr, "[twintrace] %s (%s): %s\n",
            msg, path ? path : "(null)", strerror(errno));
    utils_die("twintrace fatal");
}

static const char *tt_type_name(uint32_t type)
{
    switch (type) {
        case TT_R: return "READ";
        case TT_W: return "WRITE";
        case TT_IRQ_TAKEN: return "IRQ_TAKEN";
        case TT_IRQ_SERVED: return "IRQ_SERVED";
        default: return "UNKNOWN";
    }
}

static void tt_abort(const char *why,
                     hwaddr addr, unsigned size, uint64_t pc, uint64_t val,
                     const tt_rec_t *exp)
{
    fprintf(stderr,
        "\n[twintrace] %s\n"
        "  idx=%" PRIu64 " / %" PRIu64 "\n"
        "  got: addr=0x%08" PRIx64 " size=%u pc=0x%08" PRIx64 " val=0x%08" PRIx64 "\n"
        "  exp: %s icount=%" PRIu64 " addr=0x%08" PRIx64 " size=%u pc=0x%08" PRIx64 " val=0x%08" PRIx64 "\n\n",
        why,
        g_rep.idx, g_rep.hdr.count,
        (uint64_t)addr, size, pc, val,
        exp ? tt_type_name(exp->type) : "UNKNOWN",
        exp ? exp->icount : 0,
        exp ? exp->addr : 0,
        exp ? exp->size : 0,
        exp ? exp->pc : 0,
        exp ? exp->value : 0
    );
    utils_die("twintrace divergence");
}

static void tt_abort_irq(const char *why, int line, const tt_rec_t *exp)
{
    uint64_t icount = core_get_icount();
    fprintf(stderr,
        "\n[twintrace] %s\n"
        "  idx=%" PRIu64 " / %" PRIu64 "\n"
        "  got: irq=%d icount=%" PRIu64 "\n"
        "  exp: %s icount=%" PRIu64 " irq=%" PRIu64 "\n\n",
        why,
        g_rep.idx, g_rep.hdr.count,
        line, icount,
        exp ? tt_type_name(exp->type) : "UNKNOWN",
        exp ? exp->icount : 0,
        exp ? exp->addr : 0
    );
    utils_die("twintrace divergence");
}

static inline void tt_get_rec(uint64_t idx, tt_rec_t *out)
{
    // Avoid unaligned packed reads by memcpy
    const uint8_t *p = g_rep.rec_bytes + idx * sizeof(tt_rec_t);
    memcpy(out, p, sizeof(tt_rec_t));
}

static void tt_replay_open(const char *path)
{
    memset(&g_rep, 0, sizeof(g_rep));
    g_rep.fd = -1;

    g_rep.fd = open(path, O_RDONLY);
    if (g_rep.fd < 0) die_sys("open replay file failed", path);

    struct stat st;
    if (fstat(g_rep.fd, &st) != 0) die_sys("fstat replay file failed", path);
    if (st.st_size < (off_t)sizeof(tt_hdr_t)) utils_die("twintrace: replay file too small");

    g_rep.map_len = (size_t)st.st_size;
    g_rep.map = (uint8_t *)mmap(NULL, g_rep.map_len, PROT_READ, MAP_PRIVATE, g_rep.fd, 0);
    if (g_rep.map == MAP_FAILED) die_sys("mmap replay file failed", path);

    // Copy header out (safe even if packed)
    memcpy(&g_rep.hdr, g_rep.map, sizeof(tt_hdr_t));

    if (memcmp(g_rep.hdr.magic, "TTTR", 4) != 0) utils_die("twintrace: bad magic");
    if (g_rep.hdr.version != 1) utils_die("twintrace: unsupported version");
    if (g_rep.hdr.record_size != sizeof(tt_rec_t)) utils_die("twintrace: record_size mismatch");

    size_t need = sizeof(tt_hdr_t) + (size_t)g_rep.hdr.count * sizeof(tt_rec_t);
    if (need > g_rep.map_len) utils_die("twintrace: truncated replay file");

    g_rep.rec_bytes = g_rep.map + sizeof(tt_hdr_t);
    g_rep.idx = 0;

    // Strictness defaults (tune as you like)
    g_rep.strict_addr = true;
    g_rep.strict_size = true;
    g_rep.strict_pc = false;
    g_rep.strict_value_on_write = true;

    fprintf(stderr, "[twintrace] replay loaded: %s (count=%" PRIu64 ")\n",
            path, g_rep.hdr.count);
}

static void* replay_irq_thread_fn(void* arg)
{
    (void)arg;

    while (1) {
        pthread_mutex_lock(&rep_mutex);
        while (g_rep.idx < g_rep.hdr.count) {
            tt_rec_t e;
            tt_get_rec(g_rep.idx, &e);
            if (e.type == TT_IRQ_TAKEN) break;
            pthread_cond_wait(&rep_cv, &rep_mutex);
        }

        if (g_rep.idx >= g_rep.hdr.count) {
            pthread_mutex_unlock(&rep_mutex);
            return NULL;
        }

        tt_rec_t e;
        tt_get_rec(g_rep.idx, &e);
        uint64_t idx = g_rep.idx;
        pthread_mutex_unlock(&rep_mutex);

        while (core_get_icount() < e.icount) {
            pthread_mutex_lock(&rep_mutex);
            if (g_rep.idx != idx) {
                pthread_mutex_unlock(&rep_mutex);
                goto next_iter;
            }
            pthread_mutex_unlock(&rep_mutex);
            usleep(50);
        }

        pthread_mutex_lock(&rep_mutex);
        if (g_rep.idx != idx) {
            pthread_mutex_unlock(&rep_mutex);
            goto next_iter;
        }
        pthread_mutex_unlock(&rep_mutex);

        qemu_plugin_raise_irq((int)e.addr, false);

        pthread_mutex_lock(&rep_mutex);
        while (g_rep.idx == idx) {
            pthread_cond_wait(&rep_cv, &rep_mutex);
        }
        pthread_mutex_unlock(&rep_mutex);

next_iter:
        continue;
    }
}

static int twintrace_serve(int line)
{
#if ENABLE_LIBHW
    if (g_mode == TT_RECORD) {
        return hw_session_serve(s_session, line);
    }
#endif

    if (g_mode != TT_REPLAY) return 0;
    if (twintrace_ignore_irq(line)) return 0;

    pthread_mutex_lock(&rep_mutex);
    if (g_rep.idx >= g_rep.hdr.count) {
        pthread_mutex_unlock(&rep_mutex);
        utils_die("twintrace: ran out of records");
    }

    tt_rec_t e;
    tt_get_rec(g_rep.idx, &e);

    if (e.type != TT_IRQ_SERVED) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort_irq("expected IRQ_SERVED", line, &e);
    }
    if (e.addr != (uint64_t)line) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort_irq("IRQ_SERVED vector mismatch", line, &e);
    }

    g_rep.idx++;
    pthread_cond_broadcast(&rep_cv);
    pthread_mutex_unlock(&rep_mutex);
    return 0;
}

static int twintrace_interrupt(int line)
{
    if (g_mode != TT_REPLAY) return 0;
    if (twintrace_ignore_irq(line)) return 0;

    pthread_mutex_lock(&rep_mutex);
    if (g_rep.idx >= g_rep.hdr.count) {
        pthread_mutex_unlock(&rep_mutex);
        utils_die("twintrace: ran out of records");
    }

    tt_rec_t e;
    tt_get_rec(g_rep.idx, &e);

    if (e.type != TT_IRQ_TAKEN) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort_irq("expected IRQ_TAKEN", line, &e);
    }
    if (e.addr != (uint64_t)line) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort_irq("IRQ_TAKEN vector mismatch", line, &e);
    }

    g_rep.idx++;
    pthread_cond_broadcast(&rep_cv);
    pthread_mutex_unlock(&rep_mutex);
    return 0;
}

// -----------------------
// MMIO ops
// -----------------------
static uint64_t twintrace_read(void *opaque, hwaddr address, unsigned size, uint64_t pc)
{
    (void)opaque;

#if ENABLE_LIBHW
    if (g_mode == TT_RECORD) {
        uint64_t value = 0;
        if (hw_session_read(s_session, address, size, &value, pc) != 0) {
            utils_die("HW Read Failed");
        }
        return value;
    }
#endif

    // replay
    pthread_mutex_lock(&rep_mutex);
    if (g_rep.idx >= g_rep.hdr.count) {
        pthread_mutex_unlock(&rep_mutex);
        utils_die("twintrace: ran out of records");
    }

    tt_rec_t e;
    tt_get_rec(g_rep.idx, &e);

    if (e.type != TT_R) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("expected READ, got non-READ", address, size, pc, 0, &e);
    }
    if (g_rep.strict_addr && e.addr != (uint64_t)address) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("READ addr mismatch", address, size, pc, 0, &e);
    }
    if (g_rep.strict_size && e.size != (uint32_t)size) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("READ size mismatch", address, size, pc, 0, &e);
    }
    if (g_rep.strict_pc && e.pc != pc) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("READ pc mismatch", address, size, pc, 0, &e);
    }

    g_rep.idx++;
    pthread_cond_broadcast(&rep_cv);
    pthread_mutex_unlock(&rep_mutex);
    return e.value;
}

static void twintrace_write(void *opaque, hwaddr address, uint64_t value, unsigned size, uint64_t pc)
{
    (void)opaque;

#if ENABLE_LIBHW
    if (g_mode == TT_RECORD) {
        if (hw_session_write(s_session, address, value, size, pc) != 0) {
            utils_die("HW Write Failed");
        }
        return;
    }
#endif

    // replay
    pthread_mutex_lock(&rep_mutex);
    if (g_rep.idx >= g_rep.hdr.count) {
        pthread_mutex_unlock(&rep_mutex);
        utils_die("twintrace: ran out of records");
    }

    tt_rec_t e;
    tt_get_rec(g_rep.idx, &e);

    if (e.type != TT_W) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("expected WRITE, got non-WRITE", address, size, pc, value, &e);
    }
    if (g_rep.strict_addr && e.addr != (uint64_t)address) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("WRITE addr mismatch", address, size, pc, value, &e);
    }
    if (g_rep.strict_size && e.size != (uint32_t)size) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("WRITE size mismatch", address, size, pc, value, &e);
    }
    if (g_rep.strict_pc && e.pc != pc) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("WRITE pc mismatch", address, size, pc, value, &e);
    }
    if (g_rep.strict_value_on_write && e.value != value) {
        pthread_mutex_unlock(&rep_mutex);
        tt_abort("WRITE value mismatch", address, size, pc, value, &e);
    }

    g_rep.idx++;
    pthread_cond_broadcast(&rep_cv);
    pthread_mutex_unlock(&rep_mutex);
}

// -----------------------
// init
// -----------------------
static void* twintrace_init(ConfigSection* model_info);

DeviceModel twintrace_model_def = {
    .name      = "twintrace",
    .read      = twintrace_read,
    .write     = twintrace_write,
    .init      = twintrace_init,
    .serve     = twintrace_serve,
    .interrupt = twintrace_interrupt,
};

static void* twintrace_init(ConfigSection* model_info)
{
    // Register address ranges
    Range ranges[10];
    utils_parse_ranges(model_info->overall_range_count, model_info->overall_ranges, ranges);
    for (int i = 0; i < model_info->overall_range_count; i++) {
        dev_register_device_model(ranges[i].start, ranges[i].end, &twintrace_model_def);
    }
    for (int di = 0; di < model_info->device_count; di++) {
        DeviceModels* d = &model_info->devices[di];

        if (d->irq_count > 0 && d->irqs) {
            for (int j = 0; j < d->irq_count; j++) {
                dev_register_interrupt_device_model((int)d->irqs[j], &twintrace_model_def);
            }
        }

        //TODO: and note: break (meaning: only first device’s IRQs are registered).
        break;
    }

    g_mode = model_info->twintrace_mode;
    g_bin_path = model_info->twintrace_bin;

    fprintf(stderr, "[twintrace] mode=%d bin=%s backend=%s\n",
            (int)g_mode,
            g_bin_path ? g_bin_path : "(null)",
            model_info->backend ? model_info->backend : "(null)");

    if (g_mode == TT_RECORD) {
#if ENABLE_LIBHW
        // behave like passthrough; share the probe via hw_session
        s_session = hw_session_acquire(model_info->backend);
        if (!s_session) utils_die("HW connection failed");
#else
        utils_die("twintrace: recording needs a probe; rebuild FastDyn with libhw "
                  "(make LIBHW=true, or meson -Denable_libhw=true). Replay works in this build.");
#endif
    } else if (g_mode == TT_REPLAY) {
        if (!g_bin_path || !g_bin_path[0]) utils_die("twintrace_bin missing for replay");
        tt_replay_open(g_bin_path);
        if (pthread_create(&rep_irq_thread, NULL, replay_irq_thread_fn, NULL) != 0) {
            perror("Failed to create replay IRQ thread");
            return NULL;
        }
    } else {
        utils_die("twintrace: TT_OFF but model selected");
    }

    return NULL;
}
