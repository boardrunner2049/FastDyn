#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "core.h"
#include "schema.h"
#include "utils.h"

static struct Field **fields;
static size_t field_count;
static struct Stream **streams;
static size_t stream_count;
static struct SchemaHook *hooks;
static size_t hook_count;
struct PostSnapshotModifier {
    uint64_t at;
    char *patch;
};
static struct PostSnapshotModifier *post_snapshot_modifiers;
static size_t post_snapshot_modifier_count;
static volatile uint8_t post_snapshot_modifiers_enabled;
static const char *schema_path;
static bool schema_loaded;
static bool locations_resolved;

enum FieldProperty {
    FieldName = 1U << 0,
    FieldLocation = 1U << 1,
    FieldType = 1U << 2,
    FieldSize = 1U << 3,
    FieldOptions = 1U << 4,
    FieldRequired = FieldName | FieldType | FieldSize,
};

static char *copy_string(const cJSON *value)
{
    char *copy;
    size_t length;

    if (!cJSON_IsString(value) || value->valuestring == NULL) {
        return NULL;
    }
    length = strlen(value->valuestring);
    copy = malloc(length + 1U);
    if (copy == NULL) {
        errno = ENOMEM;
        return NULL;
    }
    memcpy(copy, value->valuestring, length + 1U);
    return copy;
}

static void free_post_snapshot_modifiers(struct PostSnapshotModifier *modifiers,
                                         size_t count)
{
    if (modifiers == NULL) {
        return;
    }
    for (size_t i = 0; i < count; i++) {
        free(modifiers[i].patch);
    }
    free(modifiers);
}

static bool parse_modifier_address(const cJSON *value, uint64_t *address)
{
    char *end;

    if (cJSON_IsNumber(value) && value->valuedouble >= 0.0 &&
        (uint64_t)value->valuedouble == value->valuedouble) {
        *address = (uint64_t)value->valuedouble;
        return true;
    }
    if (!cJSON_IsString(value) || value->valuestring == NULL) {
        return false;
    }
    errno = 0;
    *address = strtoull(value->valuestring, &end, 0);
    return errno == 0 && end != value->valuestring && *end == '\0';
}

static bool parse_post_snapshot_modifiers(
    const cJSON *json, struct PostSnapshotModifier **output, size_t *count)
{
    struct PostSnapshotModifier *modifiers;
    const cJSON *item;
    int json_count;
    size_t index = 0;

    if (!cJSON_IsArray(json) || (json_count = cJSON_GetArraySize(json)) < 0) {
        return false;
    }
    modifiers = calloc((size_t)json_count == 0 ? 1U : (size_t)json_count,
                       sizeof(*modifiers));
    if (modifiers == NULL) {
        errno = ENOMEM;
        return false;
    }

    cJSON_ArrayForEach(item, json) {
        const cJSON *value;
        bool got_at = false;
        bool got_patch = false;

        if (!cJSON_IsObject(item)) {
            goto fail;
        }
        cJSON_ArrayForEach(value, item) {
            if (value->string == NULL) {
                goto fail;
            }
            if (strcmp(value->string, "at") == 0 && !got_at) {
                if (!parse_modifier_address(value, &modifiers[index].at)) {
                    goto fail;
                }
                got_at = true;
            } else if (strcmp(value->string, "patch") == 0 && !got_patch) {
                modifiers[index].patch = copy_string(value);
                if (modifiers[index].patch == NULL ||
                    modifiers[index].patch[0] == '\0') {
                    goto fail;
                }
                got_patch = true;
            } else {
                goto fail;
            }
        }
        if (!got_at || !got_patch) {
            goto fail;
        }
        index++;
    }

    *output = modifiers;
    *count = index;
    return true;

fail:
    free_post_snapshot_modifiers(modifiers, (size_t)json_count);
    return false;
}

static bool install_post_snapshot_modifiers(
    const struct PostSnapshotModifier *modifiers, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        if (!core_register_gated_modifier(modifiers[i].at, modifiers[i].patch,
                                          &post_snapshot_modifiers_enabled)) {
            return false;
        }
    }
    return true;
}

void schema_field_free(struct Field *field)
{
    if (field == NULL) {
        return;
    }
    if (field->handler != NULL && field->handler->free_options != NULL) {
        field->handler->free_options(field);
    } else {
        free(field->options);
    }
    free(field->name);
    free(field->location_text);
    free(field->type);
    free(field->generated);
    free(field);
}

static void free_fields(struct Field **field_list, size_t count)
{
    size_t i;

    if (field_list == NULL) {
        return;
    }
    for (i = 0; i < count; i++) {
        schema_field_free(field_list[i]);
    }
    free(field_list);
}

void generic_clear_fields(void)
{
    schema_end_iteration();
    free_fields(fields, field_count);
    schema_streams_free(streams, stream_count);
    schema_hooks_free(hooks, hook_count);
    free_post_snapshot_modifiers(post_snapshot_modifiers,
                                 post_snapshot_modifier_count);
    fields = NULL;
    field_count = 0;
    streams = NULL;
    stream_count = 0;
    hooks = NULL;
    hook_count = 0;
    post_snapshot_modifiers = NULL;
    post_snapshot_modifier_count = 0;
    post_snapshot_modifiers_enabled = 0;
    schema_streams_set_active(NULL, 0);
    schema_loaded = false;
    locations_resolved = false;
}

void schema_activate_post_snapshot_modifiers(void)
{
    post_snapshot_modifiers_enabled = 1;
}

struct Field **generic_get_fields(size_t *count)
{
    if (count != NULL) {
        *count = field_count;
    }
    return fields;
}

static bool parse_size(const cJSON *value, size_t *size)
{
    double number;
    uint64_t integer;

    if (!cJSON_IsNumber(value)) {
        return false;
    }
    number = value->valuedouble;
    if (number < 0.0 || number >= (double)UINT64_MAX) {
        return false;
    }
    integer = (uint64_t)number;
    if ((double)integer != number || integer > SIZE_MAX) {
        return false;
    }
    *size = (size_t)integer;
    return true;
}

static unsigned int field_property(const char *name)
{
    if (strcmp(name, "name") == 0) return FieldName;
    if (strcmp(name, "location") == 0) return FieldLocation;
    if (strcmp(name, "type") == 0) return FieldType;
    if (strcmp(name, "size") == 0) return FieldSize;
    if (strcmp(name, "options") == 0) return FieldOptions;
    return 0;
}

static bool parse_field(const cJSON *json, struct Field *field)
{
    const cJSON *value;
    const cJSON *options = NULL;
    unsigned int properties = 0;

    if (!cJSON_IsObject(json)) {
        return false;
    }
    field->hook_index = SIZE_MAX;
    cJSON_ArrayForEach(value, json) {
        unsigned int property;
        char **destination;

        if (value->string == NULL ||
            (property = field_property(value->string)) == 0 ||
            (properties & property) != 0) {
            return false;
        }
        if (property == FieldOptions) {
            if (!cJSON_IsObject(value)) return false;
            options = value;
        } else if (property == FieldSize) {
            if (!parse_size(value, &field->size)) return false;
        } else {
            destination = property == FieldName ? &field->name :
                          property == FieldLocation ? &field->location_text :
                          &field->type;
            *destination = copy_string(value);
            if (*destination == NULL) return false;
        }
        properties |= property;
    }
    if ((properties & FieldRequired) != FieldRequired) {
        return false;
    }
    field->handler = schema_find_type_handler(field->type);
    return field->handler != NULL && field->handler->parse_options != NULL &&
           field->handler->expected != NULL && field->handler->generate != NULL &&
           field->handler->parse_options(options, field);
}

static bool parse_fields(const cJSON *json, struct Field ***output,
                         size_t *count)
{
    struct Field **loaded;
    const cJSON *item;
    int json_count;
    size_t i = 0;

    if (!cJSON_IsArray(json) || (json_count = cJSON_GetArraySize(json)) < 0 ||
        (size_t)json_count > SIZE_MAX / sizeof(*loaded)) {
        return false;
    }
    loaded = calloc((size_t)json_count == 0 ? 1U : (size_t)json_count,
                    sizeof(*loaded));
    if (loaded == NULL) {
        errno = ENOMEM;
        return false;
    }
    cJSON_ArrayForEach(item, json) {
        loaded[i] = calloc(1, sizeof(*loaded[i]));
        if (loaded[i] == NULL || !parse_field(item, loaded[i])) {
            free_fields(loaded, (size_t)json_count);
            return false;
        }
        i++;
    }
    *output = loaded;
    *count = (size_t)json_count;
    return true;
}

static bool finalize_fields(struct Field **field_list, size_t count,
                            struct Stream **stream_list, size_t streams_count)
{
    size_t i;
    size_t j;

    for (i = 0; i < count; i++) {
        for (j = i + 1; j < count; j++) {
            if (strcmp(field_list[i]->name, field_list[j]->name) == 0) {
                return false;
            }
        }
        if (field_list[i]->handler->finalize != NULL &&
            !field_list[i]->handler->finalize(field_list[i], field_list, count,
                                              stream_list, streams_count)) {
            return false;
        }
    }
    return true;
}

static bool names_are_unique(struct Field **field_list, size_t fields_count,
                             struct Stream **stream_list, size_t streams_count)
{
    size_t i;
    size_t j;

    for (i = 0; i < streams_count; i++) {
        for (j = i + 1; j < streams_count; j++) {
            if (strcmp(stream_list[i]->name, stream_list[j]->name) == 0) return false;
        }
        for (j = 0; j < fields_count; j++) {
            if (strcmp(stream_list[i]->name, field_list[j]->name) == 0) return false;
        }
    }
    return true;
}

static bool rule_addresses_are_unique(const struct SchemaFlow *flow,
                                      bool has_flow,
                                      const struct SchemaHook *hook_list,
                                      size_t hooks_count)
{
    size_t i;
    size_t j;

    for (i = 0; i < hooks_count; i++) {
        if (has_flow && ((flow->state.present && hook_list[i].at == flow->state.at) ||
                         (flow->snap.present && hook_list[i].at == flow->snap.at))) {
            return false;
        }
        if (has_flow) {
            for (j = 0; j < flow->sync_point_count; j++) {
                if (hook_list[i].at == flow->sync_points[j].at) {
                    return false;
                }
            }
        }
        for (j = i + 1; j < hooks_count; j++) {
            if (hook_list[i].at == hook_list[j].at) {
                return false;
            }
        }
    }
    if (!has_flow) return true;
    if (flow->state.present && flow->state.at == flow->snap.at) {
        return false;
    }
    for (i = 0; i < flow->sync_point_count; i++) {
        if ((flow->state.present && flow->state.at == flow->sync_points[i].at) ||
            flow->snap.at == flow->sync_points[i].at) {
            return false;
        }
        for (j = i + 1; j < flow->sync_point_count; j++) {
            if (flow->sync_points[i].at == flow->sync_points[j].at) {
                return false;
            }
        }
    }
    return true;
}

static bool streams_are_bound(struct Stream **stream_list, size_t streams_count)
{
    size_t i;

    for (i = 0; i < streams_count; i++) {
        if (stream_list[i]->hook_index == SIZE_MAX) {
            return false;
        }
    }
    return true;
}

static char *read_file(const char *path, size_t *length)
{
    FILE *file;
    char *contents = NULL;
    long file_length;

    file = fopen(path, "rb");
    if (file == NULL) return NULL;
    if (fseek(file, 0, SEEK_END) != 0 || (file_length = ftell(file)) < 0 ||
        (uintmax_t)file_length > SIZE_MAX - 1U) {
        goto done;
    }
    *length = (size_t)file_length;
    contents = malloc(*length + 1U);
    if (contents == NULL || fseek(file, 0, SEEK_SET) != 0 ||
        fread(contents, 1, *length, file) != *length) {
        free(contents);
        contents = NULL;
        goto done;
    }
    contents[*length] = '\0';
done:
    fclose(file);
    return contents;
}

bool generic_load_fields(const char *path)
{
    char *contents = NULL;
    cJSON *root = NULL;
    const cJSON *value;
    struct Field **loaded_fields = NULL;
    struct Stream **loaded_streams = NULL;
    struct SchemaHook *loaded_hooks = NULL;
    struct PostSnapshotModifier *loaded_post_snapshot_modifiers = NULL;
    struct SchemaFlow flow = {0};
    size_t length;
    size_t loaded_field_count = 0;
    size_t loaded_stream_count = 0;
    size_t loaded_hook_count = 0;
    size_t loaded_post_snapshot_modifier_count = 0;
    bool has_flow = false;
    bool got_fields = false;
    bool got_streams = false;
    bool got_hooks = false;
    bool got_post_snapshot_modifiers = false;
    bool ok = false;

    if (path == NULL || schema_loaded) {
        errno = EINVAL;
        return false;
    }
    contents = read_file(path, &length);
    if (contents == NULL) goto done;
    root = cJSON_ParseWithLengthOpts(contents, length + 1U, NULL, 1);
    if (!cJSON_IsObject(root)) goto done;
    cJSON_ArrayForEach(value, root) {
        if (value->string == NULL) goto done;
        if (strcmp(value->string, "fields") == 0 && !got_fields) {
            if (!parse_fields(value, &loaded_fields, &loaded_field_count)) goto done;
            got_fields = true;
        } else if (strcmp(value->string, "streams") == 0 && !got_streams) {
            if (!schema_streams_parse(value, &loaded_streams, &loaded_stream_count)) goto done;
            got_streams = true;
        } else if (strcmp(value->string, "hooks") == 0 && !got_hooks) {
            if (!schema_hooks_parse(value, &loaded_hooks, &loaded_hook_count)) goto done;
            got_hooks = true;
        } else if (strcmp(value->string, "flow") == 0 && !has_flow) {
            if (!schema_flow_parse(value, &flow)) goto done;
            has_flow = true;
        } else if (strcmp(value->string, "post_snapshot_modifiers") == 0 &&
                   !got_post_snapshot_modifiers) {
            if (!parse_post_snapshot_modifiers(
                    value, &loaded_post_snapshot_modifiers,
                    &loaded_post_snapshot_modifier_count)) {
                goto done;
            }
            got_post_snapshot_modifiers = true;
        } else {
            goto done;
        }
    }
    if ((!got_fields && !got_streams) ||
        (got_post_snapshot_modifiers && !has_flow) ||
        !names_are_unique(loaded_fields, loaded_field_count, loaded_streams,
                          loaded_stream_count) ||
        !schema_streams_finalize(loaded_streams, loaded_stream_count,
                                 loaded_fields, loaded_field_count) ||
        !finalize_fields(loaded_fields, loaded_field_count, loaded_streams,
                         loaded_stream_count) ||
        !schema_hooks_finalize(loaded_hooks, loaded_hook_count, loaded_fields,
                               loaded_field_count, loaded_streams,
                               loaded_stream_count) ||
        !streams_are_bound(loaded_streams, loaded_stream_count) ||
        !rule_addresses_are_unique(&flow, has_flow, loaded_hooks,
                                  loaded_hook_count)) {
        goto done;
    }

    fields = loaded_fields;
    field_count = loaded_field_count;
    streams = loaded_streams;
    stream_count = loaded_stream_count;
    hooks = loaded_hooks;
    hook_count = loaded_hook_count;
    post_snapshot_modifiers = loaded_post_snapshot_modifiers;
    post_snapshot_modifier_count = loaded_post_snapshot_modifier_count;
    loaded_fields = NULL;
    loaded_streams = NULL;
    loaded_hooks = NULL;
    loaded_post_snapshot_modifiers = NULL;
    schema_streams_set_active(streams, stream_count);
    if ((has_flow && !schema_flow_install(&flow)) ||
        !schema_hooks_install(hooks, hook_count) ||
        !install_post_snapshot_modifiers(post_snapshot_modifiers,
                                         post_snapshot_modifier_count)) {
        generic_clear_fields();
        goto done;
    }
    schema_loaded = true;
    ok = true;

done:
    schema_flow_free(&flow);
    cJSON_Delete(root);
    free(contents);
    free_fields(loaded_fields, loaded_field_count);
    schema_streams_free(loaded_streams, loaded_stream_count);
    schema_hooks_free(loaded_hooks, loaded_hook_count);
    free_post_snapshot_modifiers(loaded_post_snapshot_modifiers,
                                 loaded_post_snapshot_modifier_count);
    if (!ok && errno == 0) errno = EINVAL;
    return ok;
}

bool schema_resolve_field_location(struct Field *field)
{
    if (field == NULL || field->location_text == NULL) {
        return false;
    }
    if (field->location_resolved) {
        return true;
    }
    if (!generic_parse_expression(field->location_text, &field->location)) {
        return false;
    }
    field->location_resolved = true;
    return true;
}

bool schema_resolve_locations(void)
{
    size_t i;

    if (locations_resolved) return true;
    for (i = 0; i < field_count; i++) {
        if (fields[i]->location_text == NULL) {
            continue;
        }
        if (!schema_resolve_field_location(fields[i])) {
            return false;
        }
    }
    locations_resolved = true;
    return true;
}

void generic_configure(int argc, char **argv)
{
    char message[512];

    schema_path = utils_get_arg("fuzzing_schema", argc, argv);
    if (schema_path != NULL && schema_path[0] != '\0' &&
        !generic_load_fields(schema_path)) {
        snprintf(message, sizeof(message),
                 "[schema] could not load fuzzing_schema '%s'", schema_path);
        utils_die(message);
    }
}

void generic_callback(void)
{
    if (!schema_loaded) {
        utils_die("[schema] fuzzing_schema is missing or could not be loaded");
    }
    if (!schema_resolve_locations()) {
        utils_die("[schema] could not resolve field location at snap");
    }
    if (!schema_begin_iteration()) {
        utils_die("[schema] could not take fuzz input");
    }
    schema_inject_snap_fields();
}
