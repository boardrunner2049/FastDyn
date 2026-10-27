#ifndef FASTDYN_TRACE_CSV_H
#define FASTDYN_TRACE_CSV_H

#include <stdint.h>

#include "../phy.h"

int write_robustness_trace_csv(double sim_time_ms, const trace_t *trace);

#endif