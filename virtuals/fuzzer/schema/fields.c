#include <stdio.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

#include "fuzz.h"
#include "schema.h"

static uint8_t *input;
static size_t input_size;

void schema_fields_begin_iteration(void)
{
    struct Field **fields;
    size_t count;
    size_t i;

    fields = generic_get_fields(&count);
    for (i = 0; i < count; i++) {
        free(fields[i]->generated);
        fields[i]->generated = NULL;
        fields[i]->generated_size = 0;
        fields[i]->generating = false;
    }
}

size_t schema_input_actual_size(void)
{
    return input_size;
}

const uint8_t *schema_input_bytes(void)
{
    return input;
}

bool schema_field_input_read(const struct Field *field, size_t *offset,
                             void *out, size_t size)
{
    size_t available = 0;

    if (field == NULL || offset == NULL || out == NULL ||
        *offset > field->input_size || size > field->input_size - *offset) {
        return false;
    }
    memset(out, 0, size);
    if (field->input_offset < input_size) {
        available = input_size - field->input_offset;
        if (available > field->input_size) {
            available = field->input_size;
        }
    }
    if (*offset < available) {
        size_t copy_size = available - *offset;
        if (copy_size > size) {
            copy_size = size;
        }
        memcpy(out, input + field->input_offset + *offset, copy_size);
    }
    *offset += size;
    return true;
}

bool schema_field_materialize(struct Field *field, const uint8_t **data,
                              size_t *size)
{
    uint8_t *generated;

    if (field == NULL || field->handler == NULL || field->handler->generate == NULL ||
        field->generating) {
        return false;
    }
    if (field->generated == NULL) {
        generated = calloc(1, field->size == 0 ? 1U : field->size);
        if (generated == NULL) {
            return false;
        }
        field->generating = true;
        if (!field->handler->generate(field, generated, field->size)) {
            field->generating = false;
            free(generated);
            return false;
        }
        field->generating = false;
        field->generated = generated;
        field->generated_size = field->size;
    }
    if (data != NULL) {
        *data = field->generated;
    }
    if (size != NULL) {
        *size = field->generated_size;
    }
    return true;
}

static bool write_location(const struct Location *location, const uint8_t *data,
                           size_t size)
{
    uint32_t register_value = 0;

    if (location->type == Register) {
        memcpy(&register_value, data, size < sizeof(register_value) ? size : sizeof(register_value));
        fuzz_set_register(register_value, location->val.reg);
        return true;
    }
    if (location->type == Memory && size <= (size_t)INT_MAX) {
        return fuzz_write_memory(location->val.address, (uint8_t *)data,
                                 (int)size) == 0;
    }
    return false;
}

bool schema_begin_iteration(void)
{
    struct Field **fields;
    size_t field_count;
    size_t offset = 0;
    size_t i;

    schema_end_iteration();
    if (!fuzz_take_input(&input, &input_size)) {
        return false;
    }
    fields = generic_get_fields(&field_count);
    schema_fields_begin_iteration();
    for (i = 0; i < field_count; i++) {
        size_t expected = fields[i]->handler->expected(fields[i]);

        /* Locationless fields are emitted by streams. Their input range is
         * assigned on first stream use so all streams retain one shared,
         * execution-ordered suffix. */
        if (fields[i]->location_text == NULL) {
            fields[i]->input_offset = 0;
            fields[i]->input_size = 0;
            fields[i]->injected = false;
            continue;
        }
        if (expected > SIZE_MAX - offset) {
            schema_end_iteration();
            return false;
        }
        fields[i]->input_offset = offset;
        fields[i]->input_size = expected;
        fields[i]->injected = false;
        offset += expected;
    }
    schema_streams_begin_iteration(offset);
    return true;
}

void schema_end_iteration(void)
{
    free(input);
    input = NULL;
    input_size = 0;
}

bool schema_inject_field(struct Field *field)
{
    const uint8_t *data;
    size_t size;
    bool ok;

    if (field == NULL || field->injected || field->handler == NULL ||
        field->handler->generate == NULL) {
        return field != NULL && field->injected;
    }
    if (field->hook_index != SIZE_MAX) {
        field->location_resolved = false;
    }
    if (!field->location_resolved && !schema_resolve_field_location(field)) {
        return false;
    }
    ok = schema_field_materialize(field, &data, &size) &&
         size == field->size && write_location(&field->location, data, size);
    if (ok) {
        field->injected = true;
    }
    return ok;
}

void schema_inject_snap_fields(void)
{
    struct Field **fields;
    size_t count;
    size_t i;

    fields = generic_get_fields(&count);
    for (i = 0; i < count; i++) {
        if (fields[i]->location_text != NULL &&
            fields[i]->hook_index == SIZE_MAX && !schema_inject_field(fields[i])) {
            fprintf(stderr, "[schema] could not inject field '%s' at snap\n",
                    fields[i]->name);
        }
    }
}
