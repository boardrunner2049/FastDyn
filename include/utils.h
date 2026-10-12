#ifndef UTILS_H
#define UTILS_H

#include <stdio.h>
#include <stdlib.h>
#include "device.h"

/* Diagnostics use <fastdyn/log.h>. DEBUG_LOG, kept for older code, logs at
 * DEBUG as fastdyn.debug. fastdyn_log() is declared here rather than by
 * including <fastdyn/log.h>, so this header does not set a file's log
 * subsystem or pull in config.h. */
void fastdyn_log(int level, const char *subsystem, const char *format, ...)
    __attribute__((format(printf, 3, 4)));
#define DEBUG_LOG(fmt, ...) fastdyn_log(10, "debug", fmt, ##__VA_ARGS__)

/* Writes trace data (io.log) to fp and flushes it. */
void utils_log_to_file(FILE *fp, const char *fmt, ...);

typedef struct {
    unsigned long start;
    unsigned long end;
} Range;

// Print error message and exit
void utils_die(const char *msg);

// Log warning, this wont kill fastdyn
void utils_warn(const char *msg); 

// Get the value of a command-line argument: --key=value
// Returns NULL if not found or malformed
char *utils_get_arg(const char *key, int argc, char **argv);

int utils_init(int argc, char ** argv);

void utils_parse_ranges(int range_count, const AddrRange *overall_ranges, Range *ranges);
#endif // UTILS_H

// {
//     "passthrough": {
//         "backend": "stlink",
//         "gpiog": {
//             "irq": "0-100",
//             "range": [
//                 "0x40021800-0x40021BFF"
//             ]
//         },
//         "overall": [
//             "0x40021800-0x40021BFF",
//             "0x40000000-0x400217FF",
//             "0x40021C00-0xE00FFFFF",
//             "0xE0000000-0xEFFFFFFF"
//         ],
//         "unhandled_space": {
//             "range": [
//                 "0x40000000-0x400217FF",
//                 "0x40021C00-0xE00FFFFF",
//                 "0xE0000000-0xEFFFFFFF"
//             ]
//         }
//     }
// }
