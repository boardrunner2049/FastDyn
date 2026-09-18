#include <dlfcn.h>
#include <utils.h>
#include <core.h>
#include <common.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <strings.h>
#include "cov_bbl.h"

// fuzzer needs to restart fastdyn with persistent work directory
#define DEFAULT_BBL_DUMP_PATH "fastdyn_work/bbl.txt"
#define DEFAULT_EDGE_DUMP_PATH "fastdyn_work/edges.txt"
static const char *bbl_dump_path = DEFAULT_BBL_DUMP_PATH;
static const char *edge_dump_path = DEFAULT_EDGE_DUMP_PATH;
static GHashTable *bbl_set;
static GHashTable *bbl_size;  /* pc -> instruction count, populated at TB translation time */
static GHashTable *edge_set;  /* src pc -> set(dst pc), populated at runtime when enabled */
static int fd = -1;
static bool edge_coverage_enabled = false;
static guint last_bbl_pc = 0;
static bool have_last_bbl_pc = false;

typedef struct {
    guint bbl_pc;
    guint timestamp;
} bbl_entry_t;

#define BBL_QUEUE_MAX 1024
static bbl_entry_t bbl_queue[BBL_QUEUE_MAX];
static int bbl_queue_len = 0;

static const char *resolve_bbl_dump_path(void) {
    const char *path = getenv("FASTDYN_BBL_FILE");
    if (path && path[0]) {
        return path;
    }

    const char *work_dir = getenv("FASTDYN_WORK_DIR");
    if (work_dir && work_dir[0]) {
        static char buffer[4096];
        snprintf(buffer, sizeof(buffer), "%s/bbl.txt", work_dir);
        return buffer;
    }

    return DEFAULT_BBL_DUMP_PATH;
}

static const char *resolve_edge_dump_path(void) {
    const char *path = getenv("FASTDYN_EDGE_FILE");
    if (path && path[0]) {
        return path;
    }

    const char *slash = strrchr(bbl_dump_path, '/');
    if (slash != NULL) {
        static char buffer[4096];
        size_t dir_len = (size_t)(slash - bbl_dump_path);

        if (dir_len + sizeof("/edges.txt") > sizeof(buffer)) {
            utils_die("Edge dump path too long");
        }

        memcpy(buffer, bbl_dump_path, dir_len);
        buffer[dir_len] = '\0';
        snprintf(buffer + dir_len, sizeof(buffer) - dir_len, "/edges.txt");
        return buffer;
    }

    return "edges.txt";
}

static bool cov_bbl_arg_enabled(const char *value) {
    if (value == NULL || value[0] == '\0') {
        return false;
    }

    if (strcasecmp(value, "1") == 0 ||
        strcasecmp(value, "true") == 0 ||
        strcasecmp(value, "yes") == 0 ||
        strcasecmp(value, "on") == 0) {
        return true;
    }

    return false;
}

static void fuzz_bbl_add_edge(guint src_pc, guint dst_pc) {
    GHashTable *destinations = g_hash_table_lookup(edge_set, GUINT_TO_POINTER(src_pc));

    if (destinations == NULL) {
        destinations = g_hash_table_new(g_direct_hash, g_direct_equal);
        if (destinations == NULL) {
            utils_die("Failed to allocate edge destination set");
        }

        g_hash_table_insert(edge_set, GUINT_TO_POINTER(src_pc), destinations);
    }

    g_hash_table_insert(
        destinations,
        GUINT_TO_POINTER(dst_pc),
        GUINT_TO_POINTER(1)
    );
}

typedef struct {
    FILE *file;
    bool failed;
} edge_dump_ctx_t;

static void fuzz_bbl_dump_edge_row(gpointer key, gpointer value, gpointer user_data) {
    edge_dump_ctx_t *ctx = (edge_dump_ctx_t *)user_data;
    GHashTable *destinations = (GHashTable *)value;
    GHashTableIter iter;
    gpointer dst_key;
    gpointer ignored;
    guint src_pc = GPOINTER_TO_UINT(key);

    if (ctx->failed) {
        return;
    }

    g_hash_table_iter_init(&iter, destinations);
    while (g_hash_table_iter_next(&iter, &dst_key, &ignored)) {
        if (fprintf(ctx->file, "%08x\t%08x\n", src_pc, GPOINTER_TO_UINT(dst_key)) < 0) {
            ctx->failed = true;
            return;
        }
    }
}

static void fuzz_dump_edges(void) {
    if (!edge_coverage_enabled || edge_set == NULL || edge_dump_path == NULL) {
        return;
    }

    char tmp[4608];
    if (snprintf(tmp, sizeof(tmp), "%s.tmp", edge_dump_path) >= (int)sizeof(tmp)) {
        utils_die("Edge dump temp path too long");
    }

    FILE *ff = fopen(tmp, "w");
    if (ff == NULL) {
        utils_die("Failed to open edge dump file for writing");
    }

    edge_dump_ctx_t ctx = {
        .file = ff,
        .failed = false,
    };
    g_hash_table_foreach(edge_set, fuzz_bbl_dump_edge_row, &ctx);

    bool failed = ctx.failed;
    if (!failed && fflush(ff) != 0) {
        failed = true;
    }
    if (!failed && fsync(fileno(ff)) != 0) {
        failed = true;
    }
    if (fclose(ff) != 0) {
        failed = true;
    }

    if (failed) {
        remove(tmp);
        utils_die("Failed to write edge coverage");
    }

    if (rename(tmp, edge_dump_path) != 0) {
        remove(tmp);
        utils_die("Failed to publish edge coverage");
    }
}

void fuzz_bbl_observe(uint32_t pc, uint32_t size) {
    guint bbl_pc = pc & (~1ULL);
    g_hash_table_insert(bbl_size, GUINT_TO_POINTER(bbl_pc), GUINT_TO_POINTER((guint)size));
}

void fuzz_dump_bbl() {
    if (fd >= 0) {
        static char buf[BBL_QUEUE_MAX * 64];
        int idx = 0;

        for (int i = 0; i < bbl_queue_len; i++) {
            guint insn_count = GPOINTER_TO_UINT(g_hash_table_lookup(bbl_size, GUINT_TO_POINTER(bbl_queue[i].bbl_pc)));
            idx += snprintf(buf + idx, sizeof(buf) - idx, "%08x\t%u\t%u\n", bbl_queue[i].bbl_pc, insn_count, bbl_queue[i].timestamp);
        }

        int written = 0;
        while (written < idx) {
            ssize_t n = write(fd, buf, idx);
            if (n <= 0) {
                utils_die("Failed to write coverage\n");
            }
            written += n;
        }

        bbl_queue_len = 0;

        fuzz_dump_edges();
    } else {
        utils_die("Tried outputting coverage without initialized file\n");
    }
}

void fuzz_bbl_add(uint32_t pc, int irq_depth) {
    guint bbl_pc = pc & (~1ULL);

    if (edge_coverage_enabled) {
        if (irq_depth != 0) {
            last_bbl_pc = 0;
            have_last_bbl_pc = false;
        } else {
            if (have_last_bbl_pc) {
                fuzz_bbl_add_edge(last_bbl_pc, bbl_pc);
            }

            last_bbl_pc = bbl_pc;
            have_last_bbl_pc = true;
        }
    }

    /* Basic-block coverage is execution coverage: retain ISR blocks too.
     * Edge accounting above deliberately keeps interrupt transitions separate. */
    if (!g_hash_table_lookup(bbl_set, GUINT_TO_POINTER(bbl_pc))) {
        guint timestamp = (guint)(g_get_monotonic_time() / 1000);

        g_hash_table_insert(
            bbl_set,
            GUINT_TO_POINTER(bbl_pc),
            GUINT_TO_POINTER(timestamp)
        );

        bbl_queue[bbl_queue_len++] = (bbl_entry_t){bbl_pc, timestamp};
        if (bbl_queue_len >= BBL_QUEUE_MAX) {
            fuzz_dump_bbl();
        }
    }
}

void fuzz_bbl_reset_trace(void) {
    have_last_bbl_pc = false;
    last_bbl_pc = 0;
}

void fuzz_bbl_init(int argc, char **argv)
{
    int total = 0;
    bbl_dump_path = resolve_bbl_dump_path();
    edge_coverage_enabled = cov_bbl_arg_enabled(utils_get_arg("edge_coverage", argc, argv));
    edge_dump_path = resolve_edge_dump_path();
    fuzz_bbl_reset_trace();

    bbl_set = g_hash_table_new(g_direct_hash, g_direct_equal);
    if (!bbl_set) {
        utils_die("Failed to allocate bbl_set");
    }

    bbl_size = g_hash_table_new(g_direct_hash, g_direct_equal);
    if (!bbl_size) {
        utils_die("Failed to allocate bbl_size");
    }

    if (edge_coverage_enabled) {
        /*
         * This is runtime-only state. bbl.txt stores novel basic blocks, not
         * the full execution adjacency needed to faithfully reconstruct edges
         * across previous runs.
         */
        edge_set = g_hash_table_new_full(
            g_direct_hash,
            g_direct_equal,
            NULL,
            (GDestroyNotify)g_hash_table_destroy
        );
        if (!edge_set) {
            utils_die("Failed to allocate edge_set");
        }
    } else {
        edge_set = NULL;
    }

    FILE *ff = fopen(bbl_dump_path, "r");
    if (!ff) {
        printf("Allocating new bbl_set\n");
    } else {
        guint pc;
        guint insn_count;
        guint timestamp;

        /* Read lines like: 08001234<TAB>4<TAB>1573 */
        while (fscanf(ff, "%x\t%u\t%u", &pc, &insn_count, &timestamp) == 3) {
            total++;

            g_hash_table_insert(bbl_set,  GUINT_TO_POINTER(pc), GUINT_TO_POINTER(timestamp));
            g_hash_table_insert(bbl_size, GUINT_TO_POINTER(pc), GUINT_TO_POINTER(insn_count));
        }

        fclose(ff);
    }

    if (edge_coverage_enabled) {
        ff = fopen(edge_dump_path, "r");
        if (ff != NULL) {
            guint src_pc;
            guint dst_pc;

            while (fscanf(ff, "%x\t%x", &src_pc, &dst_pc) == 2) {
                fuzz_bbl_add_edge(src_pc, dst_pc);
            }

            fclose(ff);
        }
    }

    fd = open(bbl_dump_path, O_CREAT | O_WRONLY | O_APPEND, 0666);
    if (fd < 0) {
        utils_die("Failed to open dump file for writing");
    }
}
