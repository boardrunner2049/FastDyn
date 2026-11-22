/* Fixed-work, firmware-like FreeRTOS workload: a producer task encodes frames
 * and sends them through a queue in chunks; a consumer task parses them and
 * updates a mutex-protected register map.  Work is defined by frame count. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "semphr.h"
#include "fw_proto.h"

#define CHUNK 8u
typedef struct { uint8_t n; uint8_t b[CHUNK]; } chunk_t;

static QueueHandle_t q;
static SemaphoreHandle_t lock;
static fw_regs_t regs;
static fw_parser_t parser;
static fw_rb_t rb;
static volatile uint32_t done_frames;
#include "fw_common.h"

static void producer(void *arg)
{
    uint32_t seed = fw_param_seed;
    const uint32_t frames = fw_param_frames;
    uint8_t payload[FW_MAX_PAYLOAD], frame[FW_MAX_PAYLOAD + 5];
    (void)arg;
    for (uint32_t f = 0; f < frames; f++) {
        uint32_t r = fw_prng(&seed);
        uint8_t type = (uint8_t)(r & 3), len = (uint8_t)(5 + ((r >> 2) % 16));
        for (uint8_t i = 0; i < len; i++) payload[i] = (uint8_t)fw_prng(&seed);
        size_t n = fw_frame_encode(frame, type, payload, len);
        for (size_t off = 0; off < n; off += CHUNK) {
            chunk_t c;
            c.n = (uint8_t)((n - off) < CHUNK ? (n - off) : CHUNK);
            memcpy(c.b, &frame[off], c.n);
            xQueueSend(q, &c, portMAX_DELAY);
        }
    }
    vTaskDelete(NULL);
}

static void consumer(void *arg)
{
    chunk_t c;
    uint8_t byte;
    (void)arg;
    const uint32_t frames = fw_param_frames;
    while (done_frames < frames) {
        xQueueReceive(q, &c, portMAX_DELAY);
        for (uint8_t i = 0; i < c.n; i++) fw_rb_put(&rb, c.b[i]);
        while (fw_rb_get(&rb, &byte)) {
            xSemaphoreTake(lock, portMAX_DELAY);
            int complete = fw_parser_feed(&parser, &regs, byte);
            xSemaphoreGive(lock);
            if (complete) done_frames++;
        }
    }
    fw_finish(regs.digest ^ regs.pings ^ (uint32_t)regs.filt ^
              (parser.frames_ok << 8) ^ parser.frames_bad, frames);
    for (;;) taskYIELD();
}

void fwbench_start(void)
{
    q = xQueueCreate(4, sizeof(chunk_t));
    lock = xSemaphoreCreateMutex();
    configASSERT(q && lock);
    xTaskCreate(producer, "prod", configMINIMAL_STACK_SIZE * 2, NULL, tskIDLE_PRIORITY + 1, NULL);
    xTaskCreate(consumer, "cons", configMINIMAL_STACK_SIZE * 2, NULL, tskIDLE_PRIORITY + 2, NULL);
}
