/* Levelled logging for FastDyn's native code.
 *
 * Usage, once per source file:
 *
 *     #define FD_LOG_SUBSYSTEM "dev"
 *     #include <fastdyn/log.h>
 *
 *     FD_LOG_INFO("registered %d device models", count);
 *
 * Messages go to stderr as "fastdyn.<subsystem>|LEVEL|  message", like the
 * Python frontend's log lines. The runtime level is set by the frontend
 * (fastdyn -q/-v/--log-level); levels use Python's logging numbers.
 * FD_LOG_DEBUG and FD_LOG_TRACE are compiled out unless the build enables
 * DEBUG_PRINT, so they cost nothing on hot paths in normal builds.
 */
#ifndef FASTDYN_LOG_H
#define FASTDYN_LOG_H

#include <stdarg.h>

#if defined(__has_include)
#if __has_include(<config.h>)
#include <config.h>
#endif
#endif

#define FD_LOG_LEVEL_TRACE 5
#define FD_LOG_LEVEL_DEBUG 10
#define FD_LOG_LEVEL_INFO 20
#define FD_LOG_LEVEL_WARNING 30
#define FD_LOG_LEVEL_ERROR 40

#ifndef FD_LOG_SUBSYSTEM
#define FD_LOG_SUBSYSTEM "core"
#endif

/* Set the lowest level that is printed (default: FD_LOG_LEVEL_INFO). */
void fastdyn_log_set_level(int level);
int fastdyn_log_get_level(void);

void fastdyn_log(int level, const char *subsystem, const char *format, ...)
    __attribute__((format(printf, 3, 4)));
void fastdyn_logv(int level, const char *subsystem, const char *format, va_list args);

#define FD_LOG_ERROR(...) fastdyn_log(FD_LOG_LEVEL_ERROR, FD_LOG_SUBSYSTEM, __VA_ARGS__)
#define FD_LOG_WARN(...) fastdyn_log(FD_LOG_LEVEL_WARNING, FD_LOG_SUBSYSTEM, __VA_ARGS__)
#define FD_LOG_INFO(...) fastdyn_log(FD_LOG_LEVEL_INFO, FD_LOG_SUBSYSTEM, __VA_ARGS__)

#if defined(DEBUG_PRINT) && DEBUG_PRINT
#define FD_LOG_DEBUG(...) fastdyn_log(FD_LOG_LEVEL_DEBUG, FD_LOG_SUBSYSTEM, __VA_ARGS__)
#define FD_LOG_TRACE(...) fastdyn_log(FD_LOG_LEVEL_TRACE, FD_LOG_SUBSYSTEM, __VA_ARGS__)
#else
/* Compiled out, but the arguments are still type-checked. */
#define FD_LOG_DEBUG(...) do { if (0) fastdyn_log(FD_LOG_LEVEL_DEBUG, FD_LOG_SUBSYSTEM, __VA_ARGS__); } while (0)
#define FD_LOG_TRACE(...) do { if (0) fastdyn_log(FD_LOG_LEVEL_TRACE, FD_LOG_SUBSYSTEM, __VA_ARGS__); } while (0)
#endif

#endif /* FASTDYN_LOG_H */
