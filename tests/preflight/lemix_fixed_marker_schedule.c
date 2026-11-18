/* Exact-work SmartSpeaker tick schedule for unchanged LEMIX machine code.
 *
 * The application emits one Interrupt_Handler_Task marker and then blocks for
 * exactly ten FreeRTOS ticks.  This interposer disables the wall-clock timer,
 * waits for the marked task to settle into its delay, injects exactly ten
 * acknowledged SIGALRM ticks, and requires exactly one new marker.  Every
 * measured execution therefore receives the same ten-ticks-per-completed-unit
 * schedule.  The environment is a bounded POSIX replay, not an MCU device model.
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

static uint64_t monotonic_ns( clockid_t clock_id )
{
    struct timespec value;
    if( clock_gettime( clock_id, &value ) != 0 )
    {
        perror( "lemix_marker_schedule clock_gettime" );
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
        dprintf( STDERR_FILENO, "lemix_marker_schedule missing %s\n", name );
        _exit( 64 );
    }
    errno = 0;
    parsed = strtoull( value, &end, 0 );
    if( errno != 0 || end == value || *end != '\0' || parsed == 0 )
    {
        dprintf( STDERR_FILENO, "lemix_marker_schedule invalid %s\n", name );
        _exit( 64 );
    }
    return ( uint64_t ) parsed;
}

static void resolve_puts( void )
{
    *( void ** ) ( &RealPuts ) = dlsym( RTLD_NEXT, "puts" );
    if( RealPuts == NULL )
    {
        static const char message[] =
            "lemix_marker_schedule cannot resolve puts\n";
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

static void wait_until( bool ( * predicate )( uint64_t, uint64_t ),
                        uint64_t first,
                        uint64_t second,
                        const char * operation )
{
    const uint64_t deadline = monotonic_ns( CLOCK_MONOTONIC )
                              + UINT64_C( 5000000000 );
    while( !predicate( first, second ) )
    {
        if( monotonic_ns( CLOCK_MONOTONIC ) > deadline )
        {
            dprintf( STDERR_FILENO,
                     "lemix_marker_schedule %s timeout\n", operation );
            _exit( 70 );
        }
        sched_yield();
    }
}

static volatile uint64_t * TickCount;

static bool tick_changed( uint64_t old_value, uint64_t ignored )
{
    ( void ) ignored;
    return __atomic_load_n( TickCount, __ATOMIC_ACQUIRE ) != old_value;
}

static bool marker_reached( uint64_t expected, uint64_t ignored )
{
    ( void ) ignored;
    return atomic_load_explicit( &DispatchMarkers, memory_order_acquire )
           >= expected;
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
        _exit( 64 );
    }
    CPU_ZERO( &affinity );
    CPU_SET( ( int ) cpu, &affinity );
    if( pthread_setaffinity_np( pthread_self(), sizeof( affinity ), &affinity ) != 0 )
    {
        _exit( 70 );
    }
}

static void * drive_ticks( void * ignored )
{
    const uint64_t requested_units = parse_u64(
        "FASTDYN_FIXED_WORK_UNITS", getenv( "FASTDYN_FIXED_WORK_UNITS" ) );
    const uintptr_t address = ( uintptr_t ) parse_u64(
        "FASTDYN_TICK_COUNTER_ADDRESS",
        getenv( "FASTDYN_TICK_COUNTER_ADDRESS" ) );
    const char * raw_settle = getenv( "FASTDYN_MARKER_SETTLE_NS" );
    const uint64_t settle_ns = raw_settle == NULL
                               ? UINT64_C( 1000000 )
                               : parse_u64( "FASTDYN_MARKER_SETTLE_NS", raw_settle );
    struct timespec startup = { .tv_sec = 0, .tv_nsec = 100000000 };
    struct timespec settle = {
        .tv_sec = ( time_t ) ( settle_ns / UINT64_C( 1000000000 ) ),
        .tv_nsec = ( long ) ( settle_ns % UINT64_C( 1000000000 ) ),
    };
    uint64_t start_tick;
    uint64_t tick;
    uint64_t start_markers;
    uint64_t driver_start;
    uint64_t driver_end;
    uint64_t unit;
    ( void ) ignored;

    pin_driver_if_requested();
    TickCount = ( volatile uint64_t * ) address;
    ( void ) nanosleep( &startup, NULL );
    wait_until( marker_reached, 1, 0, "initial marker" );
    start_markers = atomic_load_explicit(
        &DispatchMarkers, memory_order_acquire );
    start_tick = __atomic_load_n( TickCount, __ATOMIC_ACQUIRE );
    tick = start_tick;
    driver_start = monotonic_ns( CLOCK_THREAD_CPUTIME_ID );

    for( unit = 1; unit <= requested_units; ++unit )
    {
        unsigned index;
        ( void ) nanosleep( &settle, NULL );
        for( index = 0; index < 10; ++index )
        {
            const uint64_t before = tick;
            if( kill( getpid(), SIGALRM ) != 0 )
            {
                _exit( 70 );
            }
            wait_until( tick_changed, before, 0, "tick acknowledgement" );
            tick = __atomic_load_n( TickCount, __ATOMIC_ACQUIRE );
            if( tick != before + 1 )
            {
                dprintf( STDERR_FILENO,
                         "lemix_marker_schedule nonunit tick delta\n" );
                _exit( 70 );
            }
        }
        wait_until( marker_reached, start_markers + unit, 0,
                    "completed marker" );
        if( atomic_load_explicit( &DispatchMarkers, memory_order_acquire )
            != start_markers + unit )
        {
            dprintf( STDERR_FILENO,
                     "lemix_marker_schedule marker multiplicity mismatch\n" );
            _exit( 70 );
        }
    }

    driver_end = monotonic_ns( CLOCK_THREAD_CPUTIME_ID );
    dprintf( STDERR_FILENO,
             "FAST_FIXED_WORK start_tick=%" PRIu64 " end_tick=%" PRIu64
             " completed_ticks=%" PRIu64 " start_markers=%" PRIu64
             " end_markers=%" PRIu64 " completed_units=%" PRIu64
             " driver_cpu_ns=%" PRIu64 "\n",
             start_tick, tick, tick - start_tick, start_markers,
             start_markers + requested_units, requested_units,
             driver_end - driver_start );
    if( tick - start_tick != requested_units * UINT64_C( 10 ) )
    {
        _exit( 70 );
    }
    if( kill( getpid(), SIGINT ) != 0 )
    {
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
        _exit( 70 );
    }
    ( void ) pthread_detach( driver );
    return 0;
}
