/* Entry point of the fixed-work workloads.  FW_WORKLOAD selects one:
 * 1 fwbench (protocol + queue), 2 kernbench (kernel services),
 * 3 libbench (C library), 4 algobench (compute kernels).
 * (0 is the packet-processing application in main_compute.c.)
 *
 * The run parameters come from the input bank, like the input stream of the
 * compute application, so one firmware image and one translation cache serve
 * every input.  The measured window and the result use the same marker
 * functions as main_compute.c. */
#include <stdint.h>
#include "FreeRTOS.h"
#include "task.h"
#include "fw_common.h"

#define FW_INPUT_BASE   0x21000000UL
#define FW_INPUT_MAGIC  0x52504254UL            /* "TBPR" */

typedef struct { uint32_t magic, seed, frames; } FwInput_t;
typedef struct { uint32_t checksum, iterations; } FwResult_t;

volatile uint32_t fw_param_seed = 0x1234abcdUL;
volatile uint32_t fw_param_frames = 8000UL;
FwResult_t fw_result;                           /* read by the stock-QEMU runner */

extern void vComputeWindowStart( void );
extern void vComputeReport1( uint32_t a, uint32_t b, uint32_t c, uint32_t d );
extern void vComputeDone( void );

__attribute__((noinline)) void fw_finish(uint32_t checksum, uint32_t iterations)
{
    fw_result.checksum = checksum;
    fw_result.iterations = iterations;
    vComputeReport1(checksum, iterations, 0, 0);
    vComputeDone();
    for (;;) { }
}

void main_workload(void)
{
    const FwInput_t *in = (const FwInput_t *)FW_INPUT_BASE;

    if (in->magic == FW_INPUT_MAGIC) {
        fw_param_seed = in->seed;
        fw_param_frames = in->frames;
    }
    vComputeWindowStart();
#if FW_WORKLOAD == 1
    fwbench_start();
#elif FW_WORKLOAD == 2
    kernbench_start();
#elif FW_WORKLOAD == 3
    libbench_start();
#else
    algobench_start();
#endif
    vTaskStartScheduler();
    for (;;) { }
}
