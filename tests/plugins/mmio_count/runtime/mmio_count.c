/* Runtime half of the out-of-tree test plugin. Built into this folder's
 * runtime/ with `fastdyn build-model --pkg fastdyn`, loaded by libfastdyn.so. */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include <fastdyn_runtime.h>

static const VirtualContext *context;
static char label[64];
static uint64_t reads_handled, reads_unhandled, writes_handled, writes_unhandled, blocks;

static void on_mmio(const VirtualMmioAccess *access) {
    if (access->is_write) {
        if (access->handled) writes_handled++; else writes_unhandled++;
    } else {
        if (access->handled) reads_handled++; else reads_unhandled++;
    }
}

static void on_translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb) {
    (void)id;
    (void)tb;
    blocks++;
}

static void write_counts(void) {
    char path[4096];
    if (virtual_artifact_path(context, "counts.txt", path, sizeof(path)) != 0) {
        return;
    }
    FILE *out = fopen(path, "w");
    if (!out) {
        return;
    }
    fprintf(out, "label=%s\nreads_handled=%" PRIu64 "\nreads_unhandled=%" PRIu64
            "\nwrites_handled=%" PRIu64 "\nwrites_unhandled=%" PRIu64
            "\ntranslated_blocks=%" PRIu64 "\n",
            label, reads_handled, reads_unhandled, writes_handled, writes_unhandled, blocks);
    fclose(out);
}

static int mmio_count_initialize(const VirtualContext *ctx) {
    char path[4096];
    FILE *settings;
    context = ctx;
    if (virtual_artifact_path(ctx, "settings.txt", path, sizeof(path)) != 0
        || !(settings = fopen(path, "r"))) {
        return 0;  /* not enabled for this run */
    }
    if (!fgets(label, sizeof(label), settings)) {
        label[0] = '\0';
    }
    fclose(settings);
    label[strcspn(label, "\n")] = '\0';
    if (virtual_register_mmio_observer(ctx, on_mmio) != 0) {
        return -1;
    }
    virtual_register_tb_translation_hook(ctx, on_translate);
    virtual_register_exit(ctx, write_counts);
    virtual_log(ctx, VIRTUAL_LOG_INFO, "counting MMIO accesses (%s)", label);
    return 0;
}

FASTDYN_EXTERNAL_PLUGIN("mmio_count", mmio_count_initialize);
