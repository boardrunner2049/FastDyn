#ifndef COMMON_H
#define COMMON_H

#include <qemu/qemu-plugin.h>
#include <stdint.h>

// Constants needed by both core.c and virtuals.c
#define MAX_BUFFER_SIZE 256
#define MAX_FILENAME_LEN 256
#define LOG_BUFFER_SIZE (UINT16_MAX + 1)
#define MAX_ENTRIES_PER_LIST 100
#define MAX_LISTS 100
#define MAX_ENTRIES 1024
#define MAX_LINE_LEN 128
/* Run-wide instrumentation may install one virtual at every function entry. */
#define MAX_RULES 4096
#define LINE_BUFFER_SIZE 1024

// ARM register constants
#define ARM_V7M_R0    0
#define ARM_V7M_R1    1
#define ARM_V7M_R2    2
#define ARM_V7M_R3    3
#define ARM_V7M_R4    4
#define ARM_V7M_R5    5
#define ARM_V7M_R6    6
#define ARM_V7M_R7    7
#define ARM_V7M_R8    8
#define ARM_V7M_R9    9
#define ARM_V7M_R10  10
#define ARM_V7M_R11  11
#define ARM_V7M_R12  12
#define ARM_V7M_SP   13  // Stack Pointer
#define ARM_V7M_LR   14  // Link Register
#define ARM_V7M_PC   15  // Program Counter

#define ARM_V7M_S0   26
#define ARM_V7M_S1   27
#define ARM_V7M_S2   28
#define ARM_V7M_S3   29
#define ARM_V7M_S4   30
#define ARM_V7M_S5   31
#define ARM_V7M_S6   32
#define ARM_V7M_S7   33
#define ARM_V7M_S8   34
#define ARM_V7M_S9   35
#define ARM_V7M_S10  36
#define ARM_V7M_S11  37
#define ARM_V7M_S12  38
#define ARM_V7M_S13  39
#define ARM_V7M_S14  40
#define ARM_V7M_S15  41
#define ARM_V7M_S16  42
#define ARM_V7M_S17  43
#define ARM_V7M_S18  44
#define ARM_V7M_S19  45
#define ARM_V7M_S20  46
#define ARM_V7M_S21  47
#define ARM_V7M_S22  48
#define ARM_V7M_S23  49
#define ARM_V7M_S24  50
#define ARM_V7M_S25  51
#define ARM_V7M_S26  52
#define ARM_V7M_S27  53
#define ARM_V7M_S28  54
#define ARM_V7M_S29  55
#define ARM_V7M_S30  56
#define ARM_V7M_S31  57

#define ARM_V7M_D0  26
#define ARM_V7M_D1  27
#define ARM_V7M_D2  28
#define ARM_V7M_D3  29
#define ARM_V7M_D4  30
#define ARM_V7M_D5  31
#define ARM_V7M_D6  32
#define ARM_V7M_D7  33
#define ARM_V7M_D8  34
#define ARM_V7M_D9  35
#define ARM_V7M_D10 36
#define ARM_V7M_D11 37
#define ARM_V7M_D12 38
#define ARM_V7M_D13 39
#define ARM_V7M_D14 40
#define ARM_V7M_D15 41

// Common types used by both core.c and virtuals.c
#ifndef FASTDYN_CB_FUNC_T_DEFINED
#define FASTDYN_CB_FUNC_T_DEFINED
typedef void (*cb_func_t)(unsigned int cpu_index, void *userdata);
#endif

typedef struct {
    const char *name;
    cb_func_t func;
} cb_entry_t;

typedef struct {
    unsigned long long address;
    cb_func_t func;        // function pointer, NOT the name
    char args[384];
} rule_t;

typedef union {
    float  f;
    uint32_t i;
} FloatConverter;

typedef union {
    double d;
    float f[2];
    uint32_t i[2];
} DoubleConverter;

typedef struct {
    uint32_t *buffer;  // Pointer to the buffer
    uint16_t index;     // Current index into the buffer
} Buffy;

typedef struct {
    uintptr_t address;
    int reg; // "register" number (just a number)
} LoggerEntry;

typedef struct {
    LoggerEntry entries[MAX_ENTRIES_PER_LIST];
    size_t count;
    Buffy log_buf;
} AddressList;

typedef struct {
    uint32_t address;
    char mode; // 'r' or 'w'
    uint32_t length;
    uint8_t buffer[MAX_BUFFER_SIZE];
} MemAccess;

typedef struct {
    uint64_t addr;
    char filename[MAX_FILENAME_LEN];  // Fixed-size buffer
} AddrFilePair;

typedef struct {
    int idx;
    char file_name[256]; // Max file name size (adjust as needed)
} FileEntry;

// Value types for updates
typedef enum {
    VALUE_IMMEDIATE, // existing: value is immediate
    VALUE_REGISTER,  // new: value comes from another register (e.g., r3)
    VALUE_DEREF      // new: value is loaded from the memory address held in r3
} ValueType;

typedef enum {
    TARGET_REGISTER,
    TARGET_MEMORY,
    TARGET_DEREF
} TargetType;

typedef struct {
    unsigned long update_point;
    TargetType type; // TARGET_REGISTER or TARGET_MEMORY

    union {
        int reg_num;          // if TARGET_REGISTER
        unsigned long addr;   // if TARGET_MEMORY
    } target;

    ValueType value_type;

    union {
        unsigned long imm; // VALUE_IMMEDIATE
        int reg_num;       // VALUE_REGISTER and VALUE_DEREF (source register)
    } value;

    /* NULL means unconditional. Otherwise the update is emitted only when
       the pointed-to byte is non-zero at guest runtime. */
    const volatile uint8_t *enabled;
} UpdateEntry;

typedef struct {
    uintptr_t anchor;
    uintptr_t target;
} AddressTuple;

typedef struct {
    AddressList* list;
    LoggerEntry* entry;
} LookupResult;

// External function declarations (moved from virtuals.h to avoid circular dependency)
extern uint32_t qemu_get_register(int reg);
extern void qemu_set_register(uint32_t value, int reg);

#endif // COMMON_H
