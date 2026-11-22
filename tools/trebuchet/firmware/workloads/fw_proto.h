/* Firmware-like protocol library: framing, CRC, ring buffer, register map. */
#ifndef FW_PROTO_H
#define FW_PROTO_H
#include <stdint.h>
#include <stddef.h>

#define FW_SOF 0x7eu
#define FW_MAX_PAYLOAD 24u
#define FW_RB_SIZE 64u

typedef struct { uint8_t data[FW_RB_SIZE]; uint16_t head, tail; } fw_rb_t;
typedef enum { PS_SOF, PS_TYPE, PS_LEN, PS_PAYLOAD, PS_CRC_HI, PS_CRC_LO } fw_pstate_t;
typedef struct {
    fw_pstate_t state; uint8_t type, len, pos; uint16_t crc, rx_crc;
    uint8_t payload[FW_MAX_PAYLOAD];
    uint32_t frames_ok, frames_bad;
} fw_parser_t;
typedef struct { int32_t reg[16]; int32_t filt; uint32_t pings; uint32_t digest; } fw_regs_t;

uint16_t fw_crc16_update(uint16_t crc, uint8_t byte);
uint16_t fw_crc16(const uint8_t *data, size_t len);
int fw_rb_put(fw_rb_t *rb, uint8_t byte);
int fw_rb_get(fw_rb_t *rb, uint8_t *byte);
unsigned fw_rb_count(const fw_rb_t *rb);
size_t fw_frame_encode(uint8_t *out, uint8_t type, const uint8_t *payload, uint8_t len);
int fw_parser_feed(fw_parser_t *p, fw_regs_t *regs, uint8_t byte);
int32_t fw_filter_step(fw_regs_t *regs, int32_t sample);
uint32_t fw_prng(uint32_t *state);
#endif
