#ifndef UTILS_H
#define UTILS_H

#include <stdio.h>
#include <stdlib.h>
#include "device.h"

#define DEBUG_FILE


#if DEBUG_PRINT
  #define DEBUG_LOG(fmt, ...) \
    printf(fmt "\n", ##__VA_ARGS__)
#elif defined(DEBUG_FILE)
  FILE *utils_log_fp(void);
  void utils_log_to_file(FILE *fp, const char *fmt, ...);
  #define DEBUG_LOG(fmt, ...) \
    utils_log_to_file(utils_log_fp(), fmt "\n", ##__VA_ARGS__)
#else
  #define DEBUG_LOG(fmt, ...) ((void)0)
#endif

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
