/* DWARF-assisted entry tracer used as a runtime-plugin teaching example. */
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fastdyn_runtime.h>

#define TRACE_MAX_FUNCTIONS 4096
#define TRACE_MAX_ARGUMENTS 8
#define TRACE_MAX_FIELDS 8
#define TRACE_NAME_MAX 96
#define TRACE_VALUE_MAX 64
#define TRACE_LINE_MAX 1024

typedef enum { TRACE_UNKNOWN, TRACE_INT, TRACE_UINT, TRACE_BOOL, TRACE_FLOAT,
               TRACE_POINTER, TRACE_STRUCT, TRACE_ARRAY } TraceKind;
typedef enum { TRACE_REGISTER, TRACE_STACK } TraceLocationKind;
typedef struct { char name[TRACE_NAME_MAX]; unsigned offset, size; TraceKind kind; int sign; } TraceField;
typedef struct {
    char name[TRACE_NAME_MAX]; TraceLocationKind location_kind; unsigned location, size;
    TraceKind kind; int sign; TraceField fields[TRACE_MAX_FIELDS]; size_t field_count;
} TraceArgument;
typedef struct {
    uint64_t address, calls; char name[TRACE_NAME_MAX];
    TraceArgument arguments[TRACE_MAX_ARGUMENTS]; size_t argument_count;
} TraceFunction;

static TraceFunction functions[TRACE_MAX_FUNCTIONS];
static size_t function_count;
static uint64_t emitted_events, max_events = 100000;
static FILE *trace_stream;
static int tracer_active;
static const VirtualContext *runtime_context;

static TraceKind parse_kind(const char *text) {
    if (strcmp(text, "int") == 0) return TRACE_INT;
    if (strcmp(text, "uint") == 0) return TRACE_UINT;
    if (strcmp(text, "bool") == 0) return TRACE_BOOL;
    if (strcmp(text, "float") == 0) return TRACE_FLOAT;
    if (strcmp(text, "pointer") == 0) return TRACE_POINTER;
    if (strcmp(text, "struct") == 0) return TRACE_STRUCT;
    if (strcmp(text, "array") == 0) return TRACE_ARRAY;
    return TRACE_UNKNOWN;
}

static TraceFunction *find_function(const char *name) {
    size_t index;
    for (index = 0; index < function_count; ++index)
        if (strcmp(functions[index].name, name) == 0) return &functions[index];
    return NULL;
}

static int add_function(uint64_t address, const char *name) {
    TraceFunction *function;
    if (!name || !name[0] || find_function(name)) return 0;
    if (function_count >= TRACE_MAX_FUNCTIONS) return -1;
    function = &functions[function_count++];
    memset(function, 0, sizeof(*function));
    function->address = address;
    snprintf(function->name, sizeof(function->name), "%s", name);
    return 0;
}

static int load_functions(const VirtualContext *ctx) {
    char path[4096], line[TRACE_LINE_MAX];
    FILE *stream;
    if (virtual_artifact_path(ctx, "functions.tsv", path, sizeof(path)) != 0) return -1;
    stream = fopen(path, "r");
    if (!stream) return -1;
    while (fgets(line, sizeof(line), stream)) {
        char *tab = strchr(line, '\t'), *name;
        unsigned long long address;
        if (!tab || sscanf(line, "%llx", &address) != 1) continue;
        name = tab + 1;
        name[strcspn(name, "\r\n")] = '\0';
        if (add_function((uint64_t)address, name) != 0) { fclose(stream); return -1; }
    }
    fclose(stream);
    return function_count ? 0 : -1;
}

static int parse_location(const char *text, TraceLocationKind *kind, unsigned *value) {
    char prefix;
    if (sscanf(text, "%c%u", &prefix, value) != 2) return -1;
    if (prefix == 'r') *kind = TRACE_REGISTER;
    else if (prefix == 's') *kind = TRACE_STACK;
    else return -1;
    return 0;
}

static void parse_fields(TraceArgument *argument, char *text) {
    char *entry = text;
    while (entry && *entry && argument->field_count < TRACE_MAX_FIELDS) {
        TraceField *field = &argument->fields[argument->field_count];
        char *parts[5] = {0}, *next, *separator;
        size_t index = 0;
        next = strchr(entry, ';');
        if (next) *next++ = '\0';
        parts[index++] = entry;
        while (index < 5 && (separator = strchr(parts[index - 1], '|')) != NULL) {
            *separator = '\0';
            parts[index++] = separator + 1;
        }
        if (index != 5) {
            entry = next;
            continue;
        }
        snprintf(field->name, sizeof(field->name), "%s", parts[0]);
        field->offset = (unsigned)strtoul(parts[1], NULL, 10);
        field->kind = parse_kind(parts[2]);
        field->size = (unsigned)strtoul(parts[3], NULL, 10);
        field->sign = atoi(parts[4]) != 0;
        argument->field_count++;
        entry = next;
    }
}

static int load_arguments(const VirtualContext *ctx) {
    char path[4096], line[TRACE_LINE_MAX];
    FILE *stream;
    if (virtual_artifact_path(ctx, "arguments.tsv", path, sizeof(path)) != 0) return -1;
    stream = fopen(path, "r");
    if (!stream) return -1;
    while (fgets(line, sizeof(line), stream)) {
        char *parts[8] = {0}, *token;
        size_t index = 0;
        TraceFunction *function;
        TraceArgument *argument;
        for (token = strtok(line, "\t\r\n"); token && index < 8; token = strtok(NULL, "\t\r\n"))
            parts[index++] = token;
        if (index != 8 || strcmp(parts[0], "function") == 0) continue;
        function = find_function(parts[0]);
        if (!function || function->argument_count >= TRACE_MAX_ARGUMENTS) continue;
        argument = &function->arguments[function->argument_count];
        memset(argument, 0, sizeof(*argument));
        snprintf(argument->name, sizeof(argument->name), "%s", parts[2]);
        if (parse_location(parts[3], &argument->location_kind, &argument->location) != 0) continue;
        argument->size = (unsigned)strtoul(parts[4], NULL, 10);
        argument->size = argument->size > TRACE_VALUE_MAX ? TRACE_VALUE_MAX : argument->size;
        argument->kind = parse_kind(parts[5]);
        argument->sign = atoi(parts[6]) != 0;
        parse_fields(argument, parts[7]);
        function->argument_count++;
    }
    fclose(stream);
    return 0;
}

static void load_settings(const VirtualContext *ctx) {
    char path[4096], key[64];
    unsigned long long value;
    FILE *stream;
    if (virtual_artifact_path(ctx, "settings.tsv", path, sizeof(path)) != 0) return;
    stream = fopen(path, "r");
    if (!stream) return;
    if (fscanf(stream, "%63[^\t]\t%llu", key, &value) == 2 && strcmp(key, "max_events") == 0)
        max_events = (uint64_t)value;
    fclose(stream);
}

static uint64_t read_unsigned(const uint8_t *bytes, size_t size) {
    uint64_t value = 0; size_t index;
    for (index = 0; index < size && index < sizeof(value); ++index)
        value |= (uint64_t)bytes[index] << (index * 8U);
    return value;
}

static int64_t read_signed(const uint8_t *bytes, size_t size) {
    uint64_t value = read_unsigned(bytes, size);
    if (size && size < sizeof(value) && (bytes[size - 1] & 0x80U))
        value |= ~((UINT64_C(1) << (size * 8U)) - 1U);
    return (int64_t)value;
}

static int read_argument_bytes(const TraceArgument *argument, uint8_t *output, size_t *out_size) {
    size_t copied = 0;
    int register_index = (int)argument->location;
    if (argument->location_kind == TRACE_STACK) {
        if (virtual_read_memory(runtime_context,
                                (uintptr_t)(virtual_sp(runtime_context) + argument->location),
                                argument->size, output) != 0) return -1;
        *out_size = argument->size;
        return 0;
    }
    while (copied < argument->size) {
        uint8_t register_bytes[16]; size_t register_size = 0, take;
        if (virtual_read_register_bytes(runtime_context,
                                        register_index++,
                                        register_bytes, sizeof(register_bytes),
                                        &register_size) != 0 || register_size == 0) return -1;
        take = argument->size - copied < register_size ? argument->size - copied : register_size;
        memcpy(output + copied, register_bytes, take);
        copied += take;
    }
    *out_size = copied;
    return 0;
}

static void render_scalar(char *output, size_t output_size, TraceKind kind,
                          int sign, const uint8_t *bytes, size_t size) {
    uint64_t value = read_unsigned(bytes, size);
    if (kind == TRACE_BOOL) snprintf(output, output_size, "%s", value ? "true" : "false");
    else if (kind == TRACE_FLOAT && size == 4) {
        float number; memcpy(&number, bytes, sizeof(number));
        snprintf(output, output_size, "%g", (double)number);
    } else if (sign || kind == TRACE_INT)
        snprintf(output, output_size, "%" PRId64, read_signed(bytes, size));
    else snprintf(output, output_size, "%" PRIu64, value);
}

static void render_structure(char *output, size_t output_size, const TraceArgument *argument,
                             const uint8_t *bytes, size_t size) {
    size_t used = 0, index;
    used += (size_t)snprintf(output + used, output_size - used, "{");
    for (index = 0; index < argument->field_count && used < output_size; ++index) {
        const TraceField *field = &argument->fields[index];
        char value[64];
        if (field->offset + field->size > size || !field->size) continue;
        if (field->kind == TRACE_POINTER)
            snprintf(value, sizeof(value), "0x%" PRIx64, read_unsigned(bytes + field->offset, field->size));
        else render_scalar(value, sizeof(value), field->kind, field->sign,
                           bytes + field->offset, field->size);
        used += (size_t)snprintf(output + used, output_size - used, "%s%s=%s",
                                 used > 1 ? "," : "", field->name, value);
    }
    if (used < output_size) snprintf(output + used, output_size - used, "}");
}

static void render_argument(const TraceArgument *argument, char *output, size_t output_size) {
    uint8_t bytes[TRACE_VALUE_MAX] = {0}; size_t size = 0;
    if (read_argument_bytes(argument, bytes, &size) != 0)
        snprintf(output, output_size, "<unavailable>");
    else if (argument->kind == TRACE_POINTER)
        snprintf(output, output_size, "0x%" PRIx64, read_unsigned(bytes, size));
    else if (argument->kind == TRACE_STRUCT)
        render_structure(output, output_size, argument, bytes, size);
    else if (argument->kind == TRACE_ARRAY || argument->kind == TRACE_UNKNOWN)
        snprintf(output, output_size, "<%s:%u bytes>",
                 argument->kind == TRACE_ARRAY ? "array" : "unknown", argument->size);
    else render_scalar(output, output_size, argument->kind, argument->sign, bytes, size);
}

static void write_summary(void) {
    char path[4096]; FILE *stream; size_t index;
    if (!runtime_context || virtual_artifact_path(runtime_context, "counts.tsv", path, sizeof(path)) != 0) return;
    stream = fopen(path, "w");
    if (!stream) return;
    fputs("address\tfunction\tcalls\n", stream);
    for (index = 0; index < function_count; ++index)
        fprintf(stream, "0x%" PRIx64 "\t%s\t%" PRIu64 "\n",
                functions[index].address, functions[index].name, functions[index].calls);
    fclose(stream);
    if (trace_stream) { fclose(trace_stream); trace_stream = NULL; }
}

static void function_tracer(unsigned int cpu_index, void *userdata) {
    TraceFunction *function; size_t index;
    (void)cpu_index;
    if (!tracer_active || !userdata) return;
    function = find_function((const char *)userdata);
    if (!function) return;
    function->calls++;
    if (!trace_stream || (max_events && emitted_events >= max_events)) return;
    fprintf(trace_stream, "%" PRIu64 "\t0x%" PRIx64 "\t%s\t",
            virtual_icount(runtime_context), virtual_pc(runtime_context), function->name);
    for (index = 0; index < function->argument_count; ++index) {
        char value[160];
        render_argument(&function->arguments[index], value, sizeof(value));
        fprintf(trace_stream, "%s%s=%s", index ? ";" : "", function->arguments[index].name, value);
    }
    fputc('\n', trace_stream);
    fflush(trace_stream);
    emitted_events++;
}

static int function_tracer_runtime_init(const VirtualContext *ctx) {
    char path[4096];
    runtime_context = ctx;
    if (virtual_register_callback(ctx, "function_tracer", function_tracer) != 0) return -1;
    /* No manifest: the run does not use the tracer. */
    if (virtual_artifact_path(ctx, "functions.tsv", path, sizeof(path)) != 0) return -1;
    if (access(path, F_OK) != 0) return 0;
    if (load_functions(ctx) != 0 || load_arguments(ctx) != 0) {
        virtual_log(ctx, VIRTUAL_LOG_WARN, "no function-trace manifests were available");
        return 0;
    }
    load_settings(ctx);
    if (virtual_artifact_path(ctx, "trace.tsv", path, sizeof(path)) != 0) return -1;
    trace_stream = fopen(path, "a");
    if (!trace_stream) return -1;
    tracer_active = 1;
    virtual_register_exit(ctx, write_summary);
    virtual_log(ctx, VIRTUAL_LOG_INFO, "tracing %zu functions (max_events=%" PRIu64 ")",
                function_count, max_events);
    return 0;
}

VIRTUAL_PLUGIN("function_tracer", function_tracer_runtime_init);
