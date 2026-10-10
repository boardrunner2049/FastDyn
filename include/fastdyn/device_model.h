/** The interface between FastDyn and a device model loaded by the elder
 * handler (ABI version 2).
 *
 * A model is a shared library that exports one descriptor:
 *
 *     #include <fastdyn/device_model.h>
 *
 *     static void *usart_init(const FdDevice *dev, const char *config_json,
 *                             const FdHost *host);
 *     static uint64_t usart_read(void *opaque, hwaddr addr, unsigned size, uint64_t pc);
 *     static void usart_write(void *opaque, hwaddr addr, uint64_t value,
 *                             unsigned size, uint64_t pc);
 *
 *     const FdModel fastdyn_model = {
 *         FASTDYN_MODEL_ABI_FIELDS,
 *         .init = usart_init,
 *         .read = usart_read,
 *         .write = usart_write,
 *     };
 *
 * FastDyn loads the library named by the device's `scroll` and calls init
 * once per device that uses it, so one library can serve several devices
 * (e.g. USART2 and USART3). Keep all state in the struct init returns, never
 * in globals: it is passed back as `opaque` to every other callback.
 *
 * This header also declares the emulator's host functions (interrupts,
 * timers, guest memory and registers) by including QEMU's
 * <qemu/qemu-plugin.h>, so a model includes only this header and, if it uses
 * LibVIO, <vio/vio.h>.
 *
 * Every struct here carries its size, so later versions can append fields,
 * host services and optional callbacks without breaking existing models.
 */
#ifndef FASTDYN_DEVICE_MODEL_H
#define FASTDYN_DEVICE_MODEL_H

#include <stddef.h>
#include <stdint.h>

#include <qemu/qemu-plugin.h>

#define FASTDYN_MODEL_ABI_VERSION 2

#ifndef FASTDYN_HWADDR_DEFINED
#define FASTDYN_HWADDR_DEFINED
typedef unsigned long hwaddr;
#endif

/** Log levels for FdHost.log (the same numbers as <fastdyn/log.h>). */
#define FD_LOG_LEVEL_TRACE 5
#define FD_LOG_LEVEL_DEBUG 10
#define FD_LOG_LEVEL_INFO 20
#define FD_LOG_LEVEL_WARNING 30
#define FD_LOG_LEVEL_ERROR 40

/** One guest address range of a device: `size` bytes starting at `base`. */
typedef struct FdRange {
    uint64_t base;
    uint64_t size;
} FdRange;

/** What FastDyn knows about one device, from its [Device.<name>] table.
 * Valid until the model is unloaded. */
typedef struct FdDevice {
    size_t size;            /* sizeof(FdDevice) in the FastDyn that filled it */
    const char *name;       /* the device's name, e.g. "usart2" */
    const FdRange *ranges;  /* its address ranges; accesses arrive as absolute addresses */
    size_t range_count;
    const int *irqs;        /* its interrupts, as the CPU numbers them (Cortex-M: IRQn + 16) */
    size_t irq_count;
} FdDevice;

/** Services FastDyn offers to models, so models never link against FastDyn.
 * Check `size` before using a member added after the first version. */
typedef struct FdHost {
    size_t size;  /* sizeof(FdHost) in the FastDyn that filled it */

    /** Log a printf-style message at `level` (FD_LOG_LEVEL_*), shown as
     * "fastdyn.<name>|LEVEL|  message"; pass the device's name. */
    void (*log)(int level, const char *name, const char *format, ...)
        __attribute__((format(printf, 3, 4)));
} FdHost;

/** Create the instance for `dev`. `config_json` is the device's own config
 * as a JSON object (e.g. the "slaves" for LibVIO buses); it and `host` are
 * valid only during the call, `dev` until unload. Returns the instance
 * state, or NULL to refuse the configuration (FastDyn then stops). */
typedef void *(*FdModelInit)(const FdDevice *dev, const char *config_json, const FdHost *host);

/** A guest read of `size` bytes at absolute address `addr` (inside one of
 * the device's ranges) by the instruction at `pc`. Returns the value. */
typedef uint64_t (*FdModelRead)(void *opaque, hwaddr addr, unsigned size, uint64_t pc);

/** A guest write of `value` (`size` bytes) at absolute address `addr`. */
typedef void (*FdModelWrite)(void *opaque, hwaddr addr, uint64_t value, unsigned size, uint64_t pc);

/** The CPU took (interrupt) or returned from (serve) interrupt `irq`, one of
 * the device's `irqs` (Cortex-M exception number: IRQn + 16). */
typedef void (*FdModelIrq)(void *opaque, int irq);

/** The descriptor a model exports as `fastdyn_model`. Fill `abi` and `size`
 * with FASTDYN_MODEL_ABI_FIELDS. */
typedef struct FdModel {
    uint32_t abi;            /* FASTDYN_MODEL_ABI_VERSION */
    uint32_t size;           /* sizeof(FdModel) the model was built with */
    FdModelInit init;        /* required */
    FdModelRead read;        /* required */
    FdModelWrite write;      /* required */
    FdModelIrq interrupt;    /* optional */
    FdModelIrq serve;        /* optional */
} FdModel;

#define FASTDYN_MODEL_ABI_FIELDS \
    .abi = FASTDYN_MODEL_ABI_VERSION, .size = sizeof(FdModel)

/** The descriptor's name, for FastDyn's lookup. */
#define FASTDYN_MODEL_SYMBOL "fastdyn_model"

extern const FdModel fastdyn_model;

#endif /* FASTDYN_DEVICE_MODEL_H */
