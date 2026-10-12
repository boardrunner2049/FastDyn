#include <utils.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>

#include <fastdyn/log.h>

void utils_die(const char *msg) {
    FD_LOG_ERROR("%s", msg);
    exit(EXIT_FAILURE);
}

void utils_warn(const char *msg) {
    FD_LOG_WARN("%s", msg);
}

char * utils_get_arg(const char * key, int argc, char **argv) {
    int len = strlen(key);
    for (int i =0; i < argc; i ++) {
        if (strncmp(argv[i], key, len) == 0) {
            return (argv[i] + len + 1);
        }
    }

    return NULL;
}

void utils_log_to_file(FILE *fp, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vfprintf(fp, fmt, args);
    va_end(args);

	fflush(fp);  // ensure log is flushed to disk
}

int utils_init(int argc, char** argv) {
	/* The frontend passes the log level as a Python logging number. */
	const char *log_level = utils_get_arg("log_level", argc, argv);
	if (log_level) {
		fastdyn_log_set_level((int)strtol(log_level, NULL, 10));
	}
	return 0;
}

void utils_parse_ranges(int range_count, const AddrRange *overall_ranges, Range *ranges) {
    if (!ranges) return;
    if (range_count <= 0 || !overall_ranges) return;

    for (int i = 0; i < range_count; i++) {
        uint64_t start = overall_ranges[i].start;
        uint64_t end   = overall_ranges[i].end;

        // Optional safety: normalize/validate
        if (end < start) {
            // You can choose to swap, skip, or hard-fail.
            // Swapping is often safer than silently zeroing.
            uint64_t tmp = start;
            start = end;
            end = tmp;
        }

        ranges[i].start = start;
        ranges[i].end   = end;
    }
}

