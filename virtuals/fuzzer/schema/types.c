#include <errno.h>
#include <float.h>
#include <limits.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "schema.h"

static char *copy_string(const cJSON *value)
{
    char *copy;
    size_t length;

    if (!cJSON_IsString(value) || value->valuestring == NULL) {
        return NULL;
    }
    length = strlen(value->valuestring);
    copy = malloc(length + 1U);
    if (copy != NULL) {
        memcpy(copy, value->valuestring, length + 1U);
    }
    return copy;
}

struct RawOptions {
    bool has_constant;
    uint8_t *constant;
    uint8_t *values;
    size_t value_count;
    bool has_range;
    double range_min;
    double range_max;
};

static void write_unsigned_le(uint64_t value, uint8_t *out, size_t size)
{
    size_t i;

    for (i = 0; i < size; i++) {
        out[i] = i < sizeof(value) ?
                 (uint8_t)(value >> (i * CHAR_BIT)) : 0;
    }
}

static bool parse_byte(const cJSON *json, uint8_t *byte)
{
    double number;

    if (!cJSON_IsNumber(json)) {
        return false;
    }
    number = json->valuedouble;
    if (number < 0.0 || number > UINT8_MAX || (uint8_t)number != number) {
        return false;
    }
    *byte = (uint8_t)number;
    return true;
}

static bool raw_parse_data_constant(const cJSON *constant, struct Field *field,
                                    uint8_t *out)
{
    const cJSON *value;
    int count;
    size_t i = 0;

    if (!cJSON_IsArray(constant) ||
        (count = cJSON_GetArraySize(constant)) < 0 ||
        (size_t)count != field->size) {
        return false;
    }
    cJSON_ArrayForEach(value, constant) {
        if (!parse_byte(value, &out[i++])) {
            return false;
        }
    }
    return true;
}

static bool raw_parse_integer_constant(const cJSON *constant,
                                       const struct Field *field,
                                       uint8_t *out)
{
    double number;
    uint64_t value;

    if (!cJSON_IsNumber(constant)) {
        return false;
    }
    number = constant->valuedouble;
    if (strcmp(field->type, "uint") == 0) {
        if (number < 0.0 || number > 9007199254740991.0 ||
            (uint64_t)number != number) {
            return false;
        }
        value = (uint64_t)number;
        if (field->size == 0 ? value != 0 :
            (field->size < sizeof(value) &&
             value > (UINT64_MAX >> ((sizeof(value) - field->size) * CHAR_BIT)))) {
            return false;
        }
        write_unsigned_le(value, out, field->size);
        return true;
    }

    if (number < -9007199254740991.0 || number > 9007199254740991.0 ||
        (int64_t)number != number) {
        return false;
    }
    {
        int64_t signed_value = (int64_t)number;
        if (field->size == 0) {
            if (signed_value != 0) {
                return false;
            }
        } else if (field->size < sizeof(signed_value)) {
            unsigned int bits = (unsigned int)(field->size * CHAR_BIT);
            int64_t minimum = -(INT64_C(1) << (bits - 1U));
            int64_t maximum = (INT64_C(1) << (bits - 1U)) - 1;
            if (signed_value < minimum || signed_value > maximum) {
                return false;
            }
        }
        value = (uint64_t)signed_value;
        write_unsigned_le(value, out, field->size);
        if (signed_value < 0) {
            size_t i;
            for (i = sizeof(value); i < field->size; i++) {
                out[i] = UINT8_MAX;
            }
        }
    }
    return true;
}

static uint16_t float_to_half(float value)
{
    union {
        float value;
        uint32_t bits;
    } converted = { .value = value };
    uint32_t significand = converted.bits & UINT32_C(0x7fffff);
    uint16_t sign = (uint16_t)((converted.bits >> 16U) & UINT32_C(0x8000));
    int exponent = (int)((converted.bits >> 23U) & UINT32_C(0xff)) - 127 + 15;
    uint32_t rounded;

    if (exponent <= 0) {
        if (exponent < -10) {
            return sign;
        }
        significand |= UINT32_C(0x800000);
        rounded = (significand + (UINT32_C(1) << (13 - exponent))) >>
                  (14 - exponent);
        return sign | (uint16_t)rounded;
    }
    if (exponent >= 31) {
        return sign | UINT16_C(0x7c00);
    }
    rounded = (significand + UINT32_C(0x1000)) >> 13U;
    if (rounded == UINT32_C(0x400)) {
        rounded = 0;
        exponent++;
        if (exponent >= 31) {
            return sign | UINT16_C(0x7c00);
        }
    }
    return sign | (uint16_t)(exponent << 10U) | (uint16_t)rounded;
}

static bool raw_write_float(long double number, const struct Field *field,
                            uint8_t *out)
{
    if (field->size == 2U) {
        write_unsigned_le(float_to_half((float)number), out, field->size);
        return true;
    }
    if (field->size == 4U) {
        union {
            float value;
            uint32_t bits;
        } converted = { .value = (float)number };
        write_unsigned_le(converted.bits, out, field->size);
        return true;
    }
    if (field->size == 8U) {
        union {
            double value;
            uint64_t bits;
        } converted = { .value = (double)number };
        write_unsigned_le(converted.bits, out, field->size);
        return true;
    }
    return false;
}

static bool raw_parse_float_constant(const cJSON *constant,
                                     const struct Field *field, uint8_t *out)
{
    return cJSON_IsNumber(constant) &&
           raw_write_float(constant->valuedouble, field, out);
}

static void raw_free_options(struct Field *field)
{
    struct RawOptions *options = field->options;

    if (options != NULL) {
        free(options->constant);
        free(options->values);
        free(options);
        field->options = NULL;
    }
}

static bool raw_parse_integer_values(const cJSON *values, struct Field *field,
                                     struct RawOptions *parsed)
{
    const cJSON *value;
    int count;
    size_t value_size;
    size_t index = 0;

    if ((strcmp(field->type, "int") != 0 && strcmp(field->type, "uint") != 0) ||
        field->size == 0 || !cJSON_IsArray(values) ||
        (count = cJSON_GetArraySize(values)) <= 0 || count > UINT8_MAX) {
        return false;
    }
    value_size = field->size;
    if ((size_t)count > SIZE_MAX / value_size) {
        return false;
    }
    parsed->values = calloc((size_t)count, value_size);
    if (parsed->values == NULL) {
        return false;
    }
    cJSON_ArrayForEach(value, values) {
        if (!raw_parse_integer_constant(value, field,
                                        parsed->values + index * value_size)) {
            free(parsed->values);
            parsed->values = NULL;
            return false;
        }
        index++;
    }
    parsed->value_count = (size_t)count;
    return true;
}

static bool raw_parse_range(const cJSON *range, const struct Field *field,
                            struct RawOptions *parsed)
{
    const cJSON *minimum;
    const cJSON *maximum;
    uint8_t encoded[sizeof(uint64_t)];

    if (!cJSON_IsArray(range) || cJSON_GetArraySize(range) != 2) {
        return false;
    }
    minimum = cJSON_GetArrayItem(range, 0);
    maximum = cJSON_GetArrayItem(range, 1);
    if (!cJSON_IsNumber(minimum) || !cJSON_IsNumber(maximum) ||
        !isfinite(minimum->valuedouble) || !isfinite(maximum->valuedouble) ||
        minimum->valuedouble > maximum->valuedouble) {
        return false;
    }
    if (strcmp(field->type, "float") == 0) {
        double limit;

        if (field->size == 2U) {
            limit = 65504.0;
        } else if (field->size == 4U) {
            limit = FLT_MAX;
        } else if (field->size == 8U) {
            limit = DBL_MAX;
        } else {
            return false;
        }
        if (minimum->valuedouble < -limit || maximum->valuedouble > limit) {
            return false;
        }
    } else if ((strcmp(field->type, "int") != 0 &&
                strcmp(field->type, "uint") != 0) ||
               field->size == 0 || field->size > sizeof(encoded) ||
               !raw_parse_integer_constant(minimum, field, encoded) ||
               !raw_parse_integer_constant(maximum, field, encoded)) {
        return false;
    }
    parsed->has_range = true;
    parsed->range_min = minimum->valuedouble;
    parsed->range_max = maximum->valuedouble;
    return true;
}

static bool raw_parse_options(const cJSON *options, struct Field *field)
{
    const cJSON *value;
    const cJSON *constant = NULL;
    const cJSON *values = NULL;
    const cJSON *range = NULL;
    struct RawOptions *parsed;

    if (options == NULL) {
        return true;
    }
    if (!cJSON_IsObject(options)) {
        return false;
    }
    cJSON_ArrayForEach(value, options) {
        if (value->string == NULL) {
            return false;
        }
        if (strcmp(value->string, "constant") == 0 && constant == NULL) {
            constant = value;
        } else if (strcmp(value->string, "values") == 0 && values == NULL) {
            values = value;
        } else if (strcmp(value->string, "range") == 0 && range == NULL) {
            range = value;
        } else {
            return false;
        }
    }
    if (constant == NULL && values == NULL && range == NULL) {
        return true;
    }
    if ((constant != NULL) + (values != NULL) + (range != NULL) != 1) {
        return false;
    }
    parsed = calloc(1, sizeof(*parsed));
    if (parsed == NULL) {
        return false;
    }
    if (range != NULL) {
        if (!raw_parse_range(range, field, parsed)) {
            free(parsed);
            return false;
        }
        field->options = parsed;
        return true;
    }
    if (values != NULL) {
        if (!raw_parse_integer_values(values, field, parsed)) {
            free(parsed);
            return false;
        }
        field->options = parsed;
        return true;
    }
    parsed->constant = calloc(field->size == 0 ? 1U : field->size,
                              sizeof(*parsed->constant));
    if (parsed->constant == NULL) {
        free(parsed);
        return false;
    }
    if ((strcmp(field->type, "data") == 0 &&
         !raw_parse_data_constant(constant, field, parsed->constant)) ||
        ((strcmp(field->type, "int") == 0 || strcmp(field->type, "uint") == 0) &&
         !raw_parse_integer_constant(constant, field, parsed->constant)) ||
        (strcmp(field->type, "float") == 0 &&
         !raw_parse_float_constant(constant, field, parsed->constant))) {
        free(parsed->constant);
        free(parsed);
        return false;
    }
    parsed->has_constant = true;
    field->options = parsed;
    return true;
}

static bool raw_finalize(struct Field *field, struct Field **fields,
                         size_t field_count, struct Stream **streams,
                         size_t stream_count)
{
    (void)field;
    (void)fields;
    (void)field_count;
    (void)streams;
    (void)stream_count;
    return true;
}

static size_t raw_expected(const struct Field *field)
{
    const struct RawOptions *options = field->options;

    if (options != NULL && options->has_constant) {
        return 0;
    }
    /* One selector byte chooses among the bounded set. */
    if (options != NULL && options->value_count != 0) {
        return 1;
    }
    return field->size;
}

static bool raw_generate_range(const struct Field *field, uint8_t *out)
{
    const struct RawOptions *options = field->options;
    uint8_t bytes[sizeof(uint64_t)];
    uint64_t raw = 0;
    uint64_t source_max = UINT64_MAX >>
                          ((sizeof(uint64_t) - field->size) * CHAR_BIT);
    size_t offset = 0;
    size_t i;

    if (!schema_field_input_read(field, &offset, bytes, field->size)) {
        return false;
    }
    for (i = 0; i < field->size; i++) {
        raw |= (uint64_t)bytes[i] << (i * CHAR_BIT);
    }
    if (strcmp(field->type, "float") == 0) {
        long double minimum = options->range_min;
        long double maximum = options->range_max;
        long double value;

        if (raw == 0 || minimum == maximum) {
            value = minimum;
        } else if (raw == source_max) {
            value = maximum;
        } else {
            long double fraction = (long double)raw / source_max;

            /* Weighted interpolation also handles opposite-sign bounds whose
             * difference would overflow the output floating-point format. */
            value = (1.0L - fraction) * minimum + fraction * maximum;
            if (value < minimum) {
                value = minimum;
            }
            if (value > maximum) {
                value = maximum;
            }
        }
        return raw_write_float(value, field, out);
    }
    {
        int64_t minimum = (int64_t)options->range_min;
        uint64_t span = (uint64_t)((int64_t)options->range_max - minimum);
        /* The product needs up to 118 bits with the accepted JSON bounds.
         * The odd source maximum means exact halfway ties cannot occur. */
        uint64_t scaled = (uint64_t)(((unsigned __int128)raw * span +
                                     source_max / 2U) / source_max);

        write_unsigned_le((uint64_t)(minimum + (int64_t)scaled), out,
                          field->size);
    }
    return true;
}

static bool raw_generate(const struct Field *field, uint8_t *out,
                         size_t out_size)
{
    const struct RawOptions *options = field->options;
    size_t offset = 0;

    if (out_size != field->size) {
        return false;
    }
    if (options != NULL && options->has_range) {
        return raw_generate_range(field, out);
    }
    if (options != NULL && options->has_constant) {
        memcpy(out, options->constant, out_size);
        return true;
    }
    if (options != NULL && options->value_count != 0) {
        uint8_t selector;

        if (!schema_field_input_read(field, &offset, &selector,
                                     sizeof(selector))) {
            return false;
        }
        memcpy(out, options->values +
                    ((size_t)selector % options->value_count) * field->size,
               out_size);
        return true;
    }
    return schema_field_input_read(field, &offset, out, out_size);
}

static const struct TypeHandler raw_handler = {
    .parse_options = raw_parse_options,
    .finalize = raw_finalize,
    .expected = raw_expected,
    .generate = raw_generate,
    .free_options = raw_free_options,
};

struct LengthOptions {
    char **field_names;
    struct SchemaReference *references;
    size_t field_count;
    bool little_endian;
    bool fuzzable;
};

static void length_free_options(struct Field *field)
{
    struct LengthOptions *options = field->options;
    size_t i;

    if (options == NULL) {
        return;
    }
    for (i = 0; i < options->field_count; i++) {
        free(options->field_names[i]);
    }
    free(options->field_names);
    free(options->references);
    free(options);
    field->options = NULL;
}

static bool length_parse_fields(const cJSON *json, struct LengthOptions *options)
{
    const cJSON *item;
    int count;
    size_t i = 0;

    if (!cJSON_IsArray(json) || (count = cJSON_GetArraySize(json)) <= 0 ||
        (size_t)count > SIZE_MAX / sizeof(*options->field_names)) {
        return false;
    }
    options->field_names = calloc((size_t)count, sizeof(*options->field_names));
    options->references = calloc((size_t)count, sizeof(*options->references));
    if (options->field_names == NULL || options->references == NULL) {
        return false;
    }
    options->field_count = (size_t)count;
    cJSON_ArrayForEach(item, json) {
        options->field_names[i] = copy_string(item);
        if (options->field_names[i] == NULL) {
            return false;
        }
        i++;
    }
    return true;
}

static bool length_parse_options(const cJSON *json, struct Field *field)
{
    const cJSON *value;
    struct LengthOptions *options;
    bool got_fields = false;
    bool got_order = false;
    bool got_fuzzable = false;

    if (!cJSON_IsObject(json)) {
        return false;
    }
    options = calloc(1, sizeof(*options));
    if (options == NULL) {
        errno = ENOMEM;
        return false;
    }
    field->options = options;

    cJSON_ArrayForEach(value, json) {
        if (value->string == NULL) {
            return false;
        }
        if (strcmp(value->string, "fields") == 0 && !got_fields) {
            if (!length_parse_fields(value, options)) {
                return false;
            }
            got_fields = true;
        } else if (strcmp(value->string, "byte_order") == 0 && !got_order &&
                   cJSON_IsString(value) && value->valuestring != NULL) {
            if (strcmp(value->valuestring, "little") == 0) {
                options->little_endian = true;
            } else if (strcmp(value->valuestring, "big") != 0) {
                return false;
            }
            got_order = true;
        } else if (strcmp(value->string, "fuzzable") == 0 && !got_fuzzable &&
                   cJSON_IsBool(value)) {
            options->fuzzable = cJSON_IsTrue(value);
            got_fuzzable = true;
        } else {
            return false;
        }
    }
    return got_fields && got_order && (!options->fuzzable || field->size != SIZE_MAX);
}

static bool length_finalize(struct Field *field, struct Field **fields,
                            size_t field_count, struct Stream **streams,
                            size_t stream_count)
{
    struct LengthOptions *options = field->options;
    size_t i;

    if (options == NULL) {
        return false;
    }
    for (i = 0; i < options->field_count; i++) {
        size_t j;
        bool found = false;
        for (j = 0; j < field_count; j++) {
            if (strcmp(options->field_names[i], fields[j]->name) == 0) {
                options->references[i].kind = SchemaReferenceField;
                options->references[i].value.field = fields[j];
                found = true;
                break;
            }
        }
        if (!found) {
            for (j = 0; j < stream_count; j++) {
                if (strcmp(options->field_names[i], streams[j]->name) == 0) {
                    size_t stream_size;
                    if (!schema_stream_total_size(streams[j], &stream_size)) {
                        return false;
                    }
                    options->references[i].kind = SchemaReferenceStream;
                    options->references[i].value.stream = streams[j];
                    found = true;
                    break;
                }
            }
        }
        if (!found) {
            return false;
        }
    }
    return true;
}

static size_t length_expected(const struct Field *field)
{
    const struct LengthOptions *options = field->options;

    return options != NULL && options->fuzzable ? field->size + 1U : 0;
}

static bool length_write_value(const struct Field *field, uint8_t *out,
                               size_t out_size)
{
    const struct LengthOptions *options = field->options;
    uint64_t length = 0;
    size_t i;

    if (options == NULL || out_size != field->size) {
        return false;
    }
    for (i = 0; i < options->field_count; i++) {
        size_t component_size;
        if (options->references[i].kind == SchemaReferenceField) {
            component_size = options->references[i].value.field->size;
        } else if (!schema_stream_total_size(options->references[i].value.stream,
                                             &component_size)) {
            return false;
        }
        if (component_size > UINT64_MAX - length) {
            return false;
        }
        length += component_size;
    }
    memset(out, 0, out_size);
    for (i = 0; i < out_size && i < sizeof(length); i++) {
        size_t target = options->little_endian ? i : out_size - 1U - i;
        out[target] = (uint8_t)(length >> (i * CHAR_BIT));
    }
    return true;
}

static bool length_generate(const struct Field *field, uint8_t *out,
                            size_t out_size)
{
    const struct LengthOptions *options = field->options;
    size_t offset = 0;
    uint8_t selector;

    if (options == NULL || out_size != field->size) {
        return false;
    }
    if (!options->fuzzable) {
        return length_write_value(field, out, out_size);
    }
    if (!schema_field_input_read(field, &offset, &selector, sizeof(selector)) ||
        !schema_field_input_read(field, &offset, out, out_size)) {
        return false;
    }
    return selector < 128U || length_write_value(field, out, out_size);
}

static const struct TypeHandler length_handler = {
    .parse_options = length_parse_options,
    .finalize = length_finalize,
    .expected = length_expected,
    .generate = length_generate,
    .free_options = length_free_options,
};

struct ChecksumOptions {
    enum {
        ChecksumCrc16Modbus,
        ChecksumCrc16Mcrf4xx,
    } algorithm;
    char **names;
    struct SchemaReference *references;
    size_t reference_count;
    bool little_endian;
};

static void checksum_free_options(struct Field *field)
{
    struct ChecksumOptions *options = field->options;
    size_t i;

    if (options == NULL) {
        return;
    }
    for (i = 0; i < options->reference_count; i++) {
        free(options->names[i]);
    }
    free(options->names);
    free(options->references);
    free(options);
    field->options = NULL;
}

static bool checksum_parse_over(const cJSON *json, struct ChecksumOptions *options)
{
    const cJSON *item;
    int count;
    size_t i = 0;

    if (!cJSON_IsArray(json) || (count = cJSON_GetArraySize(json)) <= 0 ||
        (size_t)count > SIZE_MAX / sizeof(*options->names)) {
        return false;
    }
    options->names = calloc((size_t)count, sizeof(*options->names));
    options->references = calloc((size_t)count, sizeof(*options->references));
    if (options->names == NULL || options->references == NULL) {
        return false;
    }
    options->reference_count = (size_t)count;
    cJSON_ArrayForEach(item, json) {
        options->names[i] = copy_string(item);
        if (options->names[i] == NULL) {
            return false;
        }
        i++;
    }
    return true;
}

static bool checksum_parse_options(const cJSON *json, struct Field *field)
{
    const cJSON *value;
    struct ChecksumOptions *options;
    bool got_algorithm = false;
    bool got_over = false;
    bool got_order = false;

    if (field->size != 2U || !cJSON_IsObject(json)) {
        return false;
    }
    options = calloc(1, sizeof(*options));
    if (options == NULL) {
        errno = ENOMEM;
        return false;
    }
    field->options = options;
    cJSON_ArrayForEach(value, json) {
        if (value->string == NULL) {
            return false;
        }
        if (strcmp(value->string, "algorithm") == 0 && !got_algorithm &&
            cJSON_IsString(value) && value->valuestring != NULL) {
            if (strcmp(value->valuestring, "crc16-modbus") == 0) {
                options->algorithm = ChecksumCrc16Modbus;
            } else if (strcmp(value->valuestring, "crc16-mcrf4xx") == 0) {
                options->algorithm = ChecksumCrc16Mcrf4xx;
            } else {
                return false;
            }
            got_algorithm = true;
        } else if (strcmp(value->string, "over") == 0 && !got_over) {
            if (!checksum_parse_over(value, options)) {
                return false;
            }
            got_over = true;
        } else if (strcmp(value->string, "byte_order") == 0 && !got_order &&
                   cJSON_IsString(value) && value->valuestring != NULL) {
            if (strcmp(value->valuestring, "little") == 0) {
                options->little_endian = true;
            } else if (strcmp(value->valuestring, "big") != 0) {
                return false;
            }
            got_order = true;
        } else {
            return false;
        }
    }
    return got_algorithm && got_over && got_order;
}

static bool checksum_finalize(struct Field *field, struct Field **fields,
                              size_t field_count, struct Stream **streams,
                              size_t stream_count)
{
    struct ChecksumOptions *options = field->options;
    size_t i;

    if (options == NULL) {
        return false;
    }
    for (i = 0; i < options->reference_count; i++) {
        size_t j;
        bool found = false;
        for (j = 0; j < field_count; j++) {
            if (strcmp(options->names[i], fields[j]->name) == 0) {
                options->references[i].kind = SchemaReferenceField;
                options->references[i].value.field = fields[j];
                found = true;
                break;
            }
        }
        if (!found) {
            for (j = 0; j < stream_count; j++) {
                if (strcmp(options->names[i], streams[j]->name) == 0) {
                    options->references[i].kind = SchemaReferenceStream;
                    options->references[i].value.stream = streams[j];
                    found = true;
                    break;
                }
            }
        }
        if (!found) {
            return false;
        }
    }
    return true;
}

static uint16_t crc16_modbus_update(uint16_t crc, const uint8_t *data,
                                    size_t size)
{
    size_t i;

    for (i = 0; i < size; i++) {
        unsigned int bit;
        crc ^= data[i];
        for (bit = 0; bit < CHAR_BIT; bit++) {
            crc = (uint16_t)((crc >> 1U) ^ ((crc & 1U) ? 0xa001U : 0U));
        }
    }
    return crc;
}

static uint16_t crc16_mcrf4xx_update(uint16_t crc, const uint8_t *data,
                                     size_t size)
{
    size_t i;

    for (i = 0; i < size; i++) {
        uint8_t tmp = data[i] ^ (uint8_t)crc;
        tmp ^= (uint8_t)(tmp << 4U);
        crc = (uint16_t)((crc >> CHAR_BIT) ^ ((uint16_t)tmp << CHAR_BIT) ^
                         ((uint16_t)tmp << 3U) ^ (tmp >> 4U));
    }
    return crc;
}

static size_t checksum_expected(const struct Field *field)
{
    (void)field;
    return 0;
}

static bool checksum_generate(const struct Field *field, uint8_t *out,
                              size_t out_size)
{
    const struct ChecksumOptions *options = field->options;
    uint16_t crc = 0xffffU;
    size_t i;

    if (options == NULL || out == NULL || out_size != 2U) {
        return false;
    }
    for (i = 0; i < options->reference_count; i++) {
        const uint8_t *data;
        size_t size;
        if (options->references[i].kind == SchemaReferenceField) {
            if (!schema_stream_reserve_reference(
                    options->references[i].value.field)) {
                return false;
            }
            if (!schema_field_materialize(options->references[i].value.field,
                                          &data, &size)) {
                return false;
            }
        } else {
            data = schema_stream_emitted(options->references[i].value.stream,
                                         &size);
            if (data == NULL && size != 0) {
                return false;
            }
        }
        if (options->algorithm == ChecksumCrc16Modbus) {
            crc = crc16_modbus_update(crc, data, size);
        } else if (options->algorithm == ChecksumCrc16Mcrf4xx) {
            crc = crc16_mcrf4xx_update(crc, data, size);
        } else {
            return false;
        }
    }
    if (options->little_endian) {
        out[0] = (uint8_t)crc;
        out[1] = (uint8_t)(crc >> CHAR_BIT);
    } else {
        out[0] = (uint8_t)(crc >> CHAR_BIT);
        out[1] = (uint8_t)crc;
    }
    return true;
}

static const struct TypeHandler checksum_handler = {
    .parse_options = checksum_parse_options,
    .finalize = checksum_finalize,
    .expected = checksum_expected,
    .generate = checksum_generate,
    .free_options = checksum_free_options,
};

const struct TypeHandler *schema_find_type_handler(const char *name)
{
    if (name == NULL) {
        return NULL;
    }
    if (strcmp(name, "int") == 0 || strcmp(name, "uint") == 0 ||
        strcmp(name, "float") == 0 || strcmp(name, "data") == 0) {
        return &raw_handler;
    }
    if (strcmp(name, "length") == 0) {
        return &length_handler;
    }
    if (strcmp(name, "checksum") == 0) {
        return &checksum_handler;
    }
    return NULL;
}
