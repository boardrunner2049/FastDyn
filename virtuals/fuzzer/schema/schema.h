#ifndef FUZZ_SCHEMA_H
#define FUZZ_SCHEMA_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct cJSON;

/* Locations are intentionally only destinations. Expression values are
 * evaluated while a snapshot is active and collapse to a memory address. */
struct Location {
    enum Type {
        Register,
        Memory,
    } type;
    union {
        int reg;
        uint64_t address;
    } val;
};

struct Field;
struct Stream;

void schema_field_free(struct Field *field);

struct TypeHandler {
    bool (*parse_options)(const struct cJSON *options, struct Field *field);
    bool (*finalize)(struct Field *field, struct Field **fields,
                     size_t field_count, struct Stream **streams,
                     size_t stream_count);
    size_t (*expected)(const struct Field *field);
    bool (*generate)(const struct Field *field, uint8_t *out, size_t out_size);
    void (*free_options)(struct Field *field);
};

struct Field {
    char *name;
    char *location_text;
    struct Location location;
    bool location_resolved;
    char *type;
    const struct TypeHandler *handler;
    size_t size;
    void *options;

    /* Per-iteration fixed-input plan. This is not schema-owned data. */
    size_t input_offset;
    size_t input_size;
    bool injected;
    size_t hook_index;

    /* Materialized values let derived types reuse exactly the bytes that a
     * referenced field will write, rather than reading guest memory back. */
    uint8_t *generated;
    size_t generated_size;
    bool generating;
};

struct Stream {
    char *name;
    char *location_text;
    struct Location location;
    bool location_resolved;
    size_t hook_index;
    size_t cursor;
    size_t chunk_size;
    /* Optional value written to a register destination after no more stream
     * bytes are available. This permits byte-oriented receive APIs whose
     * normal empty indication is wider than one byte, such as -1. */
    bool has_eof_value;
    uint32_t eof_value;

    /* Optional finite sequence of top-level field definitions. Without
     * fields, a stream retains its unbounded raw-byte behavior. Each hook
     * invocation emits chunk_size bytes (one by default). */
    char **field_names;
    struct Field **fields;
    size_t field_count;
    size_t field_index;
    size_t field_offset;

    /* Bytes actually delivered during this iteration. Derived stream fields
     * such as checksums use this as their explicit, per-stream history. */
    uint8_t *emitted;
    size_t emitted_size;
    size_t emitted_capacity;
};

enum SchemaReferenceKind {
    SchemaReferenceField,
    SchemaReferenceStream,
};

struct SchemaReference {
    enum SchemaReferenceKind kind;
    union {
        struct Field *field;
        struct Stream *stream;
    } value;
};

struct SchemaHook {
    uint64_t at;
    char **names;
    size_t name_count;
    struct SchemaReference *references;
};

struct SchemaFlowPoint {
    bool present;
    uint64_t at;
};

struct SchemaFlowSyncPoint {
    uint64_t at;
    uint64_t resume;
};

struct SchemaFlow {
    struct SchemaFlowPoint state;
    struct SchemaFlowPoint snap;
    struct SchemaFlowSyncPoint *sync_points;
    size_t sync_point_count;
};

/* Compatibility entry points used by fuzz.c. Configuration is parsed early so
 * flow and hook rules exist before translated blocks are created. */
void generic_configure(int argc, char **argv);
void generic_callback(void);

bool generic_load_fields(const char *path);
void generic_clear_fields(void);
struct Field **generic_get_fields(size_t *count);
bool generic_parse_expression(const char *expression, struct Location *result);

/* Type registry and field input helpers. */
const struct TypeHandler *schema_find_type_handler(const char *name);
bool schema_field_input_read(const struct Field *field, size_t *offset,
                             void *out, size_t size);
bool schema_field_materialize(struct Field *field, const uint8_t **data,
                              size_t *size);
void schema_fields_begin_iteration(void);
size_t schema_input_actual_size(void);
const uint8_t *schema_input_bytes(void);

/* Snapshot and injection lifecycle. */
bool schema_resolve_locations(void);
bool schema_begin_iteration(void);
void schema_end_iteration(void);
bool schema_inject_field(struct Field *field);
void schema_inject_snap_fields(void);
void schema_inject_hook(size_t index);

/* JSON boundaries. */
bool schema_flow_parse(const struct cJSON *json, struct SchemaFlow *flow);
void schema_flow_free(struct SchemaFlow *flow);
bool schema_flow_install(const struct SchemaFlow *flow);
void schema_activate_post_snapshot_modifiers(void);
bool schema_hooks_parse(const struct cJSON *json, struct SchemaHook **hooks,
                        size_t *hook_count);
bool schema_hooks_finalize(struct SchemaHook *hooks, size_t hook_count,
                           struct Field **fields, size_t field_count,
                           struct Stream **streams, size_t stream_count);
bool schema_hooks_install(struct SchemaHook *hooks, size_t hook_count);
void schema_hooks_free(struct SchemaHook *hooks, size_t hook_count);

bool schema_streams_parse(const struct cJSON *json, struct Stream ***streams,
                          size_t *stream_count);
bool schema_streams_finalize(struct Stream **streams, size_t stream_count,
                             struct Field **fields, size_t field_count);
void schema_streams_free(struct Stream **streams, size_t stream_count);
void schema_streams_begin_iteration(size_t fixed_input_size);
void schema_stream_write_next(struct Stream *stream);
bool schema_stream_reserve_reference(struct Field *field);
const uint8_t *schema_stream_emitted(const struct Stream *stream,
                                     size_t *size);
bool schema_stream_total_size(const struct Stream *stream, size_t *size);
void schema_streams_set_active(struct Stream **streams, size_t count);

/* Dispatch target registered by schema_hooks_install(). */
void schema_hook_callback(unsigned int cpu_index, void *udata);

#endif
