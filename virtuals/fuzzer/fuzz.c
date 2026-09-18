#define _GNU_SOURCE
#include <dlfcn.h>
#include <utils.h>
#include <core.h>
#include <common.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <immintrin.h>

#include "virtuals.h"
#include "fuzz.h"
#include "cov_bbl.h"
#include "cov_trace.h"

#include "protocol_fuzzers/protocol_fuzzers.h"
#include "schema/schema.h"
#include "stateful_fuzzers/stateful_fuzzers.h"

static int coverage = 0;
/* Sync redirects may execute during firmware startup; they are valid only
 * after fuzz_snap_point has established the fuzzing baseline. */
static bool fuzz_snap_initialized = false;

/* Some bare-metal targets have no modeled SysTick, so their real timeout
 * handlers would otherwise never become due between restored AFLNet messages.
 * A target may hook the return of its tick function to this virtual. */
static uint32_t fuzz_virtual_time_ms;
static uint32_t fuzz_virtual_time_step_ms = 1000;
static bool fuzz_virtual_time_step_configured;

static char g_fuzzing_buf[1024];

uint32_t fuzz_prev_pc = 0;

#define CVG_PATH "fastdyn_work/cvg.bin"
#define SNAPSHOT_RAW_PATH "fastdyn_work/snapshot.bin"
#define SAVESTATE_RAW_PATH "fastdyn_work/savestate.bin"
#define SNAPSHOT_REG_COUNT 16

/* Keep these slots fixed even when a particular Cortex-M model does not
 * expose every register. This keeps snapshot files independent of optional
 * ARMv8-M features such as MSPLIM and PSPLIM. */
static const char *const fuzz_special_register_names[] = {
    "xpsr",
    "msp",
    "psp",
    "msplim",
    "psplim",
    "primask",
    "basepri",
    "faultmask",
    "control",
};

#define SNAPSHOT_SPECIAL_REG_COUNT \
    (sizeof(fuzz_special_register_names) / sizeof(fuzz_special_register_names[0]))

// Lists of consecutive address+size values, with counts measured in entries
// rather than pairs. The snapshot list is user-selected; savestate covers all
// writable ranges.
static const char *snapshot_ranges_path(void) {
    const char *path = getenv("FASTDYN_SNAPSHOT_RANGES_FILE");
    if (path && path[0]) {
        return path;
    }

    /* Keep the old override for the selected fuzzing snapshot. */
    path = getenv("FASTDYN_WRITABLE_RANGES_FILE");
    if (path && path[0]) {
        return path;
    }

    const char *work_dir = getenv("FASTDYN_WORK_DIR");
    if (work_dir && work_dir[0]) {
        static char buffer[4096];
        snprintf(buffer, sizeof(buffer), "%s/bin-writable-ranges", work_dir);
        return buffer;
    }

    return "fastdyn_work/bin-writable-ranges";
}

static const char *savestate_ranges_path(void) {
    const char *path = getenv("FASTDYN_SAVESTATE_RANGES_FILE");
    if (path && path[0]) {
        return path;
    }

    const char *work_dir = getenv("FASTDYN_WORK_DIR");
    if (work_dir && work_dir[0]) {
        static char buffer[4096];
        snprintf(buffer, sizeof(buffer), "%s/bin-all-writable-ranges", work_dir);
        return buffer;
    }

    return "fastdyn_work/bin-all-writable-ranges";
}

static const char *coverage_output_path() {
    const char *work_dir = getenv("FASTDYN_WORK_DIR");
    if (work_dir && work_dir[0]) {
        static char buffer[4096];
        snprintf(buffer, sizeof(buffer), "%s/cvg.bin", work_dir);
        return buffer;
    }

    return "./fastdyn_work/cvg.bin";
}

static const char *fuzz_state_file_path(const char *filename, char *buffer,
                                        size_t buffer_size,
                                        const char *legacy_path) {
    const char *work_dir = getenv("FASTDYN_WORK_DIR");
    int written;

    if (work_dir && work_dir[0]) {
        written = snprintf(buffer, buffer_size, "%s/%s", work_dir, filename);
        if (written >= 0 && (size_t)written < buffer_size) {
            return buffer;
        }
    }

    return legacy_path;
}

static const char *snapshot_raw_path(void) {
    static char path[4096];
    return fuzz_state_file_path("snapshot.bin", path, sizeof(path),
                                SNAPSHOT_RAW_PATH);
}

static const char *savestate_raw_path(void) {
    static char path[4096];
    return fuzz_state_file_path("savestate.bin", path, sizeof(path),
                                SAVESTATE_RAW_PATH);
}

static size_t g_snapshot_ranges_count = 0;
static uint32_t *g_snapshot_ranges = NULL;
static size_t g_savestate_ranges_count = 0;
static uint32_t *g_savestate_ranges = NULL;

static int fuzz_irq_depth = 0;

typedef enum {
    FUZZ_MSG_INACTIVE = 0,
    FUZZ_MSG_READY,
    FUZZ_MSG_CONSUMING,
    FUZZ_MSG_CONSUMED,
} fuzz_msg_state_t;

static _Atomic int g_active_msg_state = FUZZ_MSG_INACTIVE;
static const uint8_t *g_active_msg_data = NULL;
static size_t g_active_msg_len = 0;

/* Some protocols finish their reply from an IRQ.  An AFLNet exchange must not
 * happen there: the next request has to start at thread level after the IRQ
 * has returned.  The model records that completed boundary and the configured
 * thread-level virtual consumes it. */
static _Atomic bool g_fuzz_sync_requested = false;
static _Atomic uint32_t g_fuzz_sync_resume_pc = 0;

extern bool g_trace_enabled;

extern uint8_t CVG[MAP_SIZE];
extern AddressList core_cc_list; // in future, update so that we aren't using an extern global for this, maybe add core.c helper to consume inputs

static fuzz_callback_t g_fuzz_snap_callback = NULL;
static fuzz_callback_t g_fuzz_input_callback = NULL;
static fuzz_callback_t g_fuzz_sync_callback = NULL;
static fuzz_callback_t g_fuzz_exit_callback = NULL;
static fuzz_callback_t g_fuzz_restore_callback = NULL;

void fuzz_register_snap_callback(fuzz_callback_t cb) {
    g_fuzz_snap_callback = cb;
}

void fuzz_register_callback(fuzz_callback_t cb) {
    g_fuzz_sync_callback = cb;
}

void fuzz_register_input_callback(fuzz_callback_t cb) {
    g_fuzz_input_callback = cb;
}

void fuzz_register_exit(fuzz_callback_t cb) {
    g_fuzz_exit_callback = cb;
}

void fuzz_register_restore(fuzz_callback_t cb) {
    g_fuzz_restore_callback = cb;
}

// method could be better, but since we do this in a hook the firmware isn't executing and is safe
static void fuzz_irq_entry(int irq) {
    core_cc_list.log_buf.buffer[(uint16_t)core_cc_list.log_buf.index / 4] = 0xFFFFFFFD;
    core_cc_list.log_buf.index = (uint16_t)(core_cc_list.log_buf.index + 4);
}
static void fuzz_irq_exit(int irq) {
    core_cc_list.log_buf.buffer[(uint16_t)core_cc_list.log_buf.index / 4] = 0xFFFFFFFB;
    core_cc_list.log_buf.index = (uint16_t)(core_cc_list.log_buf.index + 4);
}

// wait for tracer in core to catch up with inline pc logger
static void fuzz_sync_coverage(void) {
    core_wait_for_trace_drain();
    fuzz_prev_pc = 0;
    fuzz_bbl_reset_trace();
}

// ----------------------- data getting & setting -----------------------------

// wait if handle is consuming, raturn state
static int fuzz_active_message_state(void)
{
    int state;

    do {
        state = atomic_load_explicit(&g_active_msg_state, memory_order_acquire);
        if (state == FUZZ_MSG_CONSUMING) {
            _mm_pause();
        }
    } while (state == FUZZ_MSG_CONSUMING);

    return state;
}

static fuzz_msg_state_t fuzz_deactivate_message(void)
{
    (void)fuzz_active_message_state();
    //g_active_msg_data = NULL;
    //g_active_msg_len = 0;
    return atomic_exchange_explicit(&g_active_msg_state, FUZZ_MSG_INACTIVE, memory_order_acq_rel);
}

static void fuzz_set_message_state(fuzz_msg_state_t state)
{
    (void)fuzz_active_message_state();
    atomic_store_explicit(&g_active_msg_state, state, memory_order_release);
}

static void fuzz_activate_message(const fuzz_backend_msg_t *msg)
{
    (void)fuzz_active_message_state();
    g_active_msg_data = msg->data;
    g_active_msg_len = msg->len;
    atomic_store_explicit(&g_active_msg_state, FUZZ_MSG_READY, memory_order_release);
}

bool fuzz_take_input(uint8_t **data, size_t *len)
{
    int expected = FUZZ_MSG_READY;
    uint8_t *copy;
    size_t copy_len;

    if (data == NULL || len == NULL) {
        return false;
    }
    *data = NULL;
    *len = 0;

    // essentially an atomic 'if (g_active_msg_state == expected) {g_active_msg_state = FUZZ_MSG_CONSUMING} else return 0'
    if (!atomic_compare_exchange_strong_explicit(
            &g_active_msg_state,
            &expected,
            FUZZ_MSG_CONSUMING,
            memory_order_acq_rel,
            memory_order_acquire)) {
        return false;
    }

    copy_len = g_active_msg_len;
    if (copy_len != 0 && g_active_msg_data == NULL) {
        atomic_store_explicit(&g_active_msg_state, FUZZ_MSG_READY,
                              memory_order_release);
        return false;
    }

    copy = malloc(copy_len == 0 ? 1U : copy_len);
    if (copy == NULL) {
        atomic_store_explicit(&g_active_msg_state, FUZZ_MSG_READY,
                              memory_order_release);
        return false;
    }
    if (copy_len != 0) {
        memcpy(copy, g_active_msg_data, copy_len);
    }

    atomic_store_explicit(&g_active_msg_state, FUZZ_MSG_CONSUMED, memory_order_release);
    *data = copy;
    *len = copy_len;
    return true;
}

size_t fuzz_get_data(char* buf, size_t len)
{
    uint8_t *data;
    size_t data_len;
    size_t copy_len;

    if (buf == NULL || len == 0 || !fuzz_take_input(&data, &data_len)) {
        return 0;
    }

    copy_len = data_len < len ? data_len : len;
    if (copy_len != 0) {
        memcpy(buf, data, copy_len);
    }
    free(data);
    return copy_len;
}

void fuzz_set_data(char* buf, size_t len)
{
    fuzz_backend_set_data((const uint8_t *)buf, len);
}

// ------------------- snapshotting related functions ------------------------

// Load address/size pairs produced by binary_wrange.py. Manual edits to the
// selected file can improve fuzz snapshots without reducing savestate coverage.
static uint32_t *fuzz_get_writable_ranges(const char *filename, size_t *out_count)
{
    FILE *f = fopen(filename, "r");
    if (!f) {
        perror("fopen");
        return NULL;
    }

    size_t capacity = 16;          // initial number of uint32_t entries
    size_t count = 0;
    uint32_t *array = malloc(capacity * sizeof(uint32_t));
    if (!array) {
        fclose(f);
        return NULL;
    }

    uint32_t a, b;

    while (fscanf(f, "%x\t%x", &a, &b) == 2) {
        if (count + 2 > capacity) {
            capacity *= 2;
            uint32_t *tmp = realloc(array, capacity * sizeof(uint32_t));
            if (!tmp) {
                free(array);
                fclose(f);
                return NULL;
            }
            array = tmp;
        }

        array[count++] = a;
        array[count++] = b;
    }

    fclose(f);

    *out_count = count;   // number of uint32_t entries (not pairs!)
    return array;
}

typedef struct {
    const uint32_t *pairs;
    size_t count;
} fuzz_memory_ranges_t;

typedef enum {
    FUZZ_REGISTERS_BASE,
    FUZZ_REGISTERS_BASE_AND_SPECIAL,
} fuzz_register_profile_t;

typedef struct {
    const char *name;
    const char *(*path)(void);
    fuzz_register_profile_t registers;
    fuzz_memory_ranges_t (*memory_ranges)(void);
} fuzz_state_policy_t;

typedef struct {
    const fuzz_state_policy_t *policy;
    uint32_t *ranges;
    size_t range_count;
    uint8_t *membuff;
    uint32_t regs[SNAPSHOT_REG_COUNT];
    uint32_t special_regs[SNAPSHOT_SPECIAL_REG_COUNT];
    bool loaded_from_file;
    bool taken;
} fuzz_saved_state_t;

static size_t fuzz_memory_ranges_size(const uint32_t *ranges, size_t count)
{
    size_t total_mem = 0;

    for (size_t i = 1; i < count; i += 2) {
        total_mem += ranges[i];
    }

    return total_mem;
}

/* Policy boundaries for the two restore points. The range generator writes
 * the complete writable set for savestate and a user-selected subset for the
 * fuzzing snapshot. */
static fuzz_memory_ranges_t fuzz_savestate_memory_ranges(void)
{
    /* Additional explicit savestate ranges will be appended here. */
    return (fuzz_memory_ranges_t) {
        .pairs = g_savestate_ranges,
        .count = g_savestate_ranges_count,
    };
}

static fuzz_memory_ranges_t fuzz_snapshot_memory_ranges(void)
{
    return (fuzz_memory_ranges_t) {
        .pairs = g_snapshot_ranges,
        .count = g_snapshot_ranges_count,
    };
}

static const fuzz_state_policy_t g_savestate_policy = {
    .name = "savestate",
    .path = savestate_raw_path,
    .registers = FUZZ_REGISTERS_BASE_AND_SPECIAL,
    .memory_ranges = fuzz_savestate_memory_ranges,
};

static const fuzz_state_policy_t g_fuzz_snapshot_policy = {
    .name = "snapshot",
    .path = snapshot_raw_path,
    .registers = FUZZ_REGISTERS_BASE,
    .memory_ranges = fuzz_snapshot_memory_ranges,
};

static fuzz_saved_state_t g_savestate = {
    .policy = &g_savestate_policy,
};

static fuzz_saved_state_t g_fuzz_snapshot = {
    .policy = &g_fuzz_snapshot_policy,
};

static bool fuzz_state_has_special_registers(const fuzz_saved_state_t *state)
{
    return state->policy->registers == FUZZ_REGISTERS_BASE_AND_SPECIAL;
}

static bool fuzz_state_select_memory_ranges(fuzz_saved_state_t *state)
{
    fuzz_memory_ranges_t ranges = state->policy->memory_ranges();

    if ((ranges.count & 1) || (ranges.count != 0 && !ranges.pairs)) {
        return false;
    }

    if (ranges.count == 0) {
        return true;
    }

    state->ranges = malloc(ranges.count * sizeof(*state->ranges));
    if (!state->ranges) {
        return false;
    }

    memcpy(state->ranges, ranges.pairs, ranges.count * sizeof(*state->ranges));
    state->range_count = ranges.count;
    return true;
}

static qemu_plugin_reg_descriptor *fuzz_find_register(GArray *registers,
                                                       const char *name)
{
    for (guint i = 0; i < registers->len; i++) {
        qemu_plugin_reg_descriptor *descriptor = &g_array_index(
            registers, qemu_plugin_reg_descriptor, i);

        if (strcmp(descriptor->name, name) == 0) {
            return descriptor;
        }
    }

    return NULL;
}

/* Save the current Cortex-M special-register state into a caller-provided
 * fixed-size buffer. Registers not implemented by this CPU are left as zero. */
static bool fuzz_save_special_registers(uint32_t *buffer)
{
    GArray *registers = qemu_plugin_get_registers();
    GByteArray *value = g_byte_array_sized_new(sizeof(uint32_t));
    bool success = true;

    if (!registers || !value) {
        if (registers) {
            g_array_free(registers, true);
        }
        if (value) {
            g_byte_array_free(value, true);
        }
        return false;
    }

    memset(buffer, 0, sizeof(uint32_t) * SNAPSHOT_SPECIAL_REG_COUNT);

    for (size_t i = 0; i < SNAPSHOT_SPECIAL_REG_COUNT; i++) {
        qemu_plugin_reg_descriptor *descriptor = fuzz_find_register(
            registers, fuzz_special_register_names[i]);
        int count;

        if (!descriptor) {
            continue;
        }

        g_byte_array_set_size(value, 0);
        count = qemu_plugin_read_register(descriptor->handle, value);
        if (count != sizeof(uint32_t) || value->len != sizeof(uint32_t)) {
            printf("[savestate] Failed to read special register %s\n",
                   fuzz_special_register_names[i]);
            success = false;
            break;
        }

        /* Cortex-M register data is little-endian target byte order. */
        buffer[i] = (uint32_t)value->data[0] |
                    ((uint32_t)value->data[1] << 8) |
                    ((uint32_t)value->data[2] << 16) |
                    ((uint32_t)value->data[3] << 24);
    }

    g_byte_array_free(value, true);
    g_array_free(registers, true);
    return success;
}

static bool fuzz_restore_special_registers(const uint32_t *buffer)
{
#if 0
    /*
     * The currently selected QEMU build does not export
     * qemu_plugin_write_register().  Leave this implementation here so it
     * can be re-enabled with the matching FastDyn-QEMU API, but do not leave
     * an unresolved plugin symbol in builds for stock QEMU.
     */
    GArray *registers = qemu_plugin_get_registers();
    bool success = true;

    if (!registers) {
        return false;
    }

    for (size_t i = 0; i < SNAPSHOT_SPECIAL_REG_COUNT; i++) {
        qemu_plugin_reg_descriptor *descriptor = fuzz_find_register(
            registers, fuzz_special_register_names[i]);
        uint8_t value[sizeof(uint32_t)] = {
            buffer[i] & 0xff,
            (buffer[i] >> 8) & 0xff,
            (buffer[i] >> 16) & 0xff,
            (buffer[i] >> 24) & 0xff,
        };

        if (!descriptor) {
            continue;
        }

        if (qemu_plugin_write_register(descriptor->handle, value) !=
            sizeof(uint32_t)) {
            printf("[savestate] Failed to restore special register %s\n",
                   fuzz_special_register_names[i]);
            success = false;
            break;
        }
    }

    g_array_free(registers, true);
    return success;
#else
    /* Special registers cannot be restored without the FastDyn QEMU API. */
    (void)buffer;
    return true;
#endif
}

static void fuzz_state_discard(fuzz_saved_state_t *state)
{
    free(state->membuff);
    free(state->ranges);
    state->membuff = NULL;
    state->ranges = NULL;
    state->range_count = 0;
}

/* Capture one policy-defined state, loading an existing raw image when one is
 * available. The raw file layout remains compatible with the prior format. */
static int fuzz_state_take(fuzz_saved_state_t *state)
{
    uint32_t file_regs[SNAPSHOT_REG_COUNT];
    uint32_t file_special_regs[SNAPSHOT_SPECIAL_REG_COUNT];
    bool special_regs = fuzz_state_has_special_registers(state);
    size_t total_mem;
    FILE *f;
    const char *path = state->policy->path();

    state->loaded_from_file = false;
    if (!fuzz_state_select_memory_ranges(state)) {
        printf("[%s] Invalid memory ranges\n", state->policy->name);
        return -1;
    }

    total_mem = fuzz_memory_ranges_size(state->ranges, state->range_count);
    state->membuff = malloc(total_mem ? total_mem : 1);
    if (!state->membuff) {
        printf("[%s] Failed to allocate memory buffer\n", state->policy->name);
        fuzz_state_discard(state);
        return -1;
    }

    f = fopen(path, "rb");
    if (f) {
        if (fread(file_regs, sizeof(file_regs[0]), SNAPSHOT_REG_COUNT, f) == SNAPSHOT_REG_COUNT &&
            (!special_regs ||
             fread(file_special_regs, sizeof(file_special_regs[0]),
                   SNAPSHOT_SPECIAL_REG_COUNT, f) == SNAPSHOT_SPECIAL_REG_COUNT) &&
            fread(state->membuff, 1, total_mem, f) == total_mem && fgetc(f) == EOF) {
            memcpy(state->regs, file_regs, sizeof(state->regs));
            if (special_regs) {
                memcpy(state->special_regs, file_special_regs,
                       sizeof(state->special_regs));
            }
            state->loaded_from_file = true;
            fclose(f);
            printf("Loaded %s from %s\n", state->policy->name, path);
            return 0;
        }

        fclose(f);
        printf("Ignoring invalid %s dump at %s\n",
               state->policy->name, path);
    }

    for (int i = 0; i < SNAPSHOT_REG_COUNT; i++) {
        state->regs[i] = qemu_get_register(i);
    }

    if (special_regs && !fuzz_save_special_registers(state->special_regs)) {
        fuzz_state_discard(state);
        return -1;
    }

    size_t offset = 0;
    for (size_t i = 0; i < state->range_count; i += 2) {
        qemu_plugin_read_memory(state->ranges[i], state->membuff + offset,
                                state->ranges[i + 1]);
        offset += state->ranges[i + 1];
    }

    f = fopen(path, "wb");
    if (f) {
        if (fwrite(state->regs, sizeof(state->regs[0]), SNAPSHOT_REG_COUNT, f) != SNAPSHOT_REG_COUNT) {
            printf("Failed to write %s register header to %s\n",
                   state->policy->name, path);
        } else if (special_regs &&
                   fwrite(state->special_regs, sizeof(state->special_regs[0]),
                         SNAPSHOT_SPECIAL_REG_COUNT, f) != SNAPSHOT_SPECIAL_REG_COUNT) {
            printf("Failed to write %s special-register header to %s\n",
                   state->policy->name, path);
        } else if (fwrite(state->membuff, 1, total_mem, f) != total_mem) {
            printf("Failed to write full %s to %s\n",
                   state->policy->name, path);
        } else {
            printf("Wrote %s to %s\n", state->policy->name, path);
        }
        fclose(f);
    } else {
        printf("Failed to open %s for %s dump\n",
               path, state->policy->name);
    }

    return 0;
}

static bool fuzz_state_ensure(fuzz_saved_state_t *state)
{
    if (state->taken) {
        return true;
    }

    if (fuzz_state_take(state) != 0) {
        perror("Failed to take initial state");
        return false;
    }

    state->taken = true;
    return true;
}

static bool fuzz_state_restore(fuzz_saved_state_t *state)
{
    if (!fuzz_state_ensure(state) || !state->membuff) {
        return false;
    }

    size_t offset = 0;
    for (size_t i = 0; i < state->range_count; i += 2) {
        qemu_plugin_write_memory(state->ranges[i], state->membuff + offset,
                                 state->ranges[i + 1]);
        offset += state->ranges[i + 1];
    }

    /* Snapshot and savestate currently share the base-register restore set.
     * PC is intentionally excluded because each restore point resumes through
     * its hook rather than by jumping back to the captured instruction. */
    for (int i = 0; i < 15; i++) {
        fuzz_set_register(state->regs[i], i);
    }

    if (fuzz_state_has_special_registers(state) &&
        !fuzz_restore_special_registers(state->special_regs)) {
        return false;
    }

    return true;
}

static bool fuzz_savestate_ensure(void)
{
    return fuzz_state_ensure(&g_savestate);
}

static bool fuzz_savestate_restore(void)
{
    return fuzz_state_restore(&g_savestate);
}

static bool fuzz_snapshot_ensure(void)
{
    return fuzz_state_ensure(&g_fuzz_snapshot);
}

bool fuzz_restore_snapshot(void)
{
    if (!fuzz_state_restore(&g_fuzz_snapshot)) {
        return false;
    }

    /* The guest snapshot contains its timeout queues, so its deterministic
     * coverage clock must start from the same origin. Keep time advancing
     * across the messages of one AFLNet testcase, but never carry it into the
     * next restored testcase. */
    fuzz_virtual_time_ms = 0;

    if (g_fuzz_restore_callback) {
        g_fuzz_restore_callback();
    }

    return true;
}

// ------------ assert and sync virtuals ----------------

static void virt_assert(unsigned int cpu_index, void *udata)
{
    if (!coverage) {
        utils_die("[assert] Coverage not enabled, cannot assert coverage data");
    }

    if (!udata)
        return;

    printf("[assert] Asserted\n");

    const char *str = (const char *)udata;

    // Expect something like "*0x8003940"
    if (str[0] != '*') {
        fprintf(stderr, "[assert] Invalid format: %s\n", str);
        return;
    }

    // Skip the '*' and parse the rest as hex or decimal
    uint64_t addr = strtoull(str + 1, NULL, 0);
    if (addr != 0) { // set pc to supplied address
        fuzz_backend_report_assert(false);
        qemu_set_register(addr, ARM_V7M_PC);
    } else { // for address == 0, perform a reset
        uint32_t msp = qemu_get_register(13);

        if (msp != 0) {
            qemu_plugin_read_memory(msp, (uint8_t*)g_fuzzing_buf, sizeof(uint32_t) * 8);

            printf("[assert] Hard fault:\n");
            for (int i = 0; i < 8; i++) {
                printf("[assert] Stack[%d] = %x\n", i, ((uint32_t*)g_fuzzing_buf)[i]);
            }
            for (int i = 0; i < 16; i++) {
                printf("[assert] r%d = %x\n", i, (uint32_t)qemu_get_register(i));
            }
        }

        fuzz_sync_coverage();
        fuzz_backend_report_assert(true);

        // wait for fuzzer to observe the crash
        while (true);
    }
}

static inline void observed_clear() {
    fuzz_prev_pc = 0;
    memset(CVG, 0, sizeof(CVG));
}

static inline bool fuzz_snap_init_timeout(int seconds) {
    static bool started = false;
    static struct timespec start_time;
    struct timespec now;

    if (seconds <= 0) {
        return true;
    }

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        perror("clock_gettime");
        return true;
    }

    if (!started) {
        start_time = now;
        started = true;
        return false;
    }

    time_t elapsed_sec = now.tv_sec - start_time.tv_sec;
    long elapsed_nsec = now.tv_nsec - start_time.tv_nsec;

    if (elapsed_nsec < 0) {
        elapsed_sec--;
        elapsed_nsec += 1000000000L;
    }

    return elapsed_sec > seconds || (elapsed_sec == seconds && elapsed_nsec >= 0);
}

static void fuzz_state_point(unsigned int cpu_index, void *udata)
{
    (void)cpu_index;
    (void)udata;

    static bool state_init = false;
    if (state_init) return;
    
    if (!fuzz_savestate_ensure()) {
        utils_die("[state] Failed to take savestate snapshot");
    }
    if (g_savestate.loaded_from_file && !fuzz_savestate_restore()) {
        utils_die("[state] Failed to restore savestate snapshot");
    }

    state_init = true;
}

static void fuzz_advance_time(unsigned int cpu_index, void *udata)
{
    (void)cpu_index;
    (void)udata;

    if (!fuzz_snap_initialized) {
        return;
    }

    if (!fuzz_virtual_time_step_configured) {
        const char *configured_step = getenv("FASTDYN_FUZZ_TIME_STEP_MS");
        if (configured_step && configured_step[0]) {
            char *end = NULL;
            unsigned long value = strtoul(configured_step, &end, 10);
            if (end != configured_step && *end == '\0' && value > 0 &&
                value <= UINT32_MAX) {
                fuzz_virtual_time_step_ms = (uint32_t)value;
            }
        }
        fuzz_virtual_time_step_configured = true;
    }

    fuzz_virtual_time_ms += fuzz_virtual_time_step_ms;
    qemu_set_register(fuzz_virtual_time_ms, ARM_V7M_R0);
}

static void fuzz_sync_point(unsigned int cpu_index, void *udata);

void fuzz_request_sync(uint32_t resume_pc)
{
    atomic_store_explicit(&g_fuzz_sync_resume_pc, resume_pc,
                          memory_order_relaxed);
    atomic_store_explicit(&g_fuzz_sync_requested, true, memory_order_release);
}

static bool fuzz_take_sync_request(uint32_t *resume_pc)
{
    if (!atomic_exchange_explicit(&g_fuzz_sync_requested, false,
                                  memory_order_acq_rel)) {
        return false;
    }
    if (resume_pc != NULL) {
        *resume_pc = atomic_load_explicit(&g_fuzz_sync_resume_pc,
                                          memory_order_relaxed);
    }
    return true;
}

static void fuzz_snap_point(unsigned int cpu_index, void *udata)
{
    // optionally wait x seconds after first hook to get to post initialization in some firmwares
    if (!fuzz_snap_initialized && fuzz_snap_init_timeout(0)) {
        fuzz_irq_depth = 0; // fix any drift after jumping
        if (!fuzz_snapshot_ensure()) {
            utils_die("[sync] Failed to take initial snapshot");
        }

        if (g_fuzz_snapshot.loaded_from_file && !fuzz_restore_snapshot()) {
            utils_die("[sync] Failed to restore loaded snapshot");
        }

        schema_activate_post_snapshot_modifiers();

        observed_clear();

        fuzz_snap_initialized = true;

        fuzz_sync_point(cpu_index, udata);
    }

    /* A model may mark its response complete from an IRQ.  Consume that
     * request only after normal control flow has unwound to the top-level
     * loop, where snapshot restoration and the next input begin cleanly. */
    if (fuzz_snap_initialized && fuzz_take_sync_request(NULL)) {
        fuzz_sync_point(cpu_index, udata);
    }

}

/* Stateful targets commonly return to the snapshot instruction at the end of
 * every request loop.  Take the baseline and obtain the first message there,
 * then treat each later visit as the completion boundary of the preceding
 * message.  Keeping this distinct from fuzz_snap_point preserves the latter's
 * historical one-shot behaviour for existing stateless targets. */
static void fuzz_snap_sync_point(unsigned int cpu_index, void *udata)
{
    if (!fuzz_snap_initialized) {
        fuzz_snap_point(cpu_index, udata);
        return;
    }

    fuzz_sync_point(cpu_index, udata);
}

static void fuzz_sync_point(unsigned int cpu_index, void *udata)
{
    /* A virtual rule's userdata is optional.  Schema-owned flow rules are
     * registered at runtime, and QEMU may normalize an empty argument string
     * to NULL.  Reaching the registered sync point is the activation signal. */
    (void)udata;

    if (!fuzz_snap_initialized) {
        return;
    }
    if (!coverage) {
        utils_die("[fuzz_sync] Coverage not enabled, cannot assert coverage data");
    }

    static bool clear_next = false;

    fuzz_msg_state_t oldState = fuzz_deactivate_message();

    fuzz_sync_coverage();

    /* Restoring a snapshot can leave the inline IRQ tracker at a stale depth.
     * Start each new input at thread level so coverage remains observable. */
    fuzz_irq_depth = 0;

    if (g_fuzz_sync_callback) {
        g_fuzz_sync_callback();
    }

    // not every response is input producing
    while (true) {
        if (oldState == FUZZ_MSG_READY) {
            fuzz_set_message_state(oldState); // reactivate the message
            break;
        }

        fuzz_backend_msg_t msg = {0};
        if (!fuzz_backend_next(&msg)) {
            utils_die("[fuzz_sync] Backend failed to produce input");
        }

        if (msg.restore) {
            if (!fuzz_restore_snapshot()) {
                utils_die("[fuzz_sync] Failed to restore snapshot");
            }

            clear_next = true;

            fuzz_backend_restore_complete();
        }

        // fuzzer can request just a restore without a message
        if (msg.data) {
            // start message clean
            if (clear_next) {
                observed_clear();
                clear_next = false;
            }

            fuzz_activate_message(&msg);

            if (g_fuzz_input_callback) {
                g_fuzz_input_callback();
            }
            break;
        }
    }

    /* Inject only after the message is active.  A restored guest can revisit
     * the snap address, so injecting from fuzz_snap_point would consume the
     * same message at the wrong lifecycle point. */
    if (g_fuzz_snap_callback) {
        g_fuzz_snap_callback();
    }
}

uint32_t fuzz_get_register(int reg) {
    return qemu_get_register(reg);
}
void fuzz_set_register(uint32_t value, int reg) {
    qemu_set_register(value, reg);
}

int fuzz_write_memory(unsigned long long addr, uint8_t *mem_buf, int len) {
    return qemu_plugin_write_memory(addr, mem_buf, len);
}
int fuzz_read_memory(unsigned long long addr, uint8_t *mem_buf, int len) {
    return qemu_plugin_read_memory(addr, mem_buf, len);
}

void fuzz_add_observed_value(uint32_t val) {
    
    if (val == 0xFFFFFFFD) {
        fuzz_irq_depth++;
        //printf("++IRQ == %d\n", fuzz_irq_depth);
    } else if (val == 0xFFFFFFFB) {
        fuzz_irq_depth--;
        //printf("--IRQ == %d\n", fuzz_irq_depth);
    } else {
        if (fuzz_irq_depth == 0 || false) {
            // we don't want stale value from before trace
            if (fuzz_prev_pc != 0) {
                uint32_t idx = (fuzz_prev_pc ^ val) % MAP_SIZE;
                CVG[idx] = CVG[idx] + 1;
            }

            fuzz_prev_pc = val;
        }
        fuzz_bbl_add(val, fuzz_irq_depth);
    }
    if (g_trace_enabled) fuzz_trace_record_pc(val);
}

static void fuzz_serialize_coverage(const char *filename) {
    char tmp[512];

    /* Construct temp filename in same directory */
    snprintf(tmp, sizeof(tmp), "%s.tmp", filename);

    FILE *f = fopen(tmp, "wb");
    if (!f) {
        perror("[-] Failed to open temp coverage file for writing");
        return;
    }

    // comma separated values
    for (size_t i = 0; i < MAP_SIZE; i++) {
        if (fprintf(f, "%u", CVG[i]) < 0) {
            perror("[-] Write error");
            fclose(f);
            remove(tmp);
            return;
        }
        if (i < MAP_SIZE - 1) {
            fputc(',', f);
        }
    }
    fputc('\n', f);

    /* Ensure data is flushed to disk before rename */
    fflush(f);
    fsync(fileno(f));
    fclose(f);

    /* Atomic rename replaces old file */
    if (rename(tmp, filename) != 0) {
        perror("[-] Failed to rename temp coverage file");
        remove(tmp);
    }
}

static void fuzz_destroy(void) {
    if (g_fuzz_exit_callback) {
        g_fuzz_exit_callback();
    }
    fuzz_dump_bbl();
    fuzz_serialize_coverage(coverage_output_path());
}

int fuzz_init(int argc, char **argv) {
    const char *filename = utils_get_arg("coverage", argc, argv);
    const char *fuzzing = utils_get_arg("fuzzing", argc, argv);
    if (filename && (strcasecmp(filename, "true") == 0 || strcmp(filename, "1") == 0)) {
        coverage = 1;
        fuzz_bbl_init(argc, argv);
        core_register_exit_hook(fuzz_destroy);
    } else {
        printf("Coverage is required to fuzz\n");
        return 0;
    }

    core_register_irq_hook(fuzz_irq_entry, fuzz_irq_exit);

    g_savestate_ranges = fuzz_get_writable_ranges(
        savestate_ranges_path(), &g_savestate_ranges_count);
    if (g_savestate_ranges == NULL || (g_savestate_ranges_count & 1)) {
        utils_die("[state] Couldn't parse all writable memory definitions");
    }

    g_snapshot_ranges = fuzz_get_writable_ranges(
        snapshot_ranges_path(), &g_snapshot_ranges_count);
    if (g_snapshot_ranges == NULL || (g_snapshot_ranges_count & 1)) {
        utils_die("[sync] Couldn't parse selected writable memory definitions");
    }

    fuzz_deactivate_message();

    virtual_register("assert", virt_assert);
    virtual_register("fuzz_advance_time", fuzz_advance_time);
    virtual_register("fuzz_snap_point", fuzz_snap_point);
    virtual_register("fuzz_snap_sync_point", fuzz_snap_sync_point);
    virtual_register("fuzz_state_point", fuzz_state_point);
    virtual_register("fuzz_sync_point", fuzz_sync_point);

    // This is where the stateless injection harness should go
    generic_configure(argc, argv);
    fuzz_register_snap_callback(generic_callback);

    if (fuzzing && (strcasecmp(fuzzing, "true") == 0 || strcmp(fuzzing, "1") == 0)) {
        if (!fuzz_backend_init()) {
            utils_die("[sync] Failed backend initialization");
        }
    }

    return 0;
}
