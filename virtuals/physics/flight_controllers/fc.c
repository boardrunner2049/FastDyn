#include <virtuals.h>
#include "fc.h"

extern int ardupilot_init_virtuals(int argc, char **argv);
extern int inav_init_virtuals(int argc, char **argv);

int fc_init(int argc, char ** argv) {
    // TODO: Add some sort of config to specify which flight controller
    // virtuals to initialize, for now just initialize all of them.
    ardupilot_init_virtuals(argc, argv);
    inav_init_virtuals(argc, argv);
    return 0;
}