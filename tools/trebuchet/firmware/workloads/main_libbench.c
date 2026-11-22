/* C-library workload: formatting, string and memory routines, number
 * parsing, sorting and searching (qsort/bsearch call back through a function
 * pointer).  One task; work is an iteration count. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "FreeRTOS.h"
#include "task.h"
#include "fw_common.h"
#include "fw_proto.h"

#define LIB_SCALE 30u
#define SLOTS 8u
static char slot[SLOTS][48];
static int table[16];

static int cmp_int(const void *a, const void *b)
{
    int x = *(const int *)a, y = *(const int *)b;
    return (x > y) - (x < y);
}

static void lib_task(void *arg)
{
    uint32_t seed = fw_param_seed, sum = 0;
    const uint32_t n = fw_param_frames * LIB_SCALE;
    static const char *const names[4] = { "alpha", "bravo", "charlie", "delta" };
    (void)arg;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t r = fw_prng(&seed);
        char *s = slot[i % SLOTS];
        int len = snprintf(s, sizeof slot[0], "id=%d v=%x %s", (int)(r & 0xffff), (unsigned)(r >> 8), names[r & 3]);
        sum += (uint32_t)len + (uint32_t)strlen(s);
        sum += (uint32_t)strtoul(s + 3, NULL, 10);
        if (strcmp(s, slot[(i + 3) % SLOTS]) < 0) sum ^= r;
        memmove(s + 1, s, (size_t)len < 40 ? (size_t)len : 40);
        sum += (uint8_t)s[5];
        if (strchr(s, 'v') != NULL) sum++;
        table[i & 15u] = (int)(r >> 16) - 30000;
        if ((i & 15u) == 15u) {
            int key = table[7];
            qsort(table, 16, sizeof table[0], cmp_int);
            const int *hit = bsearch(&key, table, 16, sizeof table[0], cmp_int);
            sum = sum * 31u + (uint32_t)table[0] + (uint32_t)table[15] + (uint32_t)(hit - table);
            memset(slot[(i >> 4) % SLOTS], 0, 8);
        }
    }
    fw_finish(sum, n);
}

void libbench_start(void)
{
    xTaskCreate(lib_task, "lib", configMINIMAL_STACK_SIZE * 4, NULL, tskIDLE_PRIORITY + 1, NULL);
}
