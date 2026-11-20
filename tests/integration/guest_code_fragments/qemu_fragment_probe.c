#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <qemu/qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

static uint64_t account_pc;
static uint64_t account_handles;
static uint64_t account_size;
static bool late_rejected;

static void account_translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    (void)id;
    for (size_t i = 0; i < qemu_plugin_tb_n_insns(tb); i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        if (qemu_plugin_insn_vaddr(insn) == account_pc) {
            account_handles++;
            account_size = qemu_plugin_insn_size(insn);
        }
    }
}

static void account_exit(qemu_plugin_id_t id, void *opaque)
{
    (void)id;
    (void)opaque;
    fprintf(stderr, "FASTDYN_ACCOUNT original_handles=%" PRIu64
            " original_size=%" PRIu64 "\n",
            account_handles, account_size);
}

static void late_translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    static bool attempted;
    static const uint8_t add[] = { 0x83, 0xc0, 0x01 };
    static const FastDynCodeSpec spec = { FASTDYN_GUEST_ISA_X86,
        FASTDYN_CODE_MODE_X86_32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
    uint64_t hook = 99;
    (void)id;
    (void)tb;
    if (!attempted) {
        attempted = true;
        late_rejected = !qemu_plugin_register_fastdyn_fragment(
            0x123456, add, sizeof(add), &spec, &hook) && hook == 0;
    }
}

static void late_exit(qemu_plugin_id_t id, void *opaque)
{
    (void)id;
    (void)opaque;
    fprintf(stderr, "FASTDYN_LATE_REGISTRATION rejected=%d\n", late_rejected);
}

static bool registry_validation(void)
{
    static uint8_t large[2049];
    static uint8_t count129[129 * 3];
    static uint8_t count64[64 * 3];
    static const uint8_t add[] = { 0x83, 0xc0, 0x01 };
    static const uint8_t truncated[] = { 0x83, 0xc0 };
    static const uint8_t forbidden_tail[] = {
        0x83, 0xc0, 0x01, 0x8b, 0x00,
    };
    static const uint8_t a32_add[] = { 0x01, 0x00, 0x80, 0xe2 };
    FastDynCodeSpec x86 = { FASTDYN_GUEST_ISA_X86,
        FASTDYN_CODE_MODE_X86_32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
    FastDynCodeSpec bad = { FASTDYN_GUEST_ISA_INVALID,
        FASTDYN_CODE_MODE_X86_32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
    FastDynCodeSpec a32 = { FASTDYN_GUEST_ISA_ARM,
        FASTDYN_CODE_MODE_A32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
    uint64_t hook = 99, first = 0, second = 0;

    for (size_t i = 0; i < 129; i++) {
        memcpy(count129 + i * 3, add, 3);
    }
    memcpy(count64, count129, sizeof(count64));
    memset(large, 0x66, sizeof(large));

    if (qemu_plugin_register_fastdyn_fragment(0, NULL, 1, &x86, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0, add, 0, &x86, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0, add, sizeof(add), &bad, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0, large, sizeof(large), &x86, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0, count129, sizeof(count129), &x86, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0, truncated,
            sizeof(truncated), &x86, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0, forbidden_tail,
            sizeof(forbidden_tail), &x86, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(2, a32_add,
            sizeof(a32_add), &a32, &hook) || hook) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(UINT64_C(0x100000000), a32_add,
            sizeof(a32_add), &a32, &hook) || hook) {
        return false;
    }
    /* Odd x86 instruction addresses are valid. */
    if (!qemu_plugin_register_fastdyn_fragment(0x123451, add, sizeof(add),
                                                &x86, &hook) || !hook) {
        return false;
    }
    /* Two fragments may exactly reach 128; another must fail atomically. */
    if (!qemu_plugin_register_fastdyn_fragment(0x123460, count64,
            sizeof(count64), &x86, &first) || !first ||
        !qemu_plugin_register_fastdyn_fragment(0x123460, count64,
            sizeof(count64), &x86, &second) || !second) {
        return false;
    }
    hook = 99;
    if (qemu_plugin_register_fastdyn_fragment(0x123460, add, sizeof(add),
                                               &x86, &hook) || hook) {
        return false;
    }
    return true;
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    static const uint8_t x86_16[] = { 0x25, 0xff, 0x00, 0x83, 0xc0, 0x07 };
    static const uint8_t a64[] = {
        0x00, 0x1c, 0x00, 0x12, 0x00, 0x1c, 0x00, 0x11,
    };
    static const uint8_t a32[] = {
        0xff, 0x00, 0x00, 0xe2, 0x07, 0x00, 0x80, 0xe2,
    };
    static const uint8_t thumb[] = {
        0x00, 0xf0, 0xff, 0x00, 0x00, 0xf1, 0x07, 0x00,
    };
    static const uint8_t riscv[] = {
        0x13, 0x75, 0xf5, 0x0f, 0x13, 0x05, 0x75, 0x00,
    };
    FastDynCodeSpec spec;
    const uint8_t *code = x86_16;
    size_t size = sizeof(x86_16);
    uint64_t hook = 0, pc = 0x7c03;
    uint64_t pc64 = 0;
    const char *mode = "x86_16";
    int stress = 0, account = 0;
    static uint8_t stress_code[400];
    (void)id;
    (void)info;
    for (int i = 0; i < argc; i++) {
        if (!strncmp(argv[i], "mode=", 5)) {
            mode = argv[i] + 5;
        } else if (!strncmp(argv[i], "pc=", 3)) {
            pc = strtoull(argv[i] + 3, NULL, 0);
        } else if (!strncmp(argv[i], "pc64=", 5)) {
            pc64 = strtoull(argv[i] + 5, NULL, 0);
        } else if (!strcmp(argv[i], "stress=1") || !strcmp(argv[i], "stress=on")) {
            stress = 1;
        } else if (!strcmp(argv[i], "account=1") || !strcmp(argv[i], "account=on")) {
            account = 1;
        }
    }
    if (!strcmp(mode, "registry_validation")) {
        return registry_validation() ? 0 : 1;
    }
    if (!strcmp(mode, "late_validation")) {
        qemu_plugin_register_vcpu_tb_trans_cb(id, late_translate);
        qemu_plugin_register_atexit_cb(id, late_exit, NULL);
        return 0;
    }
    if (account) {
        account_pc = pc;
        qemu_plugin_register_vcpu_tb_trans_cb(id, account_translate);
        qemu_plugin_register_atexit_cb(id, account_exit, NULL);
    }
    if (!strcmp(mode, "a32") || !strcmp(mode, "thumb")) {
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_ARM,
            !strcmp(mode, "a32") ? FASTDYN_CODE_MODE_A32 : FASTDYN_CODE_MODE_THUMB,
            FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        code = !strcmp(mode, "a32") ? a32 : thumb;
        size = !strcmp(mode, "a32") ? sizeof(a32) : sizeof(thumb);
    } else if (!strcmp(mode, "a64")) {
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_AARCH64,
            FASTDYN_CODE_MODE_A64, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        code = a64;
        size = sizeof(a64);
    } else if (!strcmp(mode, "rv32") || !strcmp(mode, "rv64")) {
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_RISCV,
            !strcmp(mode, "rv32") ? FASTDYN_CODE_MODE_RV32 : FASTDYN_CODE_MODE_RV64,
            FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        code = riscv;
        size = sizeof(riscv);
    } else if (!strcmp(mode, "rv32_c") || !strcmp(mode, "rv64_c")) {
        static const uint8_t compressed_addi[] = { 0x05, 0x05 };
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_RISCV,
            !strcmp(mode, "rv32_c") ? FASTDYN_CODE_MODE_RV32 : FASTDYN_CODE_MODE_RV64,
            FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        code = compressed_addi;
        size = sizeof(compressed_addi);
    } else if (!strcmp(mode, "x86_all")) {
        FastDynCodeSpec spec32 = { FASTDYN_GUEST_ISA_X86,
            FASTDYN_CODE_MODE_X86_32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        FastDynCodeSpec spec64 = { FASTDYN_GUEST_ISA_X86,
            FASTDYN_CODE_MODE_X86_64, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        static const uint8_t x86_32_64[] = {
            0x25, 0xff, 0, 0, 0, 0x83, 0xc0, 7,
        };
        const uint8_t *xcode = x86_32_64;
        size_t xsize = sizeof(x86_32_64);
        uint64_t second = 0;
        if (stress) {
            static const uint8_t cmp_100[] = { 0x83, 0xf8, 0x64 };
            for (int i = 0; i < 99; i++) {
                memcpy(stress_code + 3 * i, (uint8_t[]) { 0x83, 0xc0, 1 }, 3);
            }
            memcpy(stress_code + 3 * 99, cmp_100, sizeof(cmp_100));
            xcode = stress_code;
            xsize = 300;
        }
        bool ok = pc64 &&
            qemu_plugin_register_fastdyn_fragment(pc, xcode, xsize,
                &spec32, &hook) && hook &&
            qemu_plugin_register_fastdyn_fragment(pc64, xcode, xsize,
                &spec64, &second) && second;
        if (stress) {
            memset(stress_code, 0, xsize); /* registration must own its copy */
        }
        return ok ? 0 : 1;
    } else if (!strcmp(mode, "x86_32") || !strcmp(mode, "x86_64")) {
        static const uint8_t add_x86[] = { 0x83, 0xc0, 0x01 };
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_X86,
            !strcmp(mode, "x86_32") ? FASTDYN_CODE_MODE_X86_32 : FASTDYN_CODE_MODE_X86_64,
            FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        code = add_x86;
        size = sizeof(add_x86);
    } else if (!strcmp(mode, "x86_order")) {
        static const uint8_t add_one[] = { 0x83, 0xc0, 0x01 };
        static const uint8_t shift_one[] = { 0xd1, 0xe0 };
        uint64_t second = 0;
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_X86,
            FASTDYN_CODE_MODE_X86_32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        return qemu_plugin_register_fastdyn_fragment(
                   pc, add_one, sizeof(add_one), &spec, &hook) && hook &&
               qemu_plugin_register_fastdyn_fragment(
                   pc, shift_one, sizeof(shift_one), &spec, &second) && second
                   ? 0 : 1;
    } else if (!strcmp(mode, "x86_fault")) {
        static const uint8_t compare_self[] = { 0x39, 0xc0 };
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_X86,
            FASTDYN_CODE_MODE_X86_32, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
        code = compare_self;
        size = sizeof(compare_self);
    } else {
        spec = (FastDynCodeSpec) { FASTDYN_GUEST_ISA_X86,
            FASTDYN_CODE_MODE_X86_16, FASTDYN_INSN_ORDER_LITTLE_ENDIAN };
    }
    if (stress) {
        const uint8_t *one;
        size_t one_size;
        static const uint8_t add_a32[] = { 0x01, 0x00, 0x80, 0xe2 };
        static const uint8_t add_thumb[] = { 0x01, 0x30 };
        static const uint8_t cmp_a32_100[] = { 0x64, 0x00, 0x50, 0xe3 };
        static const uint8_t cmp_thumb_100[] = { 0x64, 0x28 };
        static const uint8_t add_x86[] = { 0x83, 0xc0, 0x01 };
        static const uint8_t cmp_x86_100[] = { 0x83, 0xf8, 0x64 };
        static const uint8_t add_riscv[] = { 0x13, 0x05, 0x15, 0x00 };
        static const uint8_t add_predicate_riscv[] = { 0x33, 0x05, 0xb5, 0x00 };
        static const uint8_t write_x0[] = { 0x13, 0x00, 0x10, 0x00 };
        static const uint8_t minus_one_t0[] = { 0x93, 0x02, 0xf0, 0xff };
        static const uint8_t lui_t0_sign[] = { 0xb7, 0x02, 0x00, 0x80 };
        static const uint8_t addiw_t0[] = { 0x9b, 0x82, 0x02, 0x00 };
        static const uint8_t slt_t0_negative[] = { 0xb3, 0xa5, 0x02, 0x00 };
        static const uint8_t slti_a0_99[] = { 0x93, 0x25, 0x35, 0x06 };
        static const uint8_t addi_a0_2[] = { 0x13, 0x05, 0x25, 0x00 };
        if (spec.isa == FASTDYN_GUEST_ISA_ARM) {
            one = spec.mode == FASTDYN_CODE_MODE_A32 ? add_a32 : add_thumb;
            one_size = spec.mode == FASTDYN_CODE_MODE_A32 ? 4 : 2;
            for (int i = 0; i < 99; i++) {
                memcpy(stress_code + one_size * i, one, one_size);
            }
            memcpy(stress_code + one_size * 99,
                   spec.mode == FASTDYN_CODE_MODE_A32 ? cmp_a32_100 : cmp_thumb_100,
                   one_size);
            code = stress_code;
            size = one_size * 100;
            one = NULL;
        } else if (spec.isa == FASTDYN_GUEST_ISA_AARCH64) {
            /*
             * a64_stress_fragment.S: 100 instructions covering X20,
             * 64-bit data, a W20 zero-extending write, and XZR sources.
             */
            static const uint8_t prefix[] = {
                0xf4, 0xff, 0x9f, 0xd2, 0x94, 0x3e, 0x50, 0xd3,
                0xf4, 0x03, 0x14, 0x2a, 0x94, 0x02, 0x1f, 0x8b,
            };
            static const uint8_t add_x20[] = { 0x94, 0x06, 0x00, 0x91 };
            static const uint8_t finish[] = {
                0x80, 0x02, 0x1f, 0x8b, 0x1f, 0x78, 0x01, 0xf1,
            };
            memcpy(stress_code, prefix, sizeof(prefix));
            for (int i = 0; i < 94; i++) {
                memcpy(stress_code + sizeof(prefix) + 4 * i, add_x20, 4);
            }
            memcpy(stress_code + sizeof(prefix) + 94 * 4, finish,
                   sizeof(finish));
            code = stress_code;
            size = sizeof(stress_code);
            one = NULL;
            one_size = 0;
        } else if (spec.isa == FASTDYN_GUEST_ISA_X86) {
            for (int i = 0; i < 99; i++) {
                memcpy(stress_code + 3 * i, add_x86, 3);
            }
            memcpy(stress_code + 3 * 99, cmp_x86_100, 3);
            code = stress_code;
            size = 300;
            one = NULL;
            one_size = 0;
        } else {
            size_t offset = 0;
            memcpy(stress_code + offset, write_x0, 4); offset += 4;
            if (spec.mode == FASTDYN_CODE_MODE_RV64) {
                memcpy(stress_code + offset, lui_t0_sign, 4); offset += 4;
                memcpy(stress_code + offset, addiw_t0, 4); offset += 4;
                for (int i = 0; i < 94; i++) {
                    memcpy(stress_code + offset, add_riscv, 4); offset += 4;
                }
                memcpy(stress_code + offset, slt_t0_negative, 4); offset += 4;
                memcpy(stress_code + offset, add_predicate_riscv, 4); offset += 4;
                memcpy(stress_code + offset, addi_a0_2, 4); offset += 4;
            } else {
                memcpy(stress_code + offset, minus_one_t0, 4); offset += 4;
                for (int i = 0; i < 96; i++) {
                    memcpy(stress_code + offset, add_riscv, 4); offset += 4;
                }
                memcpy(stress_code + offset, slti_a0_99, 4); offset += 4;
                memcpy(stress_code + offset, add_predicate_riscv, 4); offset += 4;
            }
            code = stress_code;
            size = offset;
            one = NULL;
            one_size = 0;
        }
        if (one) {
            for (int i = 0; i < 100; i++) {
                memcpy(stress_code + one_size * i, one, one_size);
            }
            code = stress_code;
            size = one_size * 100;
        }
    }
    bool ok = qemu_plugin_register_fastdyn_fragment(pc, code, size, &spec,
                                                     &hook) && hook;
    if (stress) {
        memset(stress_code, 0, size); /* registration must own its copy */
    }
    return ok ? 0 : 1;
}
