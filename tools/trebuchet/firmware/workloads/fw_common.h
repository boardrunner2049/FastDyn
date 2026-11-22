/* Shared by the fixed-work workloads of the Trebuchet dataset. */
#ifndef FW_COMMON_H
#define FW_COMMON_H
#include <stdint.h>

/* Run parameters: read from the input bank before the scheduler starts. */
extern volatile uint32_t fw_param_seed;
extern volatile uint32_t fw_param_frames;

/* Record the result, report it through the marker functions and stop. */
void fw_finish(uint32_t checksum, uint32_t iterations);

void fwbench_start(void);
void kernbench_start(void);
void libbench_start(void);
void algobench_start(void);
#endif
