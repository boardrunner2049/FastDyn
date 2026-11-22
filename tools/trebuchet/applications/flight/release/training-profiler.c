/* Build against the separately installed, pinned FirmBOLT training profiler.
 * Its PC and block-count stopping modes dump the counters themselves. A mission
 * ends through FastDyn's ordinary QEMU shutdown, after the external oracle. */
#define qemu_plugin_install firmbolt_install
#include FIRMBOLT_TBPROF_SOURCE
#undef qemu_plugin_install

static void dump_completed_mission(qemu_plugin_id_t id, void *userdata)
{
    (void)id;
    (void)userdata;
    dump();
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    int result = firmbolt_install(id, info, argc, argv);
    if (result == 0) {
        qemu_plugin_register_atexit_cb(id, dump_completed_mission, NULL);
    }
    return result;
}
