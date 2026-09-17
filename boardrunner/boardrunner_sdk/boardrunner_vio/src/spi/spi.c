#include "spi_internal.h"
#include <utils.h>

/**
 * @brief Initializes an SPI bus by parsing slave device configuration.
 *
 * @param model_info The configuration section containing device information.
 * @return An initialized SPIBus struct. If initialization fails, the
 * returned struct will be empty (num_slaves will be 0).
 */
#undef api_spi_init_bus
SPIBus api_spi_init_bus_impl(ConfigSection* model_info, const char* bus_name) {
    SPIBus bus = {0};
    bool success;
    success = spi_slave_device_parse(&bus, model_info, bus_name); //parses all the attached slave devices
    if (!success) {
        utils_die("Unable to parse the slave devices attached to I2C");
    }
    return bus;
}

/**
 * @brief Sets the logic state of a specific chip select (CS) line.
 *
 * This function iterates through all registered slaves. For any slave that
 * matches the target 'cs_id', it updates the bus's internal 'cs_enable'
 * state for that slave and then calls the slave's 'set_cs' callback function
 * to notify it of the change.
 *
 * @param bus A pointer to the SPIBus structure.
 * @param cs_id The identifier of the chip select line to modify.
 * @param level The new logic level of the line (0 = active, 1 = inactive).
 */
void api_spi_set_cs(SPIBus *bus, int cs_id, int level) {
    if (!bus) {
        // Safety check for null bus pointer
        return;
    }

    // Determine the new enable state (1 for active, 0 for inactive)
    // This assumes an active-low chip select, which is standard.
    int new_enable_state = (level == 0) ? 1 : 0;

    // Loop through all slaves registered on the bus
    for (int i = 0; i < bus->Slaves.num_slaves; i++) {
        SpiSlaveDetails* current_slave = &bus->Slaves.slave[i];

        // Check if this slave is attached to the target CS line
        if (current_slave->cs_id == cs_id) {

            // 1. Update the bus's record of this slave's state
            current_slave->cs_enable = new_enable_state;

            // 2. Notify the slave model by calling its callback, if it exists
            if (current_slave->set_cs) {
                current_slave->set_cs(level);
            }
        }
    }
}

/**
 * @brief Performs a full-duplex SPI transfer with the active slave.
 *
 * This function scans the bus for a slave that is currently active
 * (i.e., 'cs_enable' is 1). It calls the 'transfer' function of the
 * first active slave it finds, passing the master's data 'val' (MOSI).
 * It then returns the 32-bit value received from that slave (MISO).
 *
 * @param bus A pointer to the SPIBus structure.
 * @param val The 32-bit data word sent from the master (MOSI).
 * @return uint32_t The 32-bit data word received from the slave (MISO).
 * Returns 0xFFFFFFFF (idle line) if no slave is active.
 */
uint32_t api_spi_transfer(SPIBus *bus, uint32_t val) {
    uint32_t ret_val = 0xFFFFFFFF;
    if (!bus) {
        // Safety check, return idle value
        return ret_val;
    }
    // printf("Current value to be written\n %d", val);

    // Find the currently selected slave
    for (int i = 0; i < bus->Slaves.num_slaves; i++) {
        SpiSlaveDetails* current_slave = &bus->Slaves.slave[i];

        // Check if this slave is enabled (active)
        if (current_slave->cs_enable == 1) {

            // Found the active slave. Call its transfer function, if it exists.
            if (current_slave->transfer) {
                return current_slave->transfer(val);
            } else {
                // Slave is active but has no transfer function.
                // This is a configuration error, but we'll return idle.
                fprintf(stderr, "SPI Error: Slave '%s' is active but has no transfer function.\n",
                        current_slave->name ? current_slave->name : "unknown");
                return ret_val;
            }
        }
    }
    // printf("Current value to be returned\n %d", ret_val);

    // If no slave was found to be active, return the idle bus value
    return ret_val;
}