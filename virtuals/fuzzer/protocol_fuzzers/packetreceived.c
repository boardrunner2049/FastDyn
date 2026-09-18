#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fuzz.h"

static uint8_t fuzz_data[291];
void fuzz_packetreceived_inject(void) {
    memset(fuzz_data, 0, sizeof(fuzz_data));
    fuzz_get_data((char*)fuzz_data, sizeof(fuzz_data));
    fuzz_write_memory(fuzz_get_register(2), fuzz_data, sizeof(fuzz_data));
}
