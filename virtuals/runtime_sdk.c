/* Generic runtime-plugin dispatcher and public SDK implementation. */
#include <fastdyn_runtime.h>
#include <dlfcn.h>
#include <stdlib.h>

#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <core.h>
#include <fastdyn/log.h>
#include <virtuals.h>

extern const VirtualPlugin __start_virtual_plugins[];
extern const VirtualPlugin __stop_virtual_plugins[];

#define VIRTUAL_RUNTIME_MAX_PLUGINS 64
static VirtualContext runtime_contexts[VIRTUAL_RUNTIME_MAX_PLUGINS];

static int valid_relative_path(const char *relative) {
    return relative && relative[0] && relative[0] != '/'
        && strstr(relative, "..") == NULL;
}

int virtual_register_callback(const VirtualContext *ctx,
                                     const char *name, cb_func_t callback) {
    (void)ctx;
    return virtual_register(name, callback);
}

int virtual_artifact_path(const VirtualContext *ctx,
                                  const char *relative, char *out,
                                  size_t out_size) {
    char namespaced[512];
    if (!ctx || !ctx->plugin_name || !ctx->plugin_name[0]
        || !valid_relative_path(relative)
        || snprintf(namespaced, sizeof(namespaced), "%s/%s", ctx->plugin_name,
                    relative) >= (int)sizeof(namespaced)) {
        return -1;
    }
    return core_get_run_artifact_path(namespaced, out, out_size);
}

void virtual_register_exit(const VirtualContext *ctx,
                                   VirtualExitCallback callback) {
    (void)ctx;
    if (callback) {
        core_register_exit_hook(callback);
    }
}

void virtual_log(const VirtualContext *ctx, VirtualLogLevel level,
                 const char *format, ...) {
    /* Plugin messages use FastDyn's native logger; the subsystem is the plugin name. */
    static const int levels[] = {
        FD_LOG_LEVEL_DEBUG, FD_LOG_LEVEL_INFO, FD_LOG_LEVEL_WARNING, FD_LOG_LEVEL_ERROR,
    };
    va_list args;
    const char *plugin = (ctx && ctx->plugin_name) ? ctx->plugin_name : "unknown";
    int native_level = (level >= VIRTUAL_LOG_DEBUG && level <= VIRTUAL_LOG_ERROR)
                       ? levels[level] : FD_LOG_LEVEL_ERROR;

    va_start(args, format);
    fastdyn_logv(native_level, plugin, format, args);
    va_end(args);
}

uint64_t virtual_pc(const VirtualContext *ctx) {
    (void)ctx;
    return core_get_pc();
}

uint64_t virtual_sp(const VirtualContext *ctx) {
    (void)ctx;
    return core_get_sp();
}

uint64_t virtual_icount(const VirtualContext *ctx) {
    (void)ctx;
    return core_get_icount();
}

uint64_t virtual_guest_time_ns(const VirtualContext *ctx) {
    (void)ctx;
    return qemu_plugin_get_virtual_timer();
}

int virtual_read_register_bytes(const VirtualContext *ctx,
                                        int reg, void *buffer,
                                        size_t buffer_size,
                                        size_t *value_size) {
    GArray *descriptors;
    qemu_plugin_reg_descriptor *descriptor;
    GByteArray *value;
    int result;

    (void)ctx;
    if (!buffer || !value_size || reg < 0) {
        return -1;
    }
    descriptors = qemu_plugin_get_registers();
    if (!descriptors || (guint)reg >= descriptors->len) {
        if (descriptors) {
            g_array_free(descriptors, TRUE);
        }
        return -1;
    }
    descriptor = &g_array_index(descriptors, qemu_plugin_reg_descriptor, reg);
    value = g_byte_array_sized_new(buffer_size);
    result = qemu_plugin_read_register(descriptor->handle, value);
    if (result < 0 || (size_t)result > buffer_size) {
        g_byte_array_free(value, TRUE);
        g_array_free(descriptors, TRUE);
        return -1;
    }
    memcpy(buffer, value->data, (size_t)result);
    *value_size = (size_t)result;
    g_byte_array_free(value, TRUE);
    g_array_free(descriptors, TRUE);
    return 0;
}

int virtual_write_register_bytes(const VirtualContext *ctx,
                                         int reg, const void *buffer,
                                         size_t size) {
    (void)ctx;
    if (!buffer || reg < 0 || size == 0) {
        return -1;
    }
    qemu_plugin_set_register((uint8_t *)buffer, reg);
    return 0;
}

uint32_t virtual_read_register(const VirtualContext *ctx, int reg) {
    (void)ctx;
    return qemu_get_register(reg);
}

void virtual_write_register(const VirtualContext *ctx, int reg,
                                    uint32_t value) {
    (void)ctx;
    qemu_set_register(value, reg);
}

int virtual_read_memory(const VirtualContext *ctx,
                                uintptr_t address, size_t size, void *buffer) {
    (void)ctx;
    if (!buffer || size > INT_MAX) {
        return -1;
    }
    return qemu_plugin_read_memory(address, (uint8_t *)buffer, (int)size);
}

int virtual_write_memory(const VirtualContext *ctx,
                                 uintptr_t address, size_t size,
                                 const void *buffer) {
    (void)ctx;
    if (!buffer || size > INT_MAX) {
        return -1;
    }
    return qemu_plugin_write_memory(address, (uint8_t *)buffer, (int)size);
}

void virtual_raise_irq(const VirtualContext *ctx, int irq,
                               bool level) {
    (void)ctx;
    qemu_plugin_raise_irq(irq, level);
}

void virtual_register_irq_hook(const VirtualContext *ctx,
                                       VirtualIrqCallback entry,
                                       VirtualIrqCallback exit) {
    (void)ctx;
    core_register_irq_hook(entry, exit);
}

#define VIRTUAL_MAX_MMIO_OBSERVERS 16
static VirtualMmioObserver mmio_observers[VIRTUAL_MAX_MMIO_OBSERVERS];
int virtual_mmio_observer_count;

int virtual_register_mmio_observer(const VirtualContext *ctx,
                                   VirtualMmioObserver observer) {
    if (!observer) {
        return -1;
    }
    if (virtual_mmio_observer_count >= VIRTUAL_MAX_MMIO_OBSERVERS) {
        virtual_log(ctx, VIRTUAL_LOG_ERROR, "too many MMIO observers (at most %d)",
                    VIRTUAL_MAX_MMIO_OBSERVERS);
        return -1;
    }
    mmio_observers[virtual_mmio_observer_count++] = observer;
    return 0;
}

void virtual_dispatch_mmio(const VirtualMmioAccess *access) {
    for (int i = 0; i < virtual_mmio_observer_count; i++) {
        mmio_observers[i](access);
    }
}

void virtual_register_tb_translation_hook(
    const VirtualContext *ctx, VirtualTbTranslationHook callback) {
    (void)ctx;
    core_register_tb_trans_hook(callback);
}

bool virtual_register_rule(const VirtualContext *ctx,
                                           uint64_t address, cb_func_t callback,
                                           const char *args) {
    (void)ctx;
    return core_register_virtual_rule(address, callback, args);
}

bool virtual_register_update(const VirtualContext *ctx,
                                     uint64_t address, int reg,
                                     uint64_t value) {
    (void)ctx;
    return core_register_register_update(address, reg, value);
}

bool virtual_register_gated_modifier(const VirtualContext *ctx,
                                             uint64_t address, const char *patch,
                                             const volatile uint8_t *enabled) {
    (void)ctx;
    return core_register_gated_modifier(address, patch, enabled);
}

void virtual_wait_for_trace_drain(const VirtualContext *ctx) {
    (void)ctx;
    core_wait_for_trace_drain();
}

int virtual_initialize_plugins(void) {
    const VirtualPlugin *plugin;
    size_t index = 0;
    int failures = 0;
    for (plugin = __start_virtual_plugins;
         plugin < __stop_virtual_plugins; ++plugin) {
        if (!plugin->name || !plugin->initialize) {
            continue;
        }
        if (index >= VIRTUAL_RUNTIME_MAX_PLUGINS) {
            fprintf(stderr, "fastdyn: too many runtime plugins\n");
            return -1;
        }
        runtime_contexts[index].plugin_name = plugin->name;
        if (plugin->initialize(&runtime_contexts[index]) < 0) {
            fprintf(stderr, "fastdyn: runtime plugin '%s' failed to initialize\n",
                    plugin->name);
            failures++;
        }
        index++;
    }
    return failures ? -1 : 0;
}

static int runtime_plugin_count(void) {
    int count = 0;
    while (count < VIRTUAL_RUNTIME_MAX_PLUGINS && runtime_contexts[count].plugin_name) {
        count++;
    }
    return count;
}

/* Load one out-of-tree plugin library; errors name the library. */
static int load_external_plugin(const char *path) {
    void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins", "cannot load plugin library %s: %s",
                    path, dlerror());
        return -1;
    }
    const VirtualExternalPlugin *plugin = dlsym(handle, "fastdyn_external_plugin");
    if (!plugin) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins",
                    "%s is not a FastDyn plugin (no fastdyn_external_plugin; see "
                    "FASTDYN_EXTERNAL_PLUGIN in fastdyn_runtime.h)", path);
        return -1;
    }
    if (plugin->abi != VIRTUAL_PLUGIN_ABI_VERSION || plugin->size < sizeof(VirtualExternalPlugin)) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins",
                    "%s was built for plugin ABI %u, but this FastDyn expects %d",
                    path, (unsigned)plugin->abi, VIRTUAL_PLUGIN_ABI_VERSION);
        return -1;
    }
    if (!plugin->name || !plugin->name[0] || !plugin->initialize) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins", "%s: its descriptor has no name or initializer", path);
        return -1;
    }
    /* A copy of libfastdyn.so other than the running one would have its own,
     * unused state; the plugin must link against the library QEMU loaded. */
    void *sdk = dlsym(handle, "virtual_register_callback");
    if (sdk && sdk != (void *)virtual_register_callback) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins",
                    "%s is linked against a different libfastdyn.so than the one running", path);
        return -1;
    }
    int index = runtime_plugin_count();
    for (int i = 0; i < index; i++) {
        if (strcmp(runtime_contexts[i].plugin_name, plugin->name) == 0) {
            fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins", "%s: a plugin named '%s' is already loaded",
                        path, plugin->name);
            return -1;
        }
    }
    if (index >= VIRTUAL_RUNTIME_MAX_PLUGINS) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins", "too many runtime plugins");
        return -1;
    }
    runtime_contexts[index].plugin_name = plugin->name;
    if (plugin->initialize(&runtime_contexts[index]) < 0) {
        fastdyn_log(FD_LOG_LEVEL_ERROR, "plugins", "plugin '%s' (%s) failed to initialize",
                    plugin->name, path);
        return -1;
    }
    fastdyn_log(FD_LOG_LEVEL_INFO, "plugins", "loaded plugin '%s' from %s", plugin->name, path);
    return 0;
}

int virtual_load_external_plugins(const char *paths) {
    if (!paths || !paths[0]) {
        return 0;
    }
    char *list = strdup(paths);
    if (!list) {
        return -1;
    }
    int status = 0;
    char *saveptr = NULL;
    for (char *path = strtok_r(list, ":", &saveptr); path; path = strtok_r(NULL, ":", &saveptr)) {
        if (load_external_plugin(path) < 0) {
            status = -1;
            break;
        }
    }
    free(list);
    return status;
}
