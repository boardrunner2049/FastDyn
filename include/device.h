#ifndef DEVICE_H
#define DEVICE_H

#include <stdint.h>
#include <qemu/qemu-plugin.h>
#include <fastdyn/device_model.h>

#ifdef ENABLE_DEV_DEBUG
    #define dev_debug(fmt, ...) printf(fmt, ##__VA_ARGS__)
#else
    #define dev_debug(fmt, ...) do {} while (0)
#endif

/* Required for Elder model */
#define DEV_LOGGER

typedef struct AddrRange {
    uint64_t start;
    uint64_t end;   // inclusive
} AddrRange;

// I2C Model related info start
typedef struct {
    int address;      //address of current slave device
    char* slave_name;   //name of current slave device
    char* scroll_path;   //scroll of current slave device
    bool _is_scroll;    //whether it's a share lib or built in qemu model
} I2CDevice;

typedef struct {
    int device_count;
    I2CDevice* i2cdevice;   //Dynamic array of all the attached slave devices
} I2CDevices;

//I2C Model related info end

//SPI Model related info end
typedef struct {
    char* slave_name;
    int cs_id;
    char* scroll_path;
    bool is_scroll_path;
} SPISlaveDevice;

typedef struct {
    int slave_count;
    SPISlaveDevice* spi_slave; // Dynamic array of SPI slaves
} SPIDevices;
// SPI Model Info End

// Represents a single device model (e.g., "uart", "gpio_g")
typedef struct {
    char* name;
    char* scroll_path;
    AddrRange* ranges;
    int range_count;
    uint16_t *irqs;
    int irq_count;
    char* config_json;   // the device's own config (a JSON object), passed to its model
    I2CDevices I2Cdevices;
    SPIDevices SPIdetails;
} DeviceModels;

typedef enum {
    TT_OFF = 0,
    TT_RECORD = 1,
    TT_REPLAY = 2,
} twintrace_mode_t;

// Represents a top-level section (e.g., "elder", "passthrough")
typedef struct {
    char* name;
    char* backend;
    AddrRange* overall_ranges;
    int overall_range_count;

    DeviceModels* devices; // Dynamic array of device models
    int device_count;

    twintrace_mode_t twintrace_mode;
    const char* twintrace_bin;
} ConfigSection;

// The root structure holding the entire configuration
typedef struct {
    ConfigSection* sections; // Dynamic array of sections
    int section_count;
} AppConfig;



/**
 * @brief Function pointer for a device write operation.
 */
typedef void (*DeviceWriteFunc)(void *opaque, hwaddr offset, uint64_t value, unsigned size, uint64_t pc);

/**
 * @brief Function pointer for a device read operation.
 */
typedef uint64_t (*DeviceReadFunc)(void *opaque, hwaddr offset, unsigned size, uint64_t pc);

typedef void* (*DeviceInit)(ConfigSection* args);

typedef int (*DeviceIRQFunc)(int);

/**
 * @brief Represents a device model, containing its name and I/O functions.
 */
typedef struct DeviceModel {
    const char *name;
    void *opaque;
    DeviceReadFunc read;
    DeviceWriteFunc write;
	DeviceInit init;
	DeviceIRQFunc serve;
	DeviceIRQFunc interrupt;
} DeviceModel;

typedef struct {
    char model[64];
    ConfigSection* args;
} ModelEntry;


typedef struct unimp_exporter {
    int (*read)(char * handler, hwaddr addr, uint64_t *buf, hwaddr len);
    int (*write)(char * handler, hwaddr addr, uint64_t value, hwaddr len);
} DEV_XPORTER;

//4KB regions for now.
#define SLOT_SIZE   0x10

#define DEVICE_BASE 0x40000000ULL
#define DEVICE_SIZE 0x20000000ULL  // 0x40000000 -> 0x5FFFFFFF, ~512 MB
#define NUM_SLOTS   (DEVICE_SIZE / SLOT_SIZE)

//Interrupt Handling
#define MAX_INTERRUPTS 240

// System Control Block → system_lut
#define SYSTEM_BASE 0xE0000000ULL
#define SYSTEM_SIZE 0x10000000ULL
#define RAM_BASE 0x20000000ULL
#define RAM_SIZE 0x10000000ULL
#define SYSTEM_NUM_SLOTS (SYSTEM_SIZE / SLOT_SIZE)
#define RAM_NUM_SLOTS (RAM_SIZE / SLOT_SIZE)


int dev_init(int argc, char ** argv);
void dev_register_device_model(hwaddr start, hwaddr end, DeviceModel *dev);
void dev_register_interrupt_device_model(int irq_num, DeviceModel *dev);

#endif // DEVICE_H
