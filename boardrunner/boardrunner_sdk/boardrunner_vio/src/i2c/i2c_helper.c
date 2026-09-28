#include "i2c_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>

#include <utils.h>


// Internal helpers (not exposed to model authors)
// Helper functions
/**
 * @brief Scans the bus for a slave with the given address.
 * @param bus A pointer to the I2CBus.
 * @param address The 7-bit slave address to find.
 * @param found_dev A pointer-to-a-pointer, which will be updated to point to the found slave.
 * @return true if the slave was found, false otherwise.
 */
bool i2c_scan_bus(I2CBus *bus, uint8_t address, SlaveDetails **found_dev) {
    for (int i = 0; i < bus->Slaves.num_slaves; i++) {
        // Get a pointer to the current slave in the array
        SlaveDetails *curr_slave = &bus->Slaves.slave[i];

        // Check if the 7-bit address matches (ignoring R/W bit for scanning)
        if (address == (curr_slave->address & 0x7F)) {
            // Found the slave. Update the caller's pointer via the pointer-to-a-pointer.
            *found_dev = curr_slave;
            return true; // Success
        }
    }

    *found_dev = NULL; // Ensure the output pointer is NULL if not found
    return false; // Failure
}

/**
 * @brief (Internal) Starts a transaction with a slave device on the bus.
 * @param bus A pointer to the I2CBus.
 * @param address The pure 7-bit slave address.
 * @param event The event to send (I2C_START_SEND or I2C_START_RECV).
 * @return 0 on success (ACK), non-zero on failure (NACK).
 */
int i2c_do_start_transfer(I2CBus *bus, uint8_t address, enum i2c_event event) {
    // If no device is currently active, scan for the requested one.

    if (!bus->current_dev) {
        bool found = i2c_scan_bus(bus, address, &bus->current_dev);
        if (!found) {
            return 1; // Device not found on bus, return NACK/failure
        }
    }

    // After scanning, if current_dev is still NULL, something is wrong.
    if (!bus->current_dev) {
        return 1;
    }

    int event_status = bus->current_dev->event(event);

    return event_status;
}

int i2c_do_start_transfer_10bit(I2CBus *bus, uint16_t address, enum i2c_event event) {
    // If no device is currently active, scan for the requested one.
    if (!bus->current_dev) {
        bool found = i2c_scan_bus(bus, address, &bus->current_dev);
        if (!found) {
            return 1; // Device not found on bus, return NACK/failure
        }
    }

    // After scanning, if current_dev is still NULL, something is wrong.
    if (!bus->current_dev) {
        return 1;
    }

    int event_status = bus->current_dev->event(event);

    return event_status;
}
/**
 * @brief This function parses the device models to configure I2C slave devices.
 * It is called once when initializing the model.
 *
 * @param bus The I2C bus structure to populate.
 * @param model_info The configuration section containing device information.
 * @return true on successful parsing, false on failure.
 */
bool i2c_slave_device_parse(I2CBus* bus, ConfigSection* model_info) {
    if (!bus || !model_info) {
        return false;
    }

    // Iterate through all the devices registered with the elder model
    for (int i = 0; i < model_info->device_count; i++) {
        DeviceModels* current_dev = &model_info->devices[i];

        // Stop when we find the device with the name "i2c" in it.
        if (strstr(current_dev->name, "i2c") != NULL) {
            // Allocate memory for the SlaveList on the heap
            SlaveList* slavelist = calloc(1, sizeof(SlaveList));
            if (!slavelist) {
                perror("Failed to allocate memory for SlaveList");
                return false;
            }

            I2CDevices i2c_details = current_dev->I2Cdevices;
            // Register all the i2c slave devices requested by the user
            for (int j = 0; j < i2c_details.device_count; j++) {
                // We are only interested in devices that are shared libraries ("scrolls")
                if (i2c_details.i2cdevice[j]._is_scroll) {
                    I2CDevice* curr_slave_info = &i2c_details.i2cdevice[j];

                    void* scroll_handle = dlopen(curr_slave_info->scroll_path, RTLD_NOW);
                    if (!scroll_handle) {
                        fprintf(stderr, "Unable to open slave device scroll '%s': dlopen failed: %s\n",
                                curr_slave_info->scroll_path, dlerror());
                        // Clean up previously allocated memory before exiting
                        free(slavelist->slave);
                        free(slavelist);
                        return false;
                    }

                    // Grow the slavelist
                    slavelist->num_slaves++;
                    SlaveDetails* new_slave_array = realloc(slavelist->slave, slavelist->num_slaves * sizeof(SlaveDetails));
                    if (!new_slave_array) {
                        perror("Failed to reallocate memory for slave array");
                        dlclose(scroll_handle);
                        free(slavelist->slave); // realloc failure doesn't free the original block
                        free(slavelist);
                        return false;
                    }
                    slavelist->slave = new_slave_array;

                    // Get a pointer to the new slave entry to populate it
                    SlaveDetails* new_slave = &slavelist->slave[slavelist->num_slaves - 1];

                    // Clear the struct and populate it
                    memset(new_slave, 0, sizeof(SlaveDetails));
                    new_slave->name = curr_slave_info->slave_name;
                    new_slave->address = curr_slave_info->address;

                    /* Fixed symbol names (mirror SPI convention). Each slave
                     * .so provides `slave_i2c_send`, `slave_i2c_receive`,
                     * `slave_i2c_event`, and optionally `slave_i2c_init`. */
                    new_slave->send = (SlaveSendFunc)dlsym(scroll_handle, "slave_i2c_send");
                    if (!new_slave->send) {
                        fprintf(stderr, "Missing 'slave_i2c_send' in slave model '%s'\n",
                                curr_slave_info->scroll_path);
                        dlclose(scroll_handle);
                        free(slavelist->slave);
                        free(slavelist);
                        return false;
                    }

                    new_slave->recv = (SlaveRecvFunc)dlsym(scroll_handle, "slave_i2c_receive");
                    if (!new_slave->recv) {
                        fprintf(stderr, "Missing 'slave_i2c_receive' in slave model '%s'\n",
                                curr_slave_info->scroll_path);
                        dlclose(scroll_handle);
                        free(slavelist->slave);
                        free(slavelist);
                        return false;
                    }

                    new_slave->event = (SlaveEventFunc)dlsym(scroll_handle, "slave_i2c_event");
                    if (!new_slave->event) {
                        fprintf(stderr, "Missing 'slave_i2c_event' in slave model '%s'\n",
                                curr_slave_info->scroll_path);
                        dlclose(scroll_handle);
                        free(slavelist->slave);
                        free(slavelist);
                        return false;
                    }

                    /* Optional per-slave init hook. Missing is fine — many
                     * I2C slaves have nothing to init. If present, called
                     * once with TOML-carried attributes (data_source URI
                     * etc.) before any transaction. */
                    SlaveI2cInitFunc slave_init =
                        (SlaveI2cInitFunc)dlsym(scroll_handle, "slave_i2c_init");
                    if (slave_init) {
                        I2cSlaveInit info = {
                            .slave_name  = curr_slave_info->slave_name,
                            .address     = (uint16_t)curr_slave_info->address,
                            .data_source = curr_slave_info->data_source, // may be NULL
                            .scroll_path = curr_slave_info->scroll_path,
                        };
                        slave_init(&info);
                    }
                }
            }

            // The I2CBus struct holds the SlaveList by value, not by pointer.
            // So we copy the contents and free the temporary allocation.
            bus->Slaves = *slavelist;
            free(slavelist);

            return true; // Successful parsing
        }
    }

    // If we finished the loop and no device named "i2c" was found
    fprintf(stderr, "Error: Configuration parsing failed. No 'i2c' device found.\n");
    return false; // Failure parsing
}