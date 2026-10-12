#include <device.h>
#include <utils.h>
#include <pthread.h>
#include <unistd.h>

#define FD_LOG_SUBSYSTEM "classic"
#include <fastdyn/log.h>

static uint64_t classic_read(void *opaque, hwaddr address, unsigned size, uint64_t pc) {
    (void)opaque;

    uint64_t value = 0;
    // switch (address)
    // {
    //     case (hwaddr)0x58024804: // PWR control status register 1 (PWR_CSR1)
    //     case (hwaddr)0x58024818: // PWR D3 domain control register (PWR_D3CR)
    //         return 0x2000;

    //     case (hwaddr)0x58024400: // RCC source control register (RCC_CR)
    //         value |= (1UL << 2);
    //         value |= (1UL << 17);
    //         value |= (1UL << 13);
    //         value |= (1UL << 25);
    //         value |= (1UL << 27);
    //         value |= (1UL << 29);
    //         return value;

    //     case (hwaddr)0x58024410: // RCC clock configuration register (RCC_CFGR)
    //         value |= (1UL << 3); // HSE used as system clock
    //         value |= (1UL << 4);
    //         return value;

    //     case (hwaddr)0x52002000: // Flash access control register (FLASH_ACR)
    //         return 0x2;

    //     default:
    //         return value;
    // }
    return value;
}

static void classic_write(void *opaque, hwaddr address, uint64_t value, unsigned size, uint64_t pc) {
    (void)opaque;
}
static int classic_serve(int line) {
	FD_LOG_DEBUG("served line: %d", line);
	return 0;
}

static int classic_interrupt(int line) {
		(void)line;
		FD_LOG_DEBUG("interrupt line: %d", line);
		return 0;
}

int classic_parse_ranges(const char *s, Range *ranges, int max_ranges) {
    int count = 0;
    char buffer[256];
    strncpy(buffer, s, sizeof(buffer));
    buffer[sizeof(buffer)-1] = '\0';

    char *token = strtok(buffer, "~");
    while (token && count < max_ranges) {
        // Remove leading spaces
        while (*token == ' ') token++;

        char *dash = strchr(token, '-');
        if (dash) {
            *dash = '\0';
            ranges[count].start = strtoul(token, NULL, 0); // auto-detect hex with 0x
            ranges[count].end   = strtoul(dash + 1, NULL, 0);
            count++;
        }

        token = strtok(NULL, ",");
    }

    return count;
}
static void* classic_init(ConfigSection* model_info);
// The public definition of the classic device model
DeviceModel classic_model_def = {
    .name = "classic",
    .read = classic_read,
    .write = classic_write,
    .init = classic_init,
    .serve = classic_serve,
    .interrupt = classic_interrupt,
};

static void* classic_init(ConfigSection* model_info) {
    Range ranges[10];
    utils_parse_ranges(model_info->overall_range_count,model_info->overall_ranges, ranges);

	for (int i = 0; i < model_info->overall_range_count; i++) {
        dev_register_device_model(ranges[i].start, ranges[i].end, &classic_model_def);
    }
    return NULL;
}

