// Observe logical step requests at the upstream simulator's GPIO interface.
// That driver does not clear the pin after every pulse, so these are writes,
// not observations of physical rising edges. Defaults use no pin inversions.
#include <stdio.h>
#include <stdlib.h>
#include "mcu.h"
#include "driver.h"

void __real_mcu_gpio_set(gpio_port_t *port, uint16_t pins, uint16_t mask);

void __wrap_mcu_gpio_set(gpio_port_t *port, uint16_t pins, uint16_t mask)
{
    __real_mcu_gpio_set(port, pins, mask);
    if (port != &gpio[STEP_PORT0] || !(pins & mask & 7))
        return;

    static FILE *events;
    if (!events) {
        events = fopen("step-requests.bin", "wbx");
        if (!events) {
            perror("step-requests.bin");
            exit(EXIT_FAILURE);
        }
    }
    unsigned char event = ((gpio[DIR_PORT].state.value & 7) << 4) | (pins & mask & 7);
    if (fputc(event, events) == EOF) {
        perror("step-requests.bin");
        exit(EXIT_FAILURE);
    }
}
