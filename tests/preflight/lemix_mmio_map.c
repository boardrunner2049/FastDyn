/* Measurement-only low-fidelity MMIO backing for a LEMIX x86 application.
 *
 * LEMIX's full pipeline instruments MMIO reads and weakens selected device-
 * dependent conditions.  The public artifact does not ship a runnable
 * instrumented SmartSpeaker binary, so this shim only supplies addressable,
 * zero-initialized storage.  It must not be described as a device model.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>

static void map_region( uintptr_t address, size_t length )
{
    void * result = mmap( ( void * ) address,
                          length,
                          PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE,
                          -1,
                          0 );
    if( result == MAP_FAILED && errno != EEXIST )
    {
        perror( "lemix_mmio_map mmap" );
        abort();
    }
}

__attribute__( ( constructor ) )
static void initialize_mmio_backing( void )
{
    map_region( UINT32_C( 0x08000000 ), UINT32_C( 0x01000000 ) );
    map_region( UINT32_C( 0x10000000 ), UINT32_C( 0x00100000 ) );
    map_region( UINT32_C( 0x20000000 ), UINT32_C( 0x01000000 ) );
    map_region( UINT32_C( 0x40000000 ), UINT32_C( 0x20000000 ) );
    map_region( UINT32_C( 0xe0000000 ), UINT32_C( 0x01000000 ) );
}
