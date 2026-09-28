#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <device.h>


#define NUM_CS_LINES 4          //USER can change the max number based on the requirement

typedef uint32_t (*SlaveTransferFunc)(uint32_t value);
typedef void (*SlaveSetcsFunc)(int level);

/*
 * Optional per-slave init hook. If a slave `.so` exports `slave_spi_init`
 * with this signature, spi_helper.c calls it once during bus setup — after
 * the slave has been dlopen'd but before any CS transition — with a
 * populated SpiSlaveInit that carries the slave's TOML-defined attributes.
 *
 * Slaves typically use this to call api_channel_attach() on their
 * `data_source` URI. All strings in SpiSlaveInit are owned by the caller
 * (spi_helper.c); the slave must strdup any it wants to retain past the
 * init call.
 */
typedef struct {
    const char* slave_name;   // as declared in TOML [[Device.<bus>.connections]].name
    int         cs_id;
    const char* data_source;  // boardrunner:// URI or NULL if TOML omitted the key
    const char* scroll_path;  // for diagnostics; NULL if slave was not scroll-loaded
} SpiSlaveInit;

typedef void (*SlaveInitFunc)(const SpiSlaveInit* info);

typedef struct {
    char* name;                 //Name of the slave
    int cs_id;                  //Chip select id for which the device will be registered
    int cs_enable;              //is the current chip enable
    SlaveTransferFunc transfer; //transfer function used to send and receive the data to/from slave
    SlaveSetcsFunc set_cs;      //function to tell the slave it is selected
} SpiSlaveDetails;

typedef struct {
    int num_slaves;
    SpiSlaveDetails* slave; //Dynamic array of slaves
} SpiSlaveList;

typedef struct {
    SpiSlaveList Slaves;  //Registered on spi_init_bus
} SPIBus;
// --- End of struct definitions ---

//SPI API functions definitions -- exposed to SPI Model writer
SPIBus api_spi_init_bus(ConfigSection* model_info, const char* bus_name);             //takes the user configuration for attached slaves and creates a bus with slaves attached
uint32_t api_spi_transfer(SPIBus *bus, uint32_t val);           //transfer the data to all the slaves and calls spi_transfer_raw_default for each slave
void api_spi_set_cs(SPIBus *bus, int cs_id, int level);
bool api_spi_select(SPIBus *bus, int cs_id);
bool api_spi_deselect(SPIBus *bus, int cs_id);
int api_spi_active_cs(const SPIBus *bus);
int api_spi_active_count(const SPIBus *bus);
uint8_t api_spi_transfer_byte(SPIBus *bus, uint8_t val);
int api_spi_transfer_buf(SPIBus *bus, const uint8_t *tx, uint8_t *rx, size_t len);
//End of SPI API funcitons definitions
