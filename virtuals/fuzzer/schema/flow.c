#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "common.h"
#include "core.h"
#include "schema.h"
#include "virtuals.h"

static bool parse_address(const cJSON *value, uint64_t *address)
{
    char *end;
    unsigned long long parsed;

    if (cJSON_IsNumber(value) && value->valuedouble >= 0.0 &&
        (uint64_t)value->valuedouble == value->valuedouble) {
        *address = (uint64_t)value->valuedouble;
        return true;
    }
    if (!cJSON_IsString(value) || value->valuestring == NULL) {
        return false;
    }
    errno = 0;
    parsed = strtoull(value->valuestring, &end, 0);
    if (errno == ERANGE || end == value->valuestring || *end != '\0') {
        return false;
    }
    *address = (uint64_t)parsed;
    return true;
}

static bool parse_point(const cJSON *json, struct SchemaFlowPoint *point)
{
    const cJSON *value;
    bool got_at = false;
    bool got_resume = false;
    bool got_return_address = false;

    if (!cJSON_IsObject(json)) {
        return false;
    }
    cJSON_ArrayForEach(value, json) {
        if (value->string == NULL) {
            return false;
        }
        if (strcmp(value->string, "at") == 0 && !got_at) {
            if (!parse_address(value, &point->at)) {
                return false;
            }
            got_at = true;
        } else if (strcmp(value->string, "resume") == 0 && !got_resume) {
            if (!parse_address(value, &point->resume)) {
                return false;
            }
            got_resume = true;
        } else if (strcmp(value->string, "return_address") == 0 &&
                   !got_return_address) {
            if (!parse_address(value, &point->return_address)) {
                return false;
            }
            got_return_address = true;
        } else {
            return false;
        }
    }
    if (!got_at) {
        return false;
    }
    point->present = true;
    point->has_resume = got_resume;
    point->has_return_address = got_return_address;
    return true;
}

static bool parse_sync_point(const cJSON *json, struct SchemaFlowSyncPoint *point)
{
    const cJSON *value;
    bool got_at = false;
    bool got_resume = false;

    if (!cJSON_IsObject(json)) {
        return false;
    }
    cJSON_ArrayForEach(value, json) {
        if (value->string == NULL) {
            return false;
        }
        if (strcmp(value->string, "at") == 0 && !got_at) {
            if (!parse_address(value, &point->at)) {
                return false;
            }
            got_at = true;
        } else if (strcmp(value->string, "resume") == 0 && !got_resume) {
            if (!parse_address(value, &point->resume)) {
                return false;
            }
            got_resume = true;
        } else {
            return false;
        }
    }
    return got_at && got_resume;
}

static bool parse_sync_points(const cJSON *json, struct SchemaFlow *flow)
{
    const cJSON *item;
    int count;
    size_t i = 0;

    if (!cJSON_IsArray(json) || (count = cJSON_GetArraySize(json)) <= 0 ||
        (size_t)count > SIZE_MAX / sizeof(*flow->sync_points)) {
        return false;
    }
    flow->sync_points = calloc((size_t)count, sizeof(*flow->sync_points));
    if (flow->sync_points == NULL) {
        errno = ENOMEM;
        return false;
    }
    cJSON_ArrayForEach(item, json) {
        if (!parse_sync_point(item, &flow->sync_points[i++])) {
            free(flow->sync_points);
            flow->sync_points = NULL;
            return false;
        }
    }
    flow->sync_point_count = (size_t)count;
    return true;
}

static bool parse_register(const cJSON *value, int *reg)
{
    uint64_t parsed;

    if (reg == NULL || !cJSON_IsNumber(value) || value->valuedouble < 0.0 ||
        (uint64_t)value->valuedouble != value->valuedouble) {
        return false;
    }
    parsed = (uint64_t)value->valuedouble;
    if (parsed > INT_MAX) {
        return false;
    }
    *reg = (int)parsed;
    return true;
}

static bool parse_setup_update(const cJSON *json,
                               struct SchemaFlowSetupUpdate *update)
{
    const cJSON *value;
    bool got_at = false;
    bool got_reg = false;
    bool got_value = false;

    if (!cJSON_IsObject(json)) {
        return false;
    }
    cJSON_ArrayForEach(value, json) {
        if (value->string == NULL) {
            return false;
        }
        if (strcmp(value->string, "at") == 0 && !got_at) {
            if (!parse_address(value, &update->at)) {
                return false;
            }
            got_at = true;
        } else if (strcmp(value->string, "register") == 0 && !got_reg) {
            if (!parse_register(value, &update->reg)) {
                return false;
            }
            got_reg = true;
        } else if (strcmp(value->string, "value") == 0 && !got_value) {
            if (!parse_address(value, &update->value)) {
                return false;
            }
            got_value = true;
        } else {
            return false;
        }
    }
    return got_at && got_reg && got_value;
}

static bool parse_setup_updates(const cJSON *json, struct SchemaFlow *flow)
{
    const cJSON *item;
    int count;
    size_t i = 0;

    if (!cJSON_IsArray(json) || (count = cJSON_GetArraySize(json)) <= 0 ||
        (size_t)count > SIZE_MAX / sizeof(*flow->setup_updates)) {
        return false;
    }
    flow->setup_updates = calloc((size_t)count, sizeof(*flow->setup_updates));
    if (flow->setup_updates == NULL) {
        errno = ENOMEM;
        return false;
    }
    cJSON_ArrayForEach(item, json) {
        if (!parse_setup_update(item, &flow->setup_updates[i++])) {
            free(flow->setup_updates);
            flow->setup_updates = NULL;
            return false;
        }
    }
    flow->setup_update_count = (size_t)count;
    return true;
}

void schema_flow_free(struct SchemaFlow *flow)
{
    if (flow == NULL) {
        return;
    }
    free(flow->sync_points);
    free(flow->setup_updates);
    memset(flow, 0, sizeof(*flow));
}

bool schema_flow_parse(const cJSON *json, struct SchemaFlow *flow)
{
    const cJSON *value;
    bool got_state = false;
    bool got_snap = false;
    bool got_sync = false;
    bool got_setup = false;

    if (flow == NULL || !cJSON_IsObject(json)) {
        return false;
    }
    memset(flow, 0, sizeof(*flow));
    cJSON_ArrayForEach(value, json) {
        if (value->string == NULL) {
            goto fail;
        }
        if (strcmp(value->string, "state") == 0 && !got_state) {
            if (!parse_point(value, &flow->state)) {
                goto fail;
            }
            got_state = true;
        } else if (strcmp(value->string, "snap") == 0 && !got_snap) {
            if (!parse_point(value, &flow->snap)) {
                goto fail;
            }
            got_snap = true;
        } else if (strcmp(value->string, "sync") == 0 && !got_sync) {
            if (!parse_sync_points(value, flow)) {
                goto fail;
            }
            got_sync = true;
        } else if (strcmp(value->string, "setup") == 0 && !got_setup) {
            if (!parse_setup_updates(value, flow)) {
                goto fail;
            }
            got_setup = true;
        } else {
            goto fail;
        }
    }
    /* A schema-owned flow must form a complete per-input loop. The optional
     * state point only controls the longer-lived initialization snapshot. */
    if (got_snap && got_sync) {
        return true;
    }

fail:
    schema_flow_free(flow);
    return false;
}

static bool install_point(const struct SchemaFlowPoint *point, const char *name)
{
    cb_func_t callback;

    if (!point->present) {
        return true;
    }
    callback = lookup_callback(name);
    /* fuzz_sync_point uses non-NULL callback userdata as its activation
     * guard.  Schema-owned rules need a stable, nonempty marker just like
     * rules parsed from a virtuals file; an empty argument is normalized to
     * NULL by QEMU's plugin dispatcher and otherwise drops the first input. */
    return callback != NULL &&
           core_register_virtual_rule(point->at, callback, "schema-flow") &&
           (!point->has_return_address ||
            core_register_register_update(point->at, ARM_V7M_LR,
                                          point->return_address)) &&
           (!point->has_resume ||
            core_register_register_update(point->at, ARM_V7M_PC,
                                          point->resume));
}

static bool install_sync_point(const struct SchemaFlowSyncPoint *point)
{
    cb_func_t callback = lookup_callback("fuzz_sync_point");

    return callback != NULL &&
           core_register_virtual_rule(point->at, callback, "schema-flow") &&
           core_register_register_update(point->at, ARM_V7M_PC, point->resume);
}

bool schema_flow_install(const struct SchemaFlow *flow)
{
    size_t i;

    if (flow == NULL || !install_point(&flow->state, "fuzz_state_point") ||
        !install_point(&flow->snap, "fuzz_snap_point")) {
        return false;
    }
    for (i = 0; i < flow->sync_point_count; i++) {
        if (!install_sync_point(&flow->sync_points[i])) {
            return false;
        }
    }
    for (i = 0; i < flow->setup_update_count; i++) {
        if (!core_register_register_update(flow->setup_updates[i].at,
                                           flow->setup_updates[i].reg,
                                           flow->setup_updates[i].value)) {
            return false;
        }
    }
    return true;
}
