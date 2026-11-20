#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include <fastdyn_runtime.h>

typedef struct Fixture {
    const char *name;
    const uint8_t *code;
    size_t size;
    FastDynCodeSpec spec;
} Fixture;

static const uint8_t a32_code[] = {
    0xff, 0x00, 0x00, 0xe2, 0x07, 0x00, 0x80, 0xe2,
};
static const uint8_t thumb_code[] = {
    0x00, 0xf0, 0xff, 0x00, 0x00, 0xf1, 0x07, 0x00,
};
static const uint8_t a64_code[] = {
    0x00, 0x1c, 0x00, 0x12, 0x00, 0x1c, 0x00, 0x11,
};
static const uint8_t x86_64_code[] = {
    0x25, 0xff, 0x00, 0x00, 0x00, 0x83, 0xc0, 0x07,
};
static const uint8_t x86_16_code[] = {
    0x25, 0xff, 0x00, 0x83, 0xc0, 0x07,
};
static const uint8_t riscv_code[] = {
    0x13, 0x75, 0xf5, 0x0f, 0x13, 0x05, 0x75, 0x00,
};

#define LE FASTDYN_INSN_ORDER_LITTLE_ENDIAN
static const Fixture fixtures[] = {
    { "a32", a32_code, sizeof(a32_code),
      { FASTDYN_GUEST_ISA_ARM, FASTDYN_CODE_MODE_A32, LE } },
    { "thumb", thumb_code, sizeof(thumb_code),
      { FASTDYN_GUEST_ISA_ARM, FASTDYN_CODE_MODE_THUMB, LE } },
    { "a64", a64_code, sizeof(a64_code),
      { FASTDYN_GUEST_ISA_AARCH64, FASTDYN_CODE_MODE_A64, LE } },
    { "x86_16", x86_16_code, sizeof(x86_16_code),
      { FASTDYN_GUEST_ISA_X86, FASTDYN_CODE_MODE_X86_16, LE } },
    { "x86_32", x86_64_code, sizeof(x86_64_code),
      { FASTDYN_GUEST_ISA_X86, FASTDYN_CODE_MODE_X86_32, LE } },
    { "x86_64", x86_64_code, sizeof(x86_64_code),
      { FASTDYN_GUEST_ISA_X86, FASTDYN_CODE_MODE_X86_64, LE } },
    { "rv32", riscv_code, sizeof(riscv_code),
      { FASTDYN_GUEST_ISA_RISCV, FASTDYN_CODE_MODE_RV32, LE } },
    { "rv64", riscv_code, sizeof(riscv_code),
      { FASTDYN_GUEST_ISA_RISCV, FASTDYN_CODE_MODE_RV64, LE } },
};

static int initialize(const VirtualContext *ctx)
{
    char path[4096], profile[32];
    unsigned long long pc;
    FILE *settings;
    size_t i;
    uint64_t id = 0;

    if (virtual_artifact_path(ctx, "settings.txt", path, sizeof(path)) != 0 ||
        !(settings = fopen(path, "r"))) {
        return 0;
    }
    if (fscanf(settings, "%31s %llx", profile, &pc) != 2) {
        fclose(settings);
        virtual_log(ctx, VIRTUAL_LOG_ERROR, "invalid settings.txt");
        return -1;
    }
    fclose(settings);
    for (i = 0; i < sizeof(fixtures) / sizeof(fixtures[0]); i++) {
        if (!strcmp(profile, fixtures[i].name)) {
            if (!virtual_insert_before(ctx, pc, fixtures[i].code,
                                       fixtures[i].size, &fixtures[i].spec,
                                       &id)) {
                return -1;
            }
            virtual_log(ctx, VIRTUAL_LOG_INFO,
                        "registered %s fragment at 0x%llx as hook=%" PRIu64,
                        profile, pc, id);
            return 0;
        }
    }
    virtual_log(ctx, VIRTUAL_LOG_ERROR, "unknown profile '%s'", profile);
    return -1;
}

FASTDYN_EXTERNAL_PLUGIN("guest_code_fragments", initialize);
