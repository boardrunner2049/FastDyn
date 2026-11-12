#include <glib.h>
#include <qemu/qemu-plugin.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    int (*read)(const char *, uint64_t, uint64_t *, uint64_t);
    int (*write)(const char *, uint64_t, uint64_t, uint64_t);
} LegacyOps;

static struct qemu_plugin_register *pc_register;
static uint64_t callbacks;
static uint64_t matches;
static uint64_t invalid_contexts;
static uint64_t zero_contexts;
static uint64_t legacy_callbacks;
static bool validation_ok;

static void find_pc_register(void)
{
    GArray *registers;
    guint i;
    const char *names[] = { "rip", "pc", "r15" };
    guint name;

    if (pc_register) {
        return;
    }
    registers = qemu_plugin_get_registers();
    if (!registers) {
        return;
    }
    for (name = 0; name < G_N_ELEMENTS(names); name++) {
        for (i = 0; i < registers->len; i++) {
            qemu_plugin_reg_descriptor *rd =
                &g_array_index(registers, qemu_plugin_reg_descriptor, i);

            if (rd->name && !g_ascii_strcasecmp(rd->name, names[name])) {
                pc_register = rd->handle;
                return;
            }
        }
    }
}

static bool old_saved_pc(uint64_t *value)
{
    GByteArray *bytes;
    int length;

    find_pc_register();
    if (!pc_register) {
        return false;
    }
    bytes = g_byte_array_sized_new(sizeof(*value));
    length = qemu_plugin_read_register(pc_register, bytes);
    if (length > 0) {
        *value = 0;
        memcpy(value, bytes->data, MIN(bytes->len, sizeof(*value)));
    }
    g_byte_array_free(bytes, TRUE);
    return length > 0;
}

static void compare_context(const qemu_plugin_mmio_context *context)
{
    uint64_t old_pc = 0;
    bool match = false;

    callbacks++;
    if (!context || !(context->flags & QEMU_PLUGIN_MMIO_SAVED_PC_VALID)) {
        invalid_contexts++;
    } else if (old_saved_pc(&old_pc) && old_pc == context->saved_pc) {
        matches++;
        match = true;
        zero_contexts += context->saved_pc == 0;
    }
    if (!match && callbacks <= 3) {
        fprintf(stderr, "saved-pc mismatch old=0x%" G_GINT64_MODIFIER
                "x scalar=0x%" G_GINT64_MODIFIER "x flags=0x%x\n",
                old_pc, context ? context->saved_pc : 0,
                context ? context->flags : 0);
    }
    if (!match || !validation_ok || legacy_callbacks) {
        fputs("FAIL saved-pc equivalence callback\n", stderr);
    }
}

static int v2_read(const char *handler, uint64_t address, uint64_t *value,
                   uint64_t size, const qemu_plugin_mmio_context *context)
{
    (void)handler;
    (void)address;
    (void)value;
    (void)size;
    compare_context(context);
    return 1;
}

static int v2_write(const char *handler, uint64_t address, uint64_t value,
                    uint64_t size, const qemu_plugin_mmio_context *context)
{
    (void)handler;
    (void)address;
    (void)value;
    (void)size;
    compare_context(context);
    return 1;
}

static int legacy_read(const char *handler, uint64_t address, uint64_t *value,
                       uint64_t size)
{
    (void)handler;
    (void)address;
    (void)value;
    (void)size;
    legacy_callbacks++;
    return 1;
}

static int legacy_write(const char *handler, uint64_t address, uint64_t value,
                        uint64_t size)
{
    (void)handler;
    (void)address;
    (void)value;
    (void)size;
    legacy_callbacks++;
    return 1;
}

static void report(qemu_plugin_id_t id, void *userdata)
{
    char line[320];
    bool pass = validation_ok && callbacks && matches == callbacks &&
                !invalid_contexts && !legacy_callbacks;

    (void)id;
    (void)userdata;
    snprintf(line, sizeof(line),
             "%s saved-pc equivalence callbacks=%" G_GUINT64_FORMAT
             " matches=%" G_GUINT64_FORMAT " invalid=%" G_GUINT64_FORMAT
             " legacy=%" G_GUINT64_FORMAT " zero=%" G_GUINT64_FORMAT "\n",
             pass ? "PASS" : "FAIL", callbacks, matches, invalid_contexts,
             legacy_callbacks, zero_contexts);
    fputs(line, stderr);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    LegacyOps legacy = { legacy_read, legacy_write };
    qemu_plugin_unimp_ops_v2 valid = {
        .abi_version = QEMU_PLUGIN_UNIMP_ABI_V2,
        .struct_size = sizeof(valid),
        .read = v2_read,
        .write = v2_write,
    };
    qemu_plugin_unimp_ops_v2 invalid;

    (void)info;
    (void)argc;
    (void)argv;

    qemu_plugin_unimp_export_device(&legacy);
    validation_ok = qemu_plugin_unimp_export_device_v2(&valid);

    invalid = valid;
    invalid.abi_version++;
    validation_ok &= !qemu_plugin_unimp_export_device_v2(&invalid);
    invalid = valid;
    invalid.struct_size = sizeof(invalid) - 1;
    validation_ok &= !qemu_plugin_unimp_export_device_v2(&invalid);
    invalid = valid;
    invalid.read = NULL;
    validation_ok &= !qemu_plugin_unimp_export_device_v2(&invalid);
    invalid = valid;
    invalid.write = NULL;
    validation_ok &= !qemu_plugin_unimp_export_device_v2(&invalid);
    validation_ok &= !qemu_plugin_unimp_export_device_v2(NULL);

    qemu_plugin_register_atexit_cb(id, report, NULL);
    return 0;
}
