/* Automatic MMIO runtime module using the public virtual runtime SDK. */
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <fastdyn_runtime.h>
#include <mmio_opt.h>

#define MMIO_OPT_MAX_SITES 4096

typedef struct PendingSite {
    uint64_t pc;
    uint64_t address;
    unsigned transfer_size;
    unsigned instruction_size;
    char direction[8];
    bool installed;
    uint64_t hook_id;
    uint64_t fast_count;
    uint64_t fallback_count;
    const char *device_name;
    const char *reason;
} PendingSite;

static PendingSite sites[MMIO_OPT_MAX_SITES];
static size_t site_count;
static bool active;
static bool finalized;
static char report_path[4096];
static const VirtualContext *runtime_context;

static void json_string(FILE *stream, const char *text)
{
    const unsigned char *p = (const unsigned char *)(text ? text : "");
    fputc('"', stream);
    while (*p) {
        if (*p == '"' || *p == '\\') {
            fputc('\\', stream);
        }
        if (*p >= 0x20) {
            fputc(*p, stream);
        }
        p++;
    }
    fputc('"', stream);
}

static void write_report(void)
{
    FILE *stream;
    size_t i, installed = 0;
    if (!active || !report_path[0]) {
        return;
    }
    stream = fopen(report_path, "w");
    if (!stream) {
        virtual_log(runtime_context, VIRTUAL_LOG_WARN,
                    "cannot write installation report %s: %s",
                    report_path, strerror(errno));
        return;
    }
    for (i = 0; i < site_count; i++) {
        installed += sites[i].installed;
    }
    fprintf(stream, "{\n  \"enabled\": true,\n  \"prepared\": %zu,\n  \"installed\": %zu,\n",
            site_count, installed);
    fputs("  \"disabled_reason\": null,\n  \"sites\": [\n", stream);
    for (i = 0; i < site_count; i++) {
        PendingSite *site = &sites[i];
        fprintf(stream, "    {\"pc\": \"0x%" PRIx64 "\", \"address\": \"0x%" PRIx64
                "\", \"transfer_size\": %u, \"instruction_size\": %u, "
                "\"installed\": %s, \"hook_id\": %" PRIu64
                ", \"fast_count\": %" PRIu64 ", \"fallback_count\": %" PRIu64,
                site->pc, site->address, site->transfer_size,
                site->instruction_size, site->installed ? "true" : "false",
                site->hook_id, site->fast_count, site->fallback_count);
        fputs(", \"device\": ", stream);
        if (site->device_name) json_string(stream, site->device_name); else fputs("null", stream);
        fputs(", \"reason\": ", stream);
        if (site->reason) json_string(stream, site->reason); else fputs("null", stream);
        fprintf(stream, "}%s\n", i + 1 == site_count ? "" : ",");
    }
    fputs("  ]\n}\n", stream);
    fclose(stream);
}

static bool convert_expression(const FdMmioOperation *operation,
                               FastDynMmioExprInsn *out)
{
    unsigned i;
    if (!operation->expr_count || operation->expr_count > FASTDYN_MMIO_EXPR_MAX) {
        return false;
    }
    for (i = 0; i < operation->expr_count; i++) {
        const FdMmioExprInsn *src = &operation->expr[i];
        if (src->opcode < FD_MMIO_EXPR_CONST || src->opcode > FD_MMIO_EXPR_LSHR) {
            return false;
        }
        out[i] = (FastDynMmioExprInsn) {
            .opcode = src->opcode,
            .state_offset = src->state_offset,
            .value = src->value,
        };
    }
    return true;
}

static int bind_replacements(const VirtualContext *ctx)
{
    size_t i;
    if (!active || finalized) {
        return 0;
    }
    finalized = true;
    for (i = 0; i < site_count; i++) {
        PendingSite *site = &sites[i];
        FdResolvedMmioOperation resolved;
        FastDynMmioExprInsn expression[FASTDYN_MMIO_EXPR_MAX];
        FastDynMmioReplacement replacement;

        if (strcmp(site->direction, "read")) {
            site->reason = "write or unknown direction";
            continue;
        }
        if (!elder_resolve_mmio_operation(site->address, &resolved)) {
            site->reason = "no eligible operation for resolved device instance";
            continue;
        }
        if (!convert_expression(resolved.operation, expression)) {
            site->reason = "unsupported generated expression";
            continue;
        }
        replacement = (FastDynMmioReplacement) {
            .guest_pc = site->pc,
            .expected_address = site->address,
            .spec = { FASTDYN_GUEST_ISA_ARM, FASTDYN_CODE_MODE_THUMB,
                      FASTDYN_INSN_ORDER_LITTLE_ENDIAN },
            .transfer_size = site->transfer_size,
            .expr_count = resolved.operation->expr_count,
            .state_size = resolved.state_size,
            .state = resolved.state,
            .expr = expression,
            .fast_count = &site->fast_count,
            .fallback_count = &site->fallback_count,
        };
        if (!virtual_register_mmio_replacement(ctx, &replacement, &site->hook_id)) {
            site->reason = "QEMU rejected replacement";
            continue;
        }
        site->installed = true;
        site->device_name = resolved.device_name;
    }
    write_report();
    return 0;
}

static int load_plan(const VirtualContext *ctx)
{
    char plan_path[4096];
    char line[512];
    FILE *stream;

    if (virtual_artifact_path(ctx, "pending-plan.txt", plan_path,
                              sizeof(plan_path)) != 0 ||
        virtual_artifact_path(ctx, "installation.json", report_path,
                              sizeof(report_path)) != 0) {
        return -1;
    }
    stream = fopen(plan_path, "r");
    if (!stream) {
        if (errno == ENOENT) {
            return 0;
        }
        virtual_log(ctx, VIRTUAL_LOG_ERROR, "cannot open plan %s: %s",
                    plan_path, strerror(errno));
        return -1;
    }
    active = true;
    while (fgets(line, sizeof(line), stream)) {
        PendingSite *site;
        unsigned long long pc, address;
        unsigned width, instruction_size;
        char direction[8];

        if (line[0] == '#' || line[0] == '\n') {
            continue;
        }
        if (site_count == MMIO_OPT_MAX_SITES ||
            sscanf(line, "%llx %llx %u %u %7s", &pc, &address, &width,
                   &instruction_size, direction) != 5) {
            fclose(stream);
            virtual_log(ctx, VIRTUAL_LOG_ERROR, "invalid plan line: %s", line);
            return -1;
        }
        site = &sites[site_count++];
        site->pc = pc;
        site->address = address;
        site->transfer_size = width;
        site->instruction_size = instruction_size;
        snprintf(site->direction, sizeof(site->direction), "%s", direction);
    }
    fclose(stream);
    return 1;
}

static int mmio_opt_runtime_init(const VirtualContext *ctx)
{
    int loaded;
    runtime_context = ctx;
    loaded = load_plan(ctx);
    if (loaded < 0) {
        return -1;
    }
    if (!loaded) {
        return 0;
    }
    if (virtual_register_post_device_init(ctx, bind_replacements) != 0) {
        return -1;
    }
    virtual_register_exit(ctx, write_report);
    return 0;
}

VIRTUAL_PLUGIN("mmio_opt", mmio_opt_runtime_init);
