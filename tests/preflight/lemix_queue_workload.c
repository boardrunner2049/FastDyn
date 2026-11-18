/* Finite whole-program workload for the FreeRTOS POSIX/Linux port.
 *
 * This is an evaluation harness, not firmware logic.  It supplies a completed-
 * work metric that the upstream, wall-clock-driven full demo does not expose.
 */

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "FreeRTOS.h"
#include "queue.h"
#include "task.h"

#ifndef LEMIX_MESSAGE_COUNT
#define LEMIX_MESSAGE_COUNT 2000000u
#endif

typedef struct Message
{
    uint32_t sequence;
    uint32_t value;
} Message;

static QueueHandle_t WorkQueue;
static uint32_t MessageCount = LEMIX_MESSAGE_COUNT;
static StaticTask_t IdleTaskTCB;
static StackType_t IdleTaskStack[ configMINIMAL_STACK_SIZE ];
static StaticTask_t TimerTaskTCB;
static StackType_t TimerTaskStack[ configTIMER_TASK_STACK_DEPTH ];

static uint32_t mix_value( uint32_t value )
{
    value ^= value >> 16;
    value *= UINT32_C( 0x7feb352d );
    value ^= value >> 15;
    value *= UINT32_C( 0x846ca68b );
    return value ^ ( value >> 16 );
}

static void producer( void * ignored )
{
    ( void ) ignored;
    for( uint32_t sequence = 0; sequence < MessageCount; ++sequence )
    {
        Message message = { sequence, mix_value( sequence ) };
        configASSERT( xQueueSend( WorkQueue, &message, portMAX_DELAY ) == pdPASS );
    }
    vTaskDelete( NULL );
}

static void consumer( void * ignored )
{
    uint64_t checksum = 0;
    ( void ) ignored;
    for( uint32_t expected = 0; expected < MessageCount; ++expected )
    {
        Message message;
        configASSERT( xQueueReceive( WorkQueue, &message, portMAX_DELAY ) == pdPASS );
        configASSERT( message.sequence == expected );
        checksum += message.value;
    }
    printf( "RESULT messages=%u checksum=%" PRIu64 " tick=%u\n",
            MessageCount,
            checksum,
            ( unsigned ) xTaskGetTickCount() );
    fflush( stdout );
    exit( EXIT_SUCCESS );
}

static uint32_t parse_message_count( int argc, char ** argv )
{
    char * end = NULL;
    unsigned long parsed;

    if( argc == 1 )
    {
        return LEMIX_MESSAGE_COUNT;
    }
    if( argc != 3 || strcmp( argv[ 1 ], "--messages" ) != 0 )
    {
        fputs( "usage: lemix_queue_workload [--messages COUNT]\n", stderr );
        exit( 64 );
    }
    parsed = strtoul( argv[ 2 ], &end, 10 );
    if( end == argv[ 2 ] || *end != '\0' || parsed == 0 || parsed > UINT32_MAX )
    {
        fputs( "invalid message count\n", stderr );
        exit( 64 );
    }
    return ( uint32_t ) parsed;
}

int main( int argc, char ** argv )
{
    MessageCount = parse_message_count( argc, argv );
    WorkQueue = xQueueCreate( 64, sizeof( Message ) );
    configASSERT( WorkQueue != NULL );
    configASSERT( xTaskCreate( producer, "producer", configMINIMAL_STACK_SIZE,
                               NULL, 2, NULL ) == pdPASS );
    configASSERT( xTaskCreate( consumer, "consumer", configMINIMAL_STACK_SIZE,
                               NULL, 2, NULL ) == pdPASS );
    vTaskStartScheduler();
    return EXIT_FAILURE;
}

void vApplicationIdleHook( void ) {}
void vApplicationTickHook( void ) {}
void vApplicationDaemonTaskStartupHook( void ) {}

void vApplicationMallocFailedHook( void )
{
    fputs( "ASSERT malloc failed\n", stderr );
    abort();
}

void vApplicationStackOverflowHook( TaskHandle_t task, char * name )
{
    ( void ) task;
    fprintf( stderr, "ASSERT stack overflow: %s\n", name );
    abort();
}

void vAssertCalled( const char * const file, unsigned long line )
{
    fprintf( stderr, "ASSERT %s:%lu\n", file, line );
    abort();
}

void vApplicationGetIdleTaskMemory( StaticTask_t ** tcb,
                                    StackType_t ** stack,
                                    configSTACK_DEPTH_TYPE * depth )
{
    *tcb = &IdleTaskTCB;
    *stack = IdleTaskStack;
    *depth = configMINIMAL_STACK_SIZE;
}

void vApplicationGetTimerTaskMemory( StaticTask_t ** tcb,
                                     StackType_t ** stack,
                                     configSTACK_DEPTH_TYPE * depth )
{
    *tcb = &TimerTaskTCB;
    *stack = TimerTaskStack;
    *depth = configTIMER_TASK_STACK_DEPTH;
}

void vConfigureTimerForRunTimeStats( void ) {}

unsigned long ulGetRunTimeCounterValue( void )
{
    struct timespec now;
    clock_gettime( CLOCK_MONOTONIC, &now );
    return ( unsigned long ) ( now.tv_sec * 1000000UL + now.tv_nsec / 1000UL );
}
