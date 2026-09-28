/*
 * channel.c — api_channel_* implementation
 *
 * Currently a thin dispatcher onto the shm transport (see
 * transport/shm.c). BrChannel is the public opaque handle; internally
 * it holds a BrShmChannel* and the payload_size negotiated at attach.
 *
 * All attach calls that return NULL are silent as far as the model is
 * concerned — the model handles NULL by falling through to its static
 * default sample source. Any diagnostic printing (bad URI, mmap failure,
 * schema mismatch) happens inside the transport layer.
 */

#include <boardrunner/channel.h>
#include "../transport/shm_internal.h"

#include <stdlib.h>
#include <string.h>

struct BrChannel {
    BrShmChannel* shm;      /* only backing transport for now */
    size_t        payload_size;
    BrRole        role;
};

BrChannel* api_channel_attach(const char* uri,
                              BrRole role,
                              size_t payload_size)
{
    if (uri == NULL || uri[0] == '\0') {
        return NULL;
    }

    BrShmChannel* shm = br_shm_attach(uri, role, payload_size);
    if (shm == NULL) {
        return NULL;
    }

    BrChannel* ch = calloc(1, sizeof(*ch));
    if (ch == NULL) {
        br_shm_detach(shm);
        return NULL;
    }
    ch->shm          = shm;
    ch->payload_size = payload_size;
    ch->role         = role;
    return ch;
}

BrChannel* api_channel_attach_from_config(ConfigSection* cfg,
                                          const char* key,
                                          BrRole role,
                                          size_t payload_size)
{
    /* ConfigSection is a top-level section, not a per-slave descriptor. The
     * per-slave data_source URI is delivered by spi_helper.c through the
     * slave_spi_init hook, not through ConfigSection walking, so this
     * function is currently a no-op that returns NULL and lets the caller
     * take the static-default path.
     *
     * A future change may extend ConfigSection with a generic key/value
     * bag so bus-level models (USART, DMA) can also attach via TOML — at
     * that point this function gets a real implementation. Kept in the
     * header so the surface is stable. */
    (void)cfg;
    (void)key;
    (void)role;
    (void)payload_size;
    return NULL;
}

bool api_channel_read(BrChannel* ch, void* out, size_t n)
{
    if (ch == NULL || out == NULL) {
        return false;
    }
    if (n != ch->payload_size) {
        return false;   /* caller passed a mismatched size — bug in packer */
    }
    return br_shm_read(ch->shm, out);
}

bool api_channel_write(BrChannel* ch, const void* in, size_t n)
{
    if (ch == NULL || in == NULL) {
        return false;
    }
    if (n != ch->payload_size) {
        return false;
    }
    return br_shm_write(ch->shm, in);
}

void api_channel_detach(BrChannel* ch)
{
    if (ch == NULL) {
        return;
    }
    br_shm_detach(ch->shm);
    free(ch);
}
