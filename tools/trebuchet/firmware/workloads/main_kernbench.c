/* Kernel-service workload: two tasks exchange records through a stream
 * buffer and also exercise task notifications, an event group, a counting
 * semaphore and a recursive mutex.  Work is a record count; nothing waits on
 * time. */
#include <stdint.h>
#include <string.h>
#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"
#include "event_groups.h"
#include "stream_buffer.h"
#include "fw_common.h"
#include "fw_proto.h"

#define KERN_SCALE 3u
typedef struct { uint32_t seq; uint32_t value; uint16_t crc; uint16_t pad; } record_t;

static StreamBufferHandle_t sb;
static EventGroupHandle_t eg;
static SemaphoreHandle_t counter, rmutex;
static TaskHandle_t consumer_task;
static volatile uint32_t shared_total;

static void account(uint32_t v)
{
    xSemaphoreTakeRecursive(rmutex, portMAX_DELAY);
    shared_total += v;
    xSemaphoreGiveRecursive(rmutex);
}

static void producer(void *arg)
{
    uint32_t seed = fw_param_seed;
    const uint32_t n = fw_param_frames * KERN_SCALE;
    (void)arg;
    for (uint32_t i = 0; i < n; i++) {
        record_t r;
        r.seq = i; r.value = fw_prng(&seed); r.pad = 0;
        r.crc = fw_crc16((const uint8_t *)&r, 8);
        xStreamBufferSend(sb, &r, sizeof r, portMAX_DELAY);
        if ((i & 3u) == 0) xTaskNotifyGive(consumer_task);
        xEventGroupSetBits(eg, (EventBits_t)1u << (i & 7u));
        xSemaphoreGive(counter);
        account(r.value & 0xffu);
    }
    vTaskDelete(NULL);
}

static void consumer(void *arg)
{
    const uint32_t n = fw_param_frames * KERN_SCALE;
    uint32_t sum = 0, notes = 0, bits = 0, taken = 0;
    (void)arg;
    for (uint32_t got = 0; got < n; got++) {
        record_t r;
        size_t have = 0;
        while (have < sizeof r)
            have += xStreamBufferReceive(sb, (uint8_t *)&r + have, sizeof r - have, portMAX_DELAY);
        if (fw_crc16((const uint8_t *)&r, 8) == r.crc) sum = sum * 33u + r.value + r.seq;
        notes += ulTaskNotifyTake(pdTRUE, 0);
        bits ^= xEventGroupClearBits(eg, 0xffu);
        if (xSemaphoreTake(counter, 0) == pdTRUE) taken++;
        account(r.value >> 24);
    }
    fw_finish(sum ^ (notes << 16) ^ bits ^ taken ^ shared_total, n);
}

void kernbench_start(void)
{
    sb = xStreamBufferCreate(64, sizeof(record_t));
    eg = xEventGroupCreate();
    counter = xSemaphoreCreateCounting(1000000, 0);
    rmutex = xSemaphoreCreateRecursiveMutex();
    configASSERT(sb && eg && counter && rmutex);
    xTaskCreate(producer, "kprod", configMINIMAL_STACK_SIZE * 2, NULL, tskIDLE_PRIORITY + 1, NULL);
    xTaskCreate(consumer, "kcons", configMINIMAL_STACK_SIZE * 2, NULL, tskIDLE_PRIORITY + 2, &consumer_task);
}
