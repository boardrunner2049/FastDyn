#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <device.h>   // ConfigSection, etc.

enum i2c_event {
    I2C_START_RECV,
    I2C_START_SEND,
    I2C_FINISH,
    I2C_NACK
};

typedef int     (*SlaveSendFunc)(uint8_t data);
typedef uint8_t (*SlaveRecvFunc)(void);
typedef int     (*SlaveEventFunc)(enum i2c_event event);

/*
 * Optional per-slave init hook. If an I2C slave .so exports
 * `slave_i2c_init` with this signature, i2c_helper.c calls it once during
 * bus setup — after the slave is dlopen'd but before any transaction —
 * with the slave's TOML-defined attributes. Slaves typically use this to
 * call api_channel_attach() on their `data_source` URI.
 *
 * Mirrors the SPI plumbing (see boardrunner/spi.h SpiSlaveInit).
 */
typedef struct {
    const char* slave_name;    // TOML [[Device.<bus>.connections]].name
    uint16_t    address;
    const char* data_source;   // boardrunner:// URI or NULL if TOML omitted
    const char* scroll_path;   // for diagnostics
} I2cSlaveInit;

typedef void (*SlaveI2cInitFunc)(const I2cSlaveInit* info);

typedef struct {
    char* name;
    uint16_t address;      // you already store as uint16_t
    SlaveSendFunc send;
    SlaveRecvFunc recv;
    SlaveEventFunc event;
} SlaveDetails;

typedef struct {
    int num_slaves;
    SlaveDetails* slave;
} SlaveList;

typedef struct {
    SlaveList Slaves;
    SlaveDetails* current_dev;
    uint8_t saved_address;
} I2CBus;

// Public APIs
I2CBus  api_i2c_init_bus(ConfigSection* model_info);
int     api_i2c_start_transfer(I2CBus* bus, uint8_t address, bool is_recv);
int     api_i2c_start_transfer_10bit(I2CBus* bus, uint16_t address, bool is_recv);
void    api_i2c_end_transfer(I2CBus* bus);
int     api_i2c_send(I2CBus* bus, uint8_t data);
uint8_t api_i2c_recv(I2CBus* bus);
