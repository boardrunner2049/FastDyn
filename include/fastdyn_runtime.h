#ifndef VIRTUAL_RUNTIME_H
#define VIRTUAL_RUNTIME_H

/*
 * Public C runtime SDK for compiled-in FastDyn plugins.
 *
 * A runtime module is given a stable namespaced context.  It may register virtual
 * callbacks, resolve artifacts below its own run-artifact directory, and add
 * a shutdown callback.  It must not inspect FastDyn command-line arguments
 * or construct paths below the work directory itself.
 */

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

#include "common.h"

typedef struct {
    const char *plugin_name;
} VirtualContext;

typedef void (*VirtualExitCallback)(void);
typedef void (*VirtualIrqCallback)(int irq);
typedef void (*VirtualTbTranslationHook)(qemu_plugin_id_t id,
                                                 struct qemu_plugin_tb *tb);
typedef int (*VirtualPluginInitializer)(const VirtualContext *ctx);

/* One MMIO access seen by FastDyn's device dispatch. */
typedef struct {
    uint64_t pc;          /* instruction that made the access */
    uint64_t address;
    uint64_t value;       /* value written, or value returned (0 if unhandled) */
    unsigned size;        /* bytes */
    bool is_write;
    bool handled;         /* a device model claimed the access */
    bool in_mmio_window;  /* the address is in a region FastDyn routes to device
                             models (0x40000000-0x5FFFFFFF, 0xE0000000-0xEFFFFFFF) */
} VirtualMmioAccess;

typedef void (*VirtualMmioObserver)(const VirtualMmioAccess *access);

typedef enum {
    VIRTUAL_LOG_DEBUG,
    VIRTUAL_LOG_INFO,
    VIRTUAL_LOG_WARN,
    VIRTUAL_LOG_ERROR,
} VirtualLogLevel;

typedef struct {
    const char *name;
    VirtualPluginInitializer initialize;
} VirtualPlugin;

int virtual_register_callback(const VirtualContext *ctx,
                                     const char *name, cb_func_t callback);
int virtual_artifact_path(const VirtualContext *ctx,
                                  const char *relative, char *out,
                                  size_t out_size);
void virtual_register_exit(const VirtualContext *ctx,
                                   VirtualExitCallback callback);
void virtual_log(const VirtualContext *ctx, VirtualLogLevel level,
                 const char *format, ...)
    __attribute__((format(printf, 3, 4)));

/* Guest state. These operations are meaningful from a runtime callback/hook. */
uint64_t virtual_pc(const VirtualContext *ctx);
uint64_t virtual_sp(const VirtualContext *ctx);
uint64_t virtual_icount(const VirtualContext *ctx);
uint64_t virtual_guest_time_ns(const VirtualContext *ctx);
/*
 * Read/write the complete target-register representation.  Register indices
 * come from an include/fastdyn/arch header. Values are in QEMU target byte
 * order; value_size receives the actual register width.
 */
int virtual_read_register_bytes(const VirtualContext *ctx,
                                        int reg, void *buffer,
                                        size_t buffer_size,
                                        size_t *value_size);
int virtual_write_register_bytes(const VirtualContext *ctx,
                                         int reg, const void *buffer,
                                         size_t size);
/* Convenience operations for 32-bit registers or low 32-bit values. */
uint32_t virtual_read_register(const VirtualContext *ctx, int reg);
void virtual_write_register(const VirtualContext *ctx, int reg,
                                    uint32_t value);
int virtual_read_memory(const VirtualContext *ctx,
                                uintptr_t address, size_t size, void *buffer);
int virtual_write_memory(const VirtualContext *ctx,
                                 uintptr_t address, size_t size,
                                 const void *buffer);
void virtual_raise_irq(const VirtualContext *ctx, int irq,
                               bool level);

/* Runtime instrumentation registration. */
void virtual_register_irq_hook(const VirtualContext *ctx,
                                       VirtualIrqCallback entry,
                                       VirtualIrqCallback exit);
/* Call `observer` for every MMIO access, handled or not, after the device
 * model ran; observers run in registration order on the vCPU thread.
 * Returns 0, or -1 if too many observers are registered. */
int virtual_register_mmio_observer(const VirtualContext *ctx,
                                   VirtualMmioObserver observer);
void virtual_register_tb_translation_hook(
    const VirtualContext *ctx, VirtualTbTranslationHook callback);
bool virtual_register_rule(const VirtualContext *ctx,
                                           uint64_t address, cb_func_t callback,
                                           const char *args);
bool virtual_register_update(const VirtualContext *ctx,
                                     uint64_t address, int reg,
                                     uint64_t value);
bool virtual_register_gated_modifier(const VirtualContext *ctx,
                                             uint64_t address, const char *patch,
                                             const volatile uint8_t *enabled);
void virtual_wait_for_trace_drain(const VirtualContext *ctx);

/* Called by FastDyn after its generic artifact root and callback registry are ready. */
int virtual_initialize_plugins(void);

/* Declare a compiled-in runtime module without editing the central dispatcher. */
#if defined(__GNUC__)
#define VIRTUAL_PLUGIN(plugin_name, initializer) \
    static const VirtualPlugin virtual_plugin_##initializer \
    __attribute__((used, section("virtual_plugins"))) = { \
        (plugin_name), (initializer) \
    }
#else
#error "FastDyn runtime plugin registration currently requires GCC-compatible linker sections"
#endif

#endif
