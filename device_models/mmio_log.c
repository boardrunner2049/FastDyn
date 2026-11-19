/* Keep file writes off the vCPU thread and preserve the trace's text format. */
#include "mmio_log.h"

#include <glib.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#define FD_LOG_SUBSYSTEM "dev"
#include <fastdyn/log.h>
#include <utils.h>

#define BATCH_BYTES (64 * 1024)
#define FLUSH_INTERVAL_US (100 * 1000)

static FILE *log_file;
static GThread *writer;
static GString *pending;
static GMutex lock;
static GCond ready;
static GCond space;
static bool stopping;

static gpointer write_log(gpointer data)
{
    (void)data;
    GString *batch = g_string_sized_new(BATCH_BYTES);
    bool failed = false;

    g_mutex_lock(&lock);
    for (;;) {
        while (!stopping && pending->len == 0) {
            g_cond_wait(&ready, &lock);
        }

        /* Let the batch fill while keeping low-volume logs visible promptly. */
        gint64 deadline = g_get_monotonic_time() + FLUSH_INTERVAL_US;
        while (!stopping && pending->len < BATCH_BYTES &&
               g_cond_wait_until(&ready, &lock, deadline)) {
        }
        if (pending->len == 0) {
            break;
        }

        GString *empty = batch;
        batch = pending;
        pending = empty;
        g_cond_broadcast(&space);
        g_mutex_unlock(&lock);

        failed |= fwrite(batch->str, 1, batch->len, log_file) != batch->len;
        g_string_truncate(batch, 0);

        g_mutex_lock(&lock);
    }
    g_mutex_unlock(&lock);

    failed |= fclose(log_file) != 0;
    if (failed) {
        FD_LOG_ERROR("could not write the complete MMIO log");
    }
    g_string_free(batch, TRUE);
    return NULL;
}

void dev_log_start(void)
{
    char path[PATH_MAX];
    const char *work_dir = getenv("FASTDYN_WORK_DIR");
    if (snprintf(path, sizeof(path), "%s/io.log", work_dir && *work_dir ? work_dir : ".")
        >= (int)sizeof(path)) {
        utils_die("FASTDYN_WORK_DIR is too long for the MMIO log path");
    }
    log_file = fopen(path, "w");
    if (!log_file) {
        utils_die("cannot open the MMIO log");
    }
    setvbuf(log_file, NULL, _IONBF, 0);

    if (atexit(dev_log_stop) != 0) {
        utils_die("cannot register MMIO log shutdown");
    }
    pending = g_string_sized_new(BATCH_BYTES);
    writer = g_thread_new("fastdyn-mmio-log", write_log, NULL);
}

void dev_log_stop(void)
{
    g_mutex_lock(&lock);
    if (!writer || stopping) {
        g_mutex_unlock(&lock);
        return;
    }
    stopping = true;
    g_cond_signal(&ready);
    g_cond_broadcast(&space);
    g_mutex_unlock(&lock);

    g_thread_join(writer);
    g_string_free(pending, TRUE);
}

void dev_log_printf(const char *format, ...)
{
    char line[512];
    char *record = line;
    va_list args;
    va_start(args, format);
    int length = vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    if (length < 0) {
        FD_LOG_ERROR("could not format an MMIO log record");
        return;
    }
    if ((size_t)length >= sizeof(line)) {
        va_start(args, format);
        record = g_strdup_vprintf(format, args);
        va_end(args);
    }

    g_mutex_lock(&lock);
    /* Keep full logs when the writer falls behind, with bounded queued data. */
    while (!stopping && pending->len >= BATCH_BYTES) {
        g_cond_wait(&space, &lock);
    }
    if (!stopping) {
        bool was_empty = pending->len == 0;
        g_string_append_len(pending, record, length);
        if (was_empty || pending->len >= BATCH_BYTES) {
            g_cond_signal(&ready);
        }
    }
    g_mutex_unlock(&lock);
    if (record != line) {
        g_free(record);
    }
}
