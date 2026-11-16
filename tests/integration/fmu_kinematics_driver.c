// Drive the built FMU through the plugin's normal getters and OptiFuzz CSV writer.
#include <stdlib.h>
#include "physics_engines/fmu/fmu.h"
#include "fmu_trace/trace_csv.h"

int main(int argc, char **argv)
{
    if (argc < 6 || !phy_select_backend("fmu")) {
        return 1;
    }
    for (int channel = 0; channel < 4; channel++) {
        if (!phy_set_servo_pwm(channel, atoi(argv[1 + channel]))) {
            return 1;
        }
    }
    fmu_init(argc - 5, argv + 5);
    if (!fmu_backend.init()) {
        return 1;
    }
    for (int ms = 0; ms <= 1000; ms++) {
        trace_t trace;
        if (!phy_advance_simulation(ms / 1000.0) || !phy_get_trace(&trace) ||
            !write_robustness_trace_csv(ms, &trace)) {
            return 1;
        }
    }
    fmu_backend.shutdown();
    return 0;
}
