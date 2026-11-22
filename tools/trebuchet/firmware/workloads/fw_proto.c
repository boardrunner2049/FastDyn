#include <string.h>
#include "fw_proto.h"

static const uint16_t crc_tab[16] = {
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50a5, 0x60c6, 0x70e7,
    0x8108, 0x9129, 0xa14a, 0xb16b, 0xc18c, 0xd1ad, 0xe1ce, 0xf1ef };

uint16_t fw_crc16_update(uint16_t crc, uint8_t byte)
{
    crc = (uint16_t)((crc << 4) ^ crc_tab[((crc >> 12) ^ (byte >> 4)) & 0xf]);
    crc = (uint16_t)((crc << 4) ^ crc_tab[((crc >> 12) ^ byte) & 0xf]);
    return crc;
}

uint16_t fw_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xffff;
    while (len--) crc = fw_crc16_update(crc, *data++);
    return crc;
}

unsigned fw_rb_count(const fw_rb_t *rb)
{
    return (unsigned)(rb->head - rb->tail) % FW_RB_SIZE;
}

int fw_rb_put(fw_rb_t *rb, uint8_t byte)
{
    uint16_t next = (uint16_t)((rb->head + 1u) % FW_RB_SIZE);
    if (next == rb->tail) return 0;
    rb->data[rb->head] = byte;
    rb->head = next;
    return 1;
}

int fw_rb_get(fw_rb_t *rb, uint8_t *byte)
{
    if (rb->head == rb->tail) return 0;
    *byte = rb->data[rb->tail];
    rb->tail = (uint16_t)((rb->tail + 1u) % FW_RB_SIZE);
    return 1;
}

uint32_t fw_prng(uint32_t *state)
{
    uint32_t x = *state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    *state = x;
    return x;
}

size_t fw_frame_encode(uint8_t *out, uint8_t type, const uint8_t *payload, uint8_t len)
{
    uint16_t crc = 0xffff;
    size_t n = 0;
    out[n++] = FW_SOF;
    out[n++] = type; crc = fw_crc16_update(crc, type);
    out[n++] = len;  crc = fw_crc16_update(crc, len);
    memcpy(&out[n], payload, len);
    for (uint8_t i = 0; i < len; i++) crc = fw_crc16_update(crc, payload[i]);
    n += len;
    out[n++] = (uint8_t)(crc >> 8);
    out[n++] = (uint8_t)crc;
    return n;
}

int32_t fw_filter_step(fw_regs_t *regs, int32_t sample)
{
    /* first-order IIR, Q8 */
    regs->filt += ((sample << 8) - regs->filt) >> 3;
    return regs->filt >> 8;
}

static void handle_set(fw_regs_t *regs, const uint8_t *p, uint8_t len)
{
    if (len < 5) return;
    int32_t v;
    memcpy(&v, &p[1], sizeof v);
    regs->reg[p[0] & 15] = v;
    regs->digest = regs->digest * 31u + (uint32_t)v;
}

static void handle_get(fw_regs_t *regs, const uint8_t *p, uint8_t len)
{
    if (len < 1) return;
    regs->digest ^= (uint32_t)regs->reg[p[0] & 15];
}

static void handle_sample(fw_regs_t *regs, const uint8_t *p, uint8_t len)
{
    for (uint8_t i = 0; i + 1 < len; i += 2) {
        int16_t s = (int16_t)(p[i] | (p[i + 1] << 8));
        regs->digest += (uint32_t)fw_filter_step(regs, s);
    }
}

static void handle_ping(fw_regs_t *regs, const uint8_t *p, uint8_t len)
{
    (void)p; (void)len;
    regs->pings++;
}

typedef void (*fw_handler_t)(fw_regs_t *, const uint8_t *, uint8_t);
static const fw_handler_t handlers[4] = { handle_set, handle_get, handle_sample, handle_ping };

int fw_parser_feed(fw_parser_t *p, fw_regs_t *regs, uint8_t byte)
{
    switch (p->state) {
    case PS_SOF:
        if (byte == FW_SOF) { p->state = PS_TYPE; p->crc = 0xffff; }
        break;
    case PS_TYPE:
        p->type = byte; p->crc = fw_crc16_update(p->crc, byte); p->state = PS_LEN;
        break;
    case PS_LEN:
        if (byte > FW_MAX_PAYLOAD) { p->state = PS_SOF; p->frames_bad++; break; }
        p->len = byte; p->pos = 0; p->crc = fw_crc16_update(p->crc, byte);
        p->state = byte ? PS_PAYLOAD : PS_CRC_HI;
        break;
    case PS_PAYLOAD:
        p->payload[p->pos++] = byte; p->crc = fw_crc16_update(p->crc, byte);
        if (p->pos == p->len) p->state = PS_CRC_HI;
        break;
    case PS_CRC_HI:
        p->rx_crc = (uint16_t)(byte << 8); p->state = PS_CRC_LO;
        break;
    case PS_CRC_LO:
        p->rx_crc |= byte; p->state = PS_SOF;
        if (p->rx_crc == p->crc) {
            p->frames_ok++;
            handlers[p->type & 3](regs, p->payload, p->len);
            return 1;
        }
        p->frames_bad++;
        break;
    }
    return 0;
}
