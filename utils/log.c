/* Levelled logging for FastDyn's native code (see include/fastdyn/log.h). */
#include <fastdyn/log.h>

#include <stdatomic.h>
#include <stdio.h>

static atomic_int current_level = FD_LOG_LEVEL_INFO;

void fastdyn_log_set_level(int level) {
    atomic_store(&current_level, level);
}

int fastdyn_log_get_level(void) {
    return atomic_load(&current_level);
}

static const char *level_name(int level) {
    if (level >= FD_LOG_LEVEL_ERROR) {
        return "ERROR";
    }
    if (level >= FD_LOG_LEVEL_WARNING) {
        return "WARNING";
    }
    if (level >= FD_LOG_LEVEL_INFO) {
        return "INFO";
    }
    if (level >= FD_LOG_LEVEL_DEBUG) {
        return "DEBUG";
    }
    return "TRACE";
}

void fastdyn_logv(int level, const char *subsystem, const char *format, va_list args) {
    if (!format || level < atomic_load(&current_level)) {
        return;
    }
    /* One locked write per message, so lines from vCPU threads do not interleave. */
    flockfile(stderr);
    fprintf(stderr, "fastdyn.%s|%s|  ", subsystem ? subsystem : "core", level_name(level));
    vfprintf(stderr, format, args);
    fputc('\n', stderr);
    funlockfile(stderr);
    fflush(stderr);
}

void fastdyn_log(int level, const char *subsystem, const char *format, ...) {
    va_list args;
    va_start(args, format);
    fastdyn_logv(level, subsystem, format, args);
    va_end(args);
}
