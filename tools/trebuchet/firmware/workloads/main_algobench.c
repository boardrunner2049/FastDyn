/* Compute-kernel workload in one translation unit: linked-list reversal and
 * search, a small integer matrix product, a character state machine and a
 * bitwise CRC.  The compiler can inline most of it, so it shows what is left
 * for a post-link optimizer on tight compute code.  Work is an iteration
 * count. */
#include <stdint.h>
#include "FreeRTOS.h"
#include "task.h"
#include "fw_common.h"

#define ALGO_SCALE 12u
#define NODES 48u
#define DIM 6u
typedef struct node { struct node *next; int16_t key; int16_t idx; } node_t;
static node_t pool[NODES];
static int16_t ma[DIM][DIM], mb[DIM][DIM];
static int32_t mc[DIM][DIM];

static uint32_t rnd(uint32_t *s) { uint32_t x = *s; x ^= x << 13; x ^= x >> 17; x ^= x << 5; return *s = x; }

static uint16_t crc_bits(uint16_t crc, uint8_t b)
{
    crc ^= (uint16_t)(b << 8);
    for (int i = 0; i < 8; i++) crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
    return crc;
}

static node_t *list_reverse(node_t *h)
{
    node_t *p = NULL;
    while (h) { node_t *n = h->next; h->next = p; p = h; h = n; }
    return p;
}

static const node_t *list_find(const node_t *h, int16_t key)
{
    while (h && h->key != key) h = h->next;
    return h;
}

static void matrix_mul(void)
{
    for (unsigned i = 0; i < DIM; i++)
        for (unsigned j = 0; j < DIM; j++) {
            int32_t acc = 0;
            for (unsigned k = 0; k < DIM; k++) acc += (int32_t)ma[i][k] * mb[k][j];
            mc[i][j] = acc;
        }
}

static unsigned state_machine(const uint8_t *s, unsigned n)
{
    enum { ST_START, ST_INT, ST_FRAC, ST_EXP, ST_WORD, ST_BAD } st = ST_START;
    unsigned transitions = 0;
    for (unsigned i = 0; i < n; i++) {
        uint8_t c = s[i];
        switch (st) {
        case ST_START: st = (c >= '0' && c <= '9') ? ST_INT : (c >= 'a' && c <= 'z') ? ST_WORD : ST_BAD; break;
        case ST_INT:   st = (c >= '0' && c <= '9') ? ST_INT : (c == '.') ? ST_FRAC : (c == 'e') ? ST_EXP : ST_START; break;
        case ST_FRAC:  st = (c >= '0' && c <= '9') ? ST_FRAC : (c == 'e') ? ST_EXP : ST_START; break;
        case ST_EXP:   st = (c >= '0' && c <= '9') ? ST_EXP : ST_START; break;
        case ST_WORD:  st = (c >= 'a' && c <= 'z') ? ST_WORD : ST_START; break;
        default:       st = ST_START; break;
        }
        transitions += (unsigned)st;
    }
    return transitions;
}

static void algo_task(void *arg)
{
    uint32_t seed = fw_param_seed, sum = 0;
    const uint32_t n = fw_param_frames * ALGO_SCALE;
    static const char alphabet[] = "0123456789.eabcxyz ,";
    uint8_t text[40];
    node_t *head = NULL;
    (void)arg;
    for (unsigned i = 0; i < NODES; i++) { pool[i].next = head; pool[i].key = (int16_t)(i * 7u); pool[i].idx = (int16_t)i; head = &pool[i]; }
    for (uint32_t it = 0; it < n; it++) {
        uint32_t r = rnd(&seed);
        head = list_reverse(head);
        const node_t *hit = list_find(head, (int16_t)((r % NODES) * 7u));
        sum += hit ? (uint32_t)hit->idx : 99u;
        for (unsigned i = 0; i < DIM; i++)
            for (unsigned j = 0; j < DIM; j++) { ma[i][j] = (int16_t)(r >> (i + j)); mb[i][j] = (int16_t)(r >> (2 * i + 1)) ^ (int16_t)j; }
        matrix_mul();
        sum ^= (uint32_t)mc[it % DIM][(it >> 3) % DIM];
        for (unsigned i = 0; i < sizeof text; i++) text[i] = (uint8_t)alphabet[rnd(&seed) % (sizeof alphabet - 1)];
        sum += state_machine(text, sizeof text);
        uint16_t crc = 0xffff;
        for (unsigned i = 0; i < sizeof text; i++) crc = crc_bits(crc, text[i]);
        sum = sum * 17u + crc;
    }
    fw_finish(sum, n);
}

void algobench_start(void)
{
    xTaskCreate(algo_task, "algo", configMINIMAL_STACK_SIZE * 4, NULL, tskIDLE_PRIORITY + 1, NULL);
}
