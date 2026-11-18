/* Fixed-work tick driver for the unchanged LEMIX FreeRTOS POSIX binary.
 *
 * The target normally requests a 1 ms ITIMER_REAL period.  For a separately
 * identified capacity experiment, this interposer disables that timer and
 * injects one SIGALRM at a time.  It waits for the non-PIE FreeRTOS xTickCount
 * object to change after every signal and stops after an exact number of
 * Interrupt_Handler_Task markers.  Each new marker proves that the prior
 * interrupt-handler iteration completed.  This is a closed-loop, fixed-work
 * capacity fixture; the number of injected ticks is measured, not fixed.
 *
 * This is measurement infrastructure, not a device model or an optimization.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

typedef int ( * setitimer_fn )( __itimer_which_t,
                                const struct itimerval *,
                                struct itimerval * );

static atomic_bool DriverStarted = false;
static atomic_uint_fast64_t DispatchMarkers = 0;
static pthread_once_t PutsOnce = PTHREAD_ONCE_INIT;
static int ( * RealPuts )( const char * );

static void resolve_puts( void )
{
    *( void ** ) ( &RealPuts ) = dlsym( RTLD_NEXT, "puts" );
    if( RealPuts == NULL )
    {
        static const char message[] = "lemix_fixed_tick cannot resolve puts\n";
        const ssize_t written = write(
            STDERR_FILENO, message, sizeof( message ) - 1 );
        ( void ) written;
        _exit( 70 );
    }
}

int puts( const char * value )
{
    static const char marker[] =
        "[JS:Debug] Interrupt_Handler_Task running ";
    int result;

    ( void ) pthread_once( &PutsOnce, resolve_puts );
    result = RealPuts( value );
    if( strcmp( value, marker ) == 0 )
    {
        ( void ) atomic_fetch_add_explicit(
            &DispatchMarkers, 1, memory_order_release );
    }
    return result;
}

static uint64_t monotonic_ns( clockid_t clock_id )
{
    struct timespec value;
    if( clock_gettime( clock_id, &value ) != 0 )
    {
        perror( "lemix_fixed_tick clock_gettime" );
        _exit( 70 );
    }
    return ( uint64_t ) value.tv_sec * UINT64_C( 1000000000 )
           + ( uint64_t ) value.tv_nsec;
}

static uint64_t parse_u64( const char * name, const char * value )
{
    char * end = NULL;
    unsigned long long parsed;
    if( value == NULL || *value == '\0' )
    {
        dprintf( STDERR_FILENO, "lemix_fixed_tick missing %s\n", name );
        _exit( 64 );
    }
    errno = 0;
    parsed = strtoull( value, &end, 0 );
    if( errno != 0 || end == value || *end != '\0' || parsed == 0 )
    {
        dprintf( STDERR_FILENO, "lemix_fixed_tick invalid %s\n", name );
        _exit( 64 );
    }
    return ( uint64_t ) parsed;
}

static void pin_driver_if_requested( void )
{
    const char * raw_cpu = getenv( "FASTDYN_FIXED_TICK_DRIVER_CPU" );
    cpu_set_t affinity;
    uint64_t cpu;
    if( raw_cpu == NULL )
    {
        return;
    }
    cpu = parse_u64( "FASTDYN_FIXED_TICK_DRIVER_CPU", raw_cpu );
    if( cpu >= CPU_SETSIZE )
    {
        dprintf( STDERR_FILENO, "lemix_fixed_tick CPU out of range\n" );
        _exit( 64 );
    }
    CPU_ZERO( &affinity );
    CPU_SET( ( int ) cpu, &affinity );
    if( pthread_setaffinity_np( pthread_self(), sizeof( affinity ), &affinity ) != 0 )
    {
        dprintf( STDERR_FILENO, "lemix_fixed_tick cannot set driver affinity\n" );
        _exit( 70 );
    }
}

static void * drive_ticks( void * ignored )
{
    volatile uint64_t * tick_count;
    const uint64_t requested_units = parse_u64(
        "FASTDYN_FIXED_WORK_UNITS",
        getenv( "FASTDYN_FIXED_WORK_UNITS" ) );
    const uintptr_t address = ( uintptr_t ) parse_u64(
        "FASTDYN_TICK_COUNTER_ADDRESS",
        getenv( "FASTDYN_TICK_COUNTER_ADDRESS" ) );
    /* Let the first marked handler iteration reach vTaskDelay before the
     * first synthetic tick.  This calibration period is outside both the
     * driver CPU clock and the completed-work budget. */
    struct timespec startup = { .tv_sec = 0, .tv_nsec = 100000000 };
    uint64_t start;
    uint64_t observed;
    uint64_t start_markers;
    uint64_t observed_markers;
    uint64_t driver_start;
    uint64_t driver_end;
    ( void ) ignored;

    pin_driver_if_requested();
    tick_count = ( volatile uint64_t * ) address;
    ( void ) nanosleep( &startup, NULL );
    while( atomic_load_explicit( &DispatchMarkers, memory_order_acquire ) == 0 )
    {
        sched_yield();
    }
    start_markers = atomic_load_explicit(
        &DispatchMarkers, memory_order_acquire );
    start = __atomic_load_n( tick_count, __ATOMIC_ACQUIRE );
    observed = start;
    driver_start = monotonic_ns( CLOCK_THREAD_CPUTIME_ID );

    observed_markers = start_markers;
    while( observed_markers - start_markers < requested_units )
    {
        const uint64_t deadline = monotonic_ns( CLOCK_MONOTONIC )
                                  + UINT64_C( 5000000000 );
        if( kill( getpid(), SIGALRM ) != 0 )
        {
            perror( "lemix_fixed_tick kill SIGALRM" );
            _exit( 70 );
        }
        while( __atomic_load_n( tick_count, __ATOMIC_ACQUIRE ) == observed )
        {
            if( monotonic_ns( CLOCK_MONOTONIC ) > deadline )
            {
                dprintf( STDERR_FILENO,
                         "lemix_fixed_tick tick acknowledgement timeout\n" );
                _exit( 70 );
            }
            sched_yield();
        }
        observed = __atomic_load_n( tick_count, __ATOMIC_ACQUIRE );
        observed_markers = atomic_load_explicit(
            &DispatchMarkers, memory_order_acquire );
    }

    observed_markers = atomic_load_explicit(
        &DispatchMarkers, memory_order_acquire );
    driver_end = monotonic_ns( CLOCK_THREAD_CPUTIME_ID );
    dprintf( STDERR_FILENO,
             "FAST_FIXED_WORK start_tick=%" PRIu64 " end_tick=%" PRIu64
             " completed_ticks=%" PRIu64 " start_markers=%" PRIu64
             " end_markers=%" PRIu64 " completed_units=%" PRIu64
             " driver_cpu_ns=%" PRIu64 "\n",
             start, observed, observed - start, start_markers,
             observed_markers, observed_markers - start_markers,
             driver_end - driver_start );
    if( observed_markers - start_markers != requested_units )
    {
        dprintf( STDERR_FILENO, "lemix_fixed_tick work count mismatch\n" );
        _exit( 70 );
    }
    if( kill( getpid(), SIGINT ) != 0 )
    {
        perror( "lemix_fixed_tick kill SIGINT" );
        _exit( 70 );
    }
    for( ;; )
    {
        pause();
    }
}

int setitimer( __itimer_which_t which,
               const struct itimerval * restrict new_value,
               struct itimerval * restrict old_value )
{
    static setitimer_fn real_setitimer;
    struct itimerval disabled;
    pthread_t driver;
    int result;

    if( real_setitimer == NULL )
    {
        *( void ** ) ( &real_setitimer ) = dlsym( RTLD_NEXT, "setitimer" );
        if( real_setitimer == NULL )
        {
            fputs( "lemix_fixed_tick cannot resolve setitimer\n", stderr );
            _exit( 70 );
        }
    }
    if( which != ITIMER_REAL || new_value == NULL
        || new_value->it_interval.tv_sec != 0
        || new_value->it_interval.tv_usec != 1000
        || new_value->it_value.tv_sec != 0
        || new_value->it_value.tv_usec != 1000 )
    {
        return real_setitimer( which, new_value, old_value );
    }
    if( atomic_exchange( &DriverStarted, true ) )
    {
        fputs( "lemix_fixed_tick duplicate timer setup\n", stderr );
        _exit( 70 );
    }
    memset( &disabled, 0, sizeof( disabled ) );
    result = real_setitimer( which, &disabled, old_value );
    if( result != 0 )
    {
        return result;
    }
    result = pthread_create( &driver, NULL, drive_ticks, NULL );
    if( result != 0 )
    {
        dprintf( STDERR_FILENO, "lemix_fixed_tick pthread_create: %s\n",
                 strerror( result ) );
        _exit( 70 );
    }
    ( void ) pthread_detach( driver );
    return 0;
}
