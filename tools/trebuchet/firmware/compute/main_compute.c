/*
 * FastDyn compute workload for FreeRTOS on the MPS2 Cortex-M3 demo
 * (tools/trebuchet, see the README there).
 *
 * A packet-processing pipeline typical of a small communications/control
 * firmware.  Input bytes come from a separate RAM bank (INPUT_BASE) that the
 * run harness fills from an input file before QEMU starts, so changing the
 * input does not change the firmware, the TOML or any other FastDyn
 * fingerprint input.
 *
 * Tasks (priorities): rx (2) -> work queue -> proc (3) -> result queue (batches
 * of 8 results) -> agg (4); proc returns packet buffers to rx through the free
 * queue.  A
 * FreeRTOS software timer (timer daemon, priority 5) posts heartbeats to a
 * monitor task (1) that scans the KV store under the KV mutex.  The agg task
 * notifies the monitor every 256 results.
 *
 *   rx    walks the framed byte stream `passes` times: SOF hunt, length/
 *         type/seq header, CRC-16/CCITT check, resync on garbage, sequence
 *         gap detection; copies good frames into pool buffers.
 *   proc  dispatches on the frame type:
 *           0x10 SENSOR  biquad low-pass (Q14) + PI controller with
 *                        anti-windup and saturation, moving average
 *           0x20 KV_SET  sorted singly linked list, node pool, LRU eviction
 *           0x21 KV_DEL  0x22 KV_GET
 *           0x30 TEXT    tokenizer + strict integer parser + 8-register VM
 *                        ("SET R1 10;ADD R1 0x20;PUT 7 R1;GET R2 7;CRC R2")
 *           0x40 BLOB    CRC-32 (table), Adler-32, Fletcher-16, RLE
 *                        encode/decode round trip
 *           0x50 ROUTE   insertion sort, median, dedupe, move-to-front list
 *         malformed payloads (odd lengths, bad tokens, overflow, unknown
 *         type) are counted and skipped.
 *   agg   folds every result into a CRC-32 digest; on the end-of-stream
 *         marker it calls the report functions below and vComputeDone().
 *
 * Everything the report prints is a function of the input bytes only (no
 * tick counts), so it is identical with and without icount.
 *
 * Report points (naked functions made of 16-bit NOPs so that FastDyn
 * `printreg` virtuals can sit on them; the run TOML is generated from the
 * symbol table):
 *   vComputeWindowStart  benchmark_start, then debug_log (runs once)
 *   vComputeReport1..4   printreg r0..r3 each
 *   vComputeDone         benchmark_end (first reached after all input
 *                        frames of all passes have been processed)
 */

#include <stdint.h>
#include <string.h>

#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "semphr.h"
#include "timers.h"

/* ------------------------------------------------------------ input bank */
#define INPUT_BASE          0x21000000UL
#define INPUT_BANK_SIZE     ( 1024UL * 1024UL )
#define INPUT_MAGIC         0x49434446UL       /* "FDCI" little-endian */
#define INPUT_VERSION       1UL
#define INPUT_HDR_SIZE      32UL

typedef struct
{
    uint32_t magic;
    uint32_t version;
    uint32_t stream_len;
    uint32_t passes;
    uint32_t seed;
    uint32_t frames;
    uint32_t stream_crc32;
    uint32_t header_crc32;     /* CRC-32 of bytes 0..27 */
} InputHeader_t;

/* ---------------------------------------------------------------- frames */
#define SOF0                0xA5U
#define SOF1                0x5AU
#define MAX_PAYLOAD         200U
#define POOL_SIZE           8U
#define EOS_INDEX           0xFFU

#define T_SENSOR            0x10U
#define T_KV_SET            0x20U
#define T_KV_DEL            0x21U
#define T_KV_GET            0x22U
#define T_TEXT              0x30U
#define T_BLOB              0x40U
#define T_ROUTE             0x50U

typedef struct
{
    uint8_t type;
    uint8_t seq;
    uint8_t len;
    uint8_t pass;
    uint8_t payload[ MAX_PAYLOAD ];
} Packet_t;

typedef struct
{
    uint8_t type;
    uint8_t seq;
    uint8_t status;            /* 0 ok, 1 malformed payload, 2 unknown type */
    uint8_t eos;
    uint32_t value;
} Result_t;

/* proc hands results to agg in batches (fewer context switches per packet) */
#define RESULT_BATCH        8U
typedef struct
{
    uint32_t n;
    Result_t r[ RESULT_BATCH ];
} ResultBatch_t;

/* ----------------------------------------------------------------- stats */
typedef struct
{
    uint32_t frames_ok;
    uint32_t err_crc;
    uint32_t err_len;
    uint32_t err_seq;
    uint32_t garbage_bytes;
    uint32_t err_type;
    uint32_t err_payload;
    uint32_t err_text;
    uint32_t kv_miss;
    uint32_t kv_evict;
    uint32_t processed;
    uint32_t sensor_digest;
    uint32_t blob_digest;
    uint32_t route_digest;
    uint32_t vm_digest;
    uint32_t digest;
    uint32_t heartbeats;       /* timing dependent: never reported */
    uint32_t monitor_scans;    /* timing dependent: never reported */
    uint32_t monitor_kv_sum;
    uint32_t input_status;
} Stats_t;

static Stats_t xStats;
static Packet_t xPool[ POOL_SIZE ];

static QueueHandle_t xFreeQ, xWorkQ, xResultQ, xMonitorQ;
static SemaphoreHandle_t xKvMutex;
static TaskHandle_t xMonitorTask;
static TimerHandle_t xHeartbeatTimer;

/* --------------------------------------------------------- report points */
void vComputeWindowStart( void ) __attribute__( ( naked, noinline, used ) );
void vComputeReport1( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) __attribute__( ( naked, noinline, used ) );
void vComputeReport2( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) __attribute__( ( naked, noinline, used ) );
void vComputeReport3( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) __attribute__( ( naked, noinline, used ) );
void vComputeReport4( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) __attribute__( ( naked, noinline, used ) );
void vComputeDone( void ) __attribute__( ( naked, noinline, used ) );

void vComputeWindowStart( void ) { __asm volatile ( "nop\n nop\n bx lr\n" ); }
void vComputeReport1( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) { (void)a; (void)b; (void)c; (void)d; __asm volatile ( "nop\n nop\n nop\n nop\n nop\n bx lr\n" ); }
void vComputeReport2( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) { (void)a; (void)b; (void)c; (void)d; __asm volatile ( "nop\n nop\n nop\n nop\n nop\n bx lr\n" ); }
void vComputeReport3( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) { (void)a; (void)b; (void)c; (void)d; __asm volatile ( "nop\n nop\n nop\n nop\n nop\n bx lr\n" ); }
void vComputeReport4( uint32_t a, uint32_t b, uint32_t c, uint32_t d ) { (void)a; (void)b; (void)c; (void)d; __asm volatile ( "nop\n nop\n nop\n nop\n nop\n bx lr\n" ); }
void vComputeDone( void ) { __asm volatile ( "nop\n nop\n 1: b 1b\n" ); }

/* ------------------------------------------------------------- checksums */
static uint32_t ulCrc32Table[ 256 ];

static void prvCrc32Init( void )
{
    for( uint32_t i = 0; i < 256; i++ )
    {
        uint32_t c = i;
        for( int k = 0; k < 8; k++ )
        {
            c = ( c & 1U ) ? ( 0xEDB88320UL ^ ( c >> 1 ) ) : ( c >> 1 );
        }
        ulCrc32Table[ i ] = c;
    }
}

static uint32_t prvCrc32Update( uint32_t crc, const uint8_t * p, uint32_t n )
{
    crc = ~crc;
    while( n-- )
    {
        crc = ulCrc32Table[ ( crc ^ *p++ ) & 0xFFU ] ^ ( crc >> 8 );
    }
    return ~crc;
}

static uint32_t prvCrc32Word( uint32_t crc, uint32_t w )
{
    uint8_t b[ 4 ] = { ( uint8_t ) w, ( uint8_t ) ( w >> 8 ), ( uint8_t ) ( w >> 16 ), ( uint8_t ) ( w >> 24 ) };
    return prvCrc32Update( crc, b, 4 );
}

/* CRC-16/CCITT-FALSE, bitwise (as small MCUs without a table do it). */
static uint16_t prvCrc16Byte( uint16_t crc, uint8_t b )
{
    crc ^= ( uint16_t ) ( ( uint16_t ) b << 8 );
    for( int k = 0; k < 8; k++ )
    {
        crc = ( crc & 0x8000U ) ? ( uint16_t ) ( ( crc << 1 ) ^ 0x1021U ) : ( uint16_t ) ( crc << 1 );
    }
    return crc;
}

static uint32_t prvAdler32( const uint8_t * p, uint32_t n )
{
    uint32_t a = 1, b = 0;
    while( n-- )
    {
        a = ( a + *p++ ) % 65521U;
        b = ( b + a ) % 65521U;
    }
    return ( b << 16 ) | a;
}

static uint16_t prvFletcher16( const uint8_t * p, uint32_t n )
{
    uint16_t s1 = 0, s2 = 0;
    while( n-- )
    {
        s1 = ( uint16_t ) ( ( s1 + *p++ ) % 255U );
        s2 = ( uint16_t ) ( ( s2 + s1 ) % 255U );
    }
    return ( uint16_t ) ( ( s2 << 8 ) | s1 );
}

/* -------------------------------------------------------------- KV store */
#define KV_NODES 48U

typedef struct KvNode
{
    struct KvNode * next;
    uint16_t key;
    uint16_t hits;
    uint32_t value;
    uint32_t stamp;
} KvNode_t;

static KvNode_t xKvNodes[ KV_NODES ];
static KvNode_t * pxKvFree;
static KvNode_t * pxKvHead;
static uint32_t ulKvStamp;
static uint32_t ulKvCount;

static void prvKvInit( void )
{
    pxKvFree = NULL;
    for( uint32_t i = 0; i < KV_NODES; i++ )
    {
        xKvNodes[ i ].next = pxKvFree;
        pxKvFree = &xKvNodes[ i ];
    }
    pxKvHead = NULL;
    ulKvCount = 0;
}

static void prvKvUnlink( KvNode_t * prev, KvNode_t * n )
{
    if( prev ) { prev->next = n->next; }
    else { pxKvHead = n->next; }
    n->next = pxKvFree;
    pxKvFree = n;
    ulKvCount--;
}

/* Evict the least recently stamped node (linear scan, as small firmware does). */
static void prvKvEvict( void )
{
    KvNode_t * prev = NULL, * victim_prev = NULL, * victim = pxKvHead;
    for( KvNode_t * n = pxKvHead; n != NULL; prev = n, n = n->next )
    {
        if( n->stamp < victim->stamp ) { victim = n; victim_prev = prev; }
    }
    if( victim ) { prvKvUnlink( victim_prev, victim ); xStats.kv_evict++; }
}

static void prvKvSet( uint16_t key, uint32_t value )
{
    KvNode_t * prev = NULL, * n = pxKvHead;
    while( n != NULL && n->key < key ) { prev = n; n = n->next; }
    if( n != NULL && n->key == key )
    {
        n->value = value; n->hits++; n->stamp = ++ulKvStamp;
        return;
    }
    if( pxKvFree == NULL )
    {
        prvKvEvict();
        /* the list changed: find the insertion point again */
        prev = NULL; n = pxKvHead;
        while( n != NULL && n->key < key ) { prev = n; n = n->next; }
    }
    KvNode_t * x = pxKvFree;
    pxKvFree = x->next;
    x->key = key; x->value = value; x->hits = 0; x->stamp = ++ulKvStamp;
    x->next = n;
    if( prev ) { prev->next = x; } else { pxKvHead = x; }
    ulKvCount++;
}

static int prvKvGet( uint16_t key, uint32_t * value )
{
    for( KvNode_t * n = pxKvHead; n != NULL && n->key <= key; n = n->next )
    {
        if( n->key == key ) { n->hits++; n->stamp = ++ulKvStamp; *value = n->value; return 1; }
    }
    xStats.kv_miss++;
    return 0;
}

static int prvKvDel( uint16_t key )
{
    KvNode_t * prev = NULL;
    for( KvNode_t * n = pxKvHead; n != NULL && n->key <= key; prev = n, n = n->next )
    {
        if( n->key == key ) { prvKvUnlink( prev, n ); return 1; }
    }
    xStats.kv_miss++;
    return 0;
}

static uint32_t prvKvChecksum( void )
{
    uint32_t c = 0x12345678UL;
    for( KvNode_t * n = pxKvHead; n != NULL; n = n->next )
    {
        c = ( c ^ ( ( uint32_t ) n->key << 16 ) ^ n->value ) * 16777619UL;
    }
    return c ^ ulKvCount;
}

/* Age: halve hit counters, drop never-hit entries older than 64 stamps. */
static void prvKvAge( void )
{
    KvNode_t * prev = NULL, * n = pxKvHead;
    while( n != NULL )
    {
        KvNode_t * next = n->next;
        n->hits >>= 1;
        if( n->hits == 0 && ( ulKvStamp - n->stamp ) > 64U ) { prvKvUnlink( prev, n ); }
        else { prev = n; }
        n = next;
    }
}

/* ------------------------------------------------------- SENSOR (control) */
typedef struct
{
    int32_t x1, x2, y1, y2;    /* biquad state (Q14 domain) */
    int32_t integ;             /* PI integrator */
    int32_t setpoint;
    int32_t avg[ 8 ];
    uint32_t avg_i;
    int32_t out;
} Control_t;

static Control_t xCtl = { .setpoint = 1000 };

/* Butterworth low-pass, fc = 0.1 fs, Q14 coefficients. */
#define BQ_B0  1100
#define BQ_B1  2199
#define BQ_B2  1100
#define BQ_A1  ( -18727 )
#define BQ_A2  6741
#define KP_Q8  180
#define KI_Q8  23
#define INTEG_LIMIT ( 1L << 22 )

static uint32_t prvSensor( const Packet_t * pk, uint8_t * status )
{
    if( ( pk->len & 1U ) || pk->len < 4U ) { *status = 1; return 0; }
    uint32_t h = 2166136261UL;
    int32_t mn = 0x7fffffff, mx = -0x7fffffff;
    for( uint32_t i = 0; i + 1U < pk->len; i += 2U )
    {
        int32_t x = ( int16_t ) ( pk->payload[ i ] | ( pk->payload[ i + 1U ] << 8 ) );
        int32_t y = ( BQ_B0 * x + BQ_B1 * xCtl.x1 + BQ_B2 * xCtl.x2 - BQ_A1 * xCtl.y1 - BQ_A2 * xCtl.y2 ) >> 14;
        xCtl.x2 = xCtl.x1; xCtl.x1 = x;
        xCtl.y2 = xCtl.y1; xCtl.y1 = y;

        int32_t err = xCtl.setpoint - y;
        int32_t integ = xCtl.integ + err;
        if( integ > INTEG_LIMIT ) { integ = INTEG_LIMIT; }
        else if( integ < -INTEG_LIMIT ) { integ = -INTEG_LIMIT; }
        int32_t u = ( KP_Q8 * err + KI_Q8 * ( integ >> 4 ) ) >> 8;
        if( u > 32767 ) { u = 32767; }                       /* saturate; no windup */
        else if( u < -32768 ) { u = -32768; }
        else { xCtl.integ = integ; }
        xCtl.out = u;

        xCtl.avg[ xCtl.avg_i++ & 7U ] = u;
        int32_t s = 0;
        for( int k = 0; k < 8; k++ ) { s += xCtl.avg[ k ]; }
        s >>= 3;
        if( s < mn ) { mn = s; }
        if( s > mx ) { mx = s; }
        h = ( h ^ ( uint32_t ) u ) * 16777619UL;
    }
    /* the packet sequence number nudges the setpoint, like a host command */
    xCtl.setpoint = 1000 + ( int32_t ) ( pk->seq & 0x3FU ) * 8;
    xStats.sensor_digest = prvCrc32Word( xStats.sensor_digest, h ^ ( uint32_t ) ( mx - mn ) );
    *status = 0;
    return h;
}

/* ------------------------------------------------------------- KV packets */
static uint32_t prvKvPacket( const Packet_t * pk, uint8_t * status )
{
    uint32_t r = 0;
    uint32_t stride = ( pk->type == T_KV_SET ) ? 6U : 2U;
    if( pk->len == 0U || ( pk->len % stride ) != 0U ) { *status = 1; return 0; }
    xSemaphoreTake( xKvMutex, portMAX_DELAY );
    for( uint32_t i = 0; i < pk->len; i += stride )
    {
        uint16_t key = ( uint16_t ) ( pk->payload[ i ] | ( pk->payload[ i + 1U ] << 8 ) );
        if( pk->type == T_KV_SET )
        {
            uint32_t v = ( uint32_t ) pk->payload[ i + 2U ] | ( ( uint32_t ) pk->payload[ i + 3U ] << 8 ) |
                         ( ( uint32_t ) pk->payload[ i + 4U ] << 16 ) | ( ( uint32_t ) pk->payload[ i + 5U ] << 24 );
            prvKvSet( key, v );
            r += v;
        }
        else if( pk->type == T_KV_DEL )
        {
            r += ( uint32_t ) prvKvDel( key );
        }
        else
        {
            uint32_t v;
            if( prvKvGet( key, &v ) ) { r = r * 31U + v; }
        }
    }
    r ^= prvKvChecksum();
    xSemaphoreGive( xKvMutex );
    *status = 0;
    return r;
}

/* ------------------------------------------------------------ TEXT (VM) */
static int32_t lVmReg[ 8 ];

static int prvIsSpace( uint8_t c ) { return c == ' ' || c == '\t'; }

/* Next token in [*pp, end); stops at space or ';'.  Returns its length. */
static uint32_t prvToken( const uint8_t ** pp, const uint8_t * end, const uint8_t ** tok )
{
    const uint8_t * p = *pp;
    while( p < end && prvIsSpace( *p ) ) { p++; }
    *tok = p;
    while( p < end && !prvIsSpace( *p ) && *p != ';' ) { p++; }
    *pp = p;
    return ( uint32_t ) ( p - *tok );
}

/* Strict number: decimal or 0x-hex, optional '-', no overflow past 31 bits. */
static int prvParseNum( const uint8_t * t, uint32_t n, int32_t * out )
{
    int neg = 0;
    uint32_t v = 0, base = 10, i = 0;
    if( n == 0U || n > 12U ) { return 0; }
    if( t[ 0 ] == '-' ) { neg = 1; i = 1; }
    if( n - i > 2U && t[ i ] == '0' && ( t[ i + 1U ] == 'x' || t[ i + 1U ] == 'X' ) ) { base = 16; i += 2U; }
    if( i >= n ) { return 0; }
    for( ; i < n; i++ )
    {
        uint8_t c = t[ i ];
        uint32_t d;
        if( c >= '0' && c <= '9' ) { d = c - '0'; }
        else if( base == 16U && c >= 'a' && c <= 'f' ) { d = c - 'a' + 10U; }
        else if( base == 16U && c >= 'A' && c <= 'F' ) { d = c - 'A' + 10U; }
        else { return 0; }
        if( v > ( 0x7FFFFFFFUL - d ) / base ) { return 0; }    /* overflow */
        v = v * base + d;
    }
    *out = neg ? -( int32_t ) v : ( int32_t ) v;
    return 1;
}

static int prvParseReg( const uint8_t * t, uint32_t n, uint32_t * r )
{
    if( n != 2U || ( t[ 0 ] != 'R' && t[ 0 ] != 'r' ) || t[ 1 ] < '0' || t[ 1 ] > '7' ) { return 0; }
    *r = t[ 1 ] - '0';
    return 1;
}

static int prvOperand( const uint8_t * t, uint32_t n, int32_t * v )
{
    uint32_t r;
    if( prvParseReg( t, n, &r ) ) { *v = lVmReg[ r ]; return 1; }
    return prvParseNum( t, n, v );
}

static int prvOpIs( const uint8_t * t, uint32_t n, const char * op )
{
    return n == 3U && t[ 0 ] == ( uint8_t ) op[ 0 ] && t[ 1 ] == ( uint8_t ) op[ 1 ] && t[ 2 ] == ( uint8_t ) op[ 2 ];
}

static uint32_t prvText( const Packet_t * pk, uint8_t * status )
{
    const uint8_t * p = pk->payload, * end = pk->payload + pk->len;
    uint32_t ok = 0, bad = 0;
    while( p < end )
    {
        const uint8_t * op, * a, * b;
        uint32_t nop = prvToken( &p, end, &op );
        uint32_t na = prvToken( &p, end, &a );
        uint32_t nb = prvToken( &p, end, &b );
        const uint8_t * extra;
        uint32_t nx = prvToken( &p, end, &extra );
        int good = 0;
        uint32_t r;
        int32_t v;
        if( nx != 0U ) { good = 0; }
        else if( prvOpIs( op, nop, "SET" ) && prvParseReg( a, na, &r ) && prvParseNum( b, nb, &v ) ) { lVmReg[ r ] = v; good = 1; }
        else if( prvOpIs( op, nop, "ADD" ) && prvParseReg( a, na, &r ) && prvOperand( b, nb, &v ) ) { lVmReg[ r ] += v; good = 1; }
        else if( prvOpIs( op, nop, "MUL" ) && prvParseReg( a, na, &r ) && prvOperand( b, nb, &v ) ) { lVmReg[ r ] *= v; good = 1; }
        else if( prvOpIs( op, nop, "XOR" ) && prvParseReg( a, na, &r ) && prvOperand( b, nb, &v ) ) { lVmReg[ r ] ^= v; good = 1; }
        else if( prvOpIs( op, nop, "SHR" ) && prvParseReg( a, na, &r ) && prvParseNum( b, nb, &v ) && v >= 0 && v < 32 ) { lVmReg[ r ] = ( int32_t ) ( ( uint32_t ) lVmReg[ r ] >> v ); good = 1; }
        else if( prvOpIs( op, nop, "PUT" ) && prvParseNum( a, na, &v ) && v >= 0 && v < 65536 && prvParseReg( b, nb, &r ) )
        {
            xSemaphoreTake( xKvMutex, portMAX_DELAY );
            prvKvSet( ( uint16_t ) v, ( uint32_t ) lVmReg[ r ] );
            xSemaphoreGive( xKvMutex );
            good = 1;
        }
        else if( prvOpIs( op, nop, "GET" ) && prvParseReg( a, na, &r ) && prvParseNum( b, nb, &v ) && v >= 0 && v < 65536 )
        {
            uint32_t kv = 0;
            xSemaphoreTake( xKvMutex, portMAX_DELAY );
            good = prvKvGet( ( uint16_t ) v, &kv );
            xSemaphoreGive( xKvMutex );
            lVmReg[ r ] = ( int32_t ) kv;
            good = 1;           /* a miss is counted by the KV store, not a syntax error */
        }
        else if( prvOpIs( op, nop, "CRC" ) && prvParseReg( a, na, &r ) && nb == 0U ) { lVmReg[ r ] = ( int32_t ) prvCrc32Word( 0, ( uint32_t ) lVmReg[ r ] ); good = 1; }
        if( good ) { ok++; } else { bad++; xStats.err_text++; }
        /* skip to the end of the statement */
        while( p < end && *p != ';' ) { p++; }
        if( p < end ) { p++; }
    }
    uint32_t h = 0;
    for( int i = 0; i < 8; i++ ) { h = h * 33U + ( uint32_t ) lVmReg[ i ]; }
    xStats.vm_digest = prvCrc32Word( xStats.vm_digest, h );
    *status = ( bad != 0U && ok == 0U ) ? 1 : 0;
    return h ^ ( ok << 8 ) ^ bad;
}

/* ------------------------------------------------------------ BLOB codec */
static uint8_t ucRle[ 2U * MAX_PAYLOAD + 2U ];
static uint8_t ucDec[ MAX_PAYLOAD ];

static uint32_t prvRleEncode( const uint8_t * p, uint32_t n, uint8_t * out )
{
    uint32_t o = 0, i = 0;
    while( i < n )
    {
        uint8_t c = p[ i ];
        uint32_t run = 1;
        while( i + run < n && p[ i + run ] == c && run < 255U ) { run++; }
        out[ o++ ] = ( uint8_t ) run;
        out[ o++ ] = c;
        i += run;
    }
    return o;
}

static uint32_t prvRleDecode( const uint8_t * p, uint32_t n, uint8_t * out, uint32_t cap )
{
    uint32_t o = 0;
    for( uint32_t i = 0; i + 1U < n; i += 2U )
    {
        for( uint32_t k = 0; k < p[ i ]; k++ )
        {
            if( o >= cap ) { return 0xFFFFFFFFUL; }
            out[ o++ ] = p[ i + 1U ];
        }
    }
    return o;
}

static uint32_t prvBlob( const Packet_t * pk, uint8_t * status )
{
    if( pk->len == 0U ) { *status = 1; return 0; }
    uint32_t c = prvCrc32Update( 0, pk->payload, pk->len );
    uint32_t a = prvAdler32( pk->payload, pk->len );
    uint16_t f = prvFletcher16( pk->payload, pk->len );
    uint32_t en = prvRleEncode( pk->payload, pk->len, ucRle );
    uint32_t dn = prvRleDecode( ucRle, en, ucDec, sizeof( ucDec ) );
    int rt = ( dn == pk->len ) && ( memcmp( ucDec, pk->payload, pk->len ) == 0 );
    uint32_t r = c ^ ( a * 3U ) ^ ( ( uint32_t ) f << 7 ) ^ ( en << 20 ) ^ ( uint32_t ) rt;
    xStats.blob_digest = prvCrc32Word( xStats.blob_digest, r );
    *status = rt ? 0 : 1;
    return r;
}

/* ----------------------------------------------------------- ROUTE table */
#define ROUTE_SLOTS 16U
typedef struct Route { struct Route * next; uint8_t node; uint8_t uses; } Route_t;
static Route_t xRoutes[ ROUTE_SLOTS ];
static Route_t * pxRouteHead;

static void prvRouteInit( void )
{
    pxRouteHead = NULL;
    for( uint32_t i = 0; i < ROUTE_SLOTS; i++ )
    {
        xRoutes[ i ].node = ( uint8_t ) ( i * 13U );
        xRoutes[ i ].uses = 0;
        xRoutes[ i ].next = pxRouteHead;
        pxRouteHead = &xRoutes[ i ];
    }
}

/* Move-to-front lookup; a miss replaces the tail entry. */
static uint32_t prvRouteTouch( uint8_t node )
{
    Route_t * prev = NULL, * n = pxRouteHead, * tail_prev = NULL;
    uint32_t depth = 0;
    while( n != NULL && n->node != node )
    {
        if( n->next == NULL ) { tail_prev = prev; break; }
        prev = n; n = n->next; depth++;
    }
    if( n == NULL || n->node != node )
    {
        n = ( tail_prev ? tail_prev->next : pxRouteHead );
        prev = tail_prev;
        n->node = node; n->uses = 0;
        depth = ROUTE_SLOTS;
    }
    n->uses++;
    if( prev != NULL ) { prev->next = n->next; n->next = pxRouteHead; pxRouteHead = n; }
    return depth;
}

static uint32_t prvRoute( const Packet_t * pk, uint8_t * status )
{
    uint8_t a[ MAX_PAYLOAD ];
    uint32_t n = pk->len;
    if( n < 3U ) { *status = 1; return 0; }
    memcpy( a, pk->payload, n );
    for( uint32_t i = 1; i < n; i++ )                     /* insertion sort */
    {
        uint8_t x = a[ i ];
        uint32_t j = i;
        while( j > 0U && a[ j - 1U ] > x ) { a[ j ] = a[ j - 1U ]; j--; }
        a[ j ] = x;
    }
    uint32_t median = a[ n / 2U ];
    uint32_t uniq = 0, depth = 0;
    for( uint32_t i = 0; i < n; i++ )
    {
        if( i == 0U || a[ i ] != a[ i - 1U ] ) { uniq++; depth += prvRouteTouch( a[ i ] ); }
    }
    uint32_t r = ( median << 24 ) ^ ( uniq << 12 ) ^ depth;
    xStats.route_digest = prvCrc32Word( xStats.route_digest, r );
    *status = 0;
    return r;
}

/* ----------------------------------------------------------------- tasks */
static const InputHeader_t * prvInput( void ) { return ( const InputHeader_t * ) INPUT_BASE; }

static uint32_t prvCheckInput( void )
{
    const InputHeader_t * h = prvInput();
    if( h->magic != INPUT_MAGIC ) { return 1; }
    if( h->version != INPUT_VERSION ) { return 2; }
    if( prvCrc32Update( 0, ( const uint8_t * ) h, 28 ) != h->header_crc32 ) { return 3; }
    if( h->stream_len == 0U || h->stream_len > INPUT_BANK_SIZE - INPUT_HDR_SIZE ) { return 4; }
    if( h->passes == 0U || h->passes > 1000U ) { return 5; }
    if( prvCrc32Update( 0, ( const uint8_t * ) INPUT_BASE + INPUT_HDR_SIZE, h->stream_len ) != h->stream_crc32 ) { return 6; }
    return 0;
}

static void prvEmit( Packet_t * src_hdr, const uint8_t * payload, uint8_t pass )
{
    uint8_t idx;
    xQueueReceive( xFreeQ, &idx, portMAX_DELAY );
    Packet_t * pk = &xPool[ idx ];
    pk->type = src_hdr->type;
    pk->seq = src_hdr->seq;
    pk->len = src_hdr->len;
    pk->pass = pass;
    memcpy( pk->payload, payload, src_hdr->len );
    xQueueSend( xWorkQ, &idx, portMAX_DELAY );
}

enum { S_SOF0, S_SOF1, S_LEN, S_TYPE, S_SEQ, S_PAYLOAD, S_CRC0, S_CRC1 };

static void prvRxTask( void * pv )
{
    ( void ) pv;
    uint32_t st = prvCheckInput();
    xStats.input_status = st;
    vComputeWindowStart();
    if( st == 0U )
    {
        const InputHeader_t * h = prvInput();
        const uint8_t * s = ( const uint8_t * ) INPUT_BASE + INPUT_HDR_SIZE;
        uint8_t payload[ MAX_PAYLOAD ];
        for( uint32_t pass = 0; pass < h->passes; pass++ )
        {
            int state = S_SOF0;
            Packet_t hdr = { 0 };
            uint32_t got = 0;
            uint16_t crc = 0xFFFF, rx_crc = 0;
            int have_seq = 0;
            uint8_t last_seq = 0;
            for( uint32_t i = 0; i < h->stream_len; i++ )
            {
                uint8_t b = s[ i ];
                switch( state )
                {
                    case S_SOF0:
                        if( b == SOF0 ) { state = S_SOF1; } else { xStats.garbage_bytes++; }
                        break;
                    case S_SOF1:
                        if( b == SOF1 ) { state = S_LEN; crc = 0xFFFF; }
                        else if( b != SOF0 ) { state = S_SOF0; xStats.garbage_bytes += 2U; }
                        else { xStats.garbage_bytes++; }
                        break;
                    case S_LEN:
                        if( b > MAX_PAYLOAD ) { xStats.err_len++; state = S_SOF0; break; }
                        hdr.len = b; crc = prvCrc16Byte( crc, b ); state = S_TYPE;
                        break;
                    case S_TYPE:
                        hdr.type = b; crc = prvCrc16Byte( crc, b ); state = S_SEQ;
                        break;
                    case S_SEQ:
                        hdr.seq = b; crc = prvCrc16Byte( crc, b ); got = 0;
                        state = ( hdr.len != 0U ) ? S_PAYLOAD : S_CRC0;
                        break;
                    case S_PAYLOAD:
                        payload[ got++ ] = b; crc = prvCrc16Byte( crc, b );
                        if( got == hdr.len ) { state = S_CRC0; }
                        break;
                    case S_CRC0:
                        rx_crc = b; state = S_CRC1;
                        break;
                    default: /* S_CRC1 */
                        rx_crc |= ( uint16_t ) ( b << 8 );
                        state = S_SOF0;
                        if( rx_crc != crc ) { xStats.err_crc++; break; }
                        if( have_seq && hdr.seq != ( uint8_t ) ( last_seq + 1U ) ) { xStats.err_seq++; }
                        have_seq = 1; last_seq = hdr.seq;
                        xStats.frames_ok++;
                        prvEmit( &hdr, payload, ( uint8_t ) pass );
                        break;
                }
            }
            if( state != S_SOF0 ) { xStats.err_len++; }     /* truncated frame at end of stream */
        }
    }
    uint8_t eos = EOS_INDEX;
    xQueueSend( xWorkQ, &eos, portMAX_DELAY );
    vTaskSuspend( NULL );
}

static void prvProcTask( void * pv )
{
    ( void ) pv;
    uint32_t since_age = 0;
    static ResultBatch_t xBatch;
    for( ; ; )
    {
        uint8_t idx;
        Result_t r = { 0 };
        xQueueReceive( xWorkQ, &idx, portMAX_DELAY );
        if( idx == EOS_INDEX )
        {
            r.eos = 1;
            xBatch.r[ xBatch.n++ ] = r;
            xQueueSend( xResultQ, &xBatch, portMAX_DELAY );
            xBatch.n = 0;
            continue;
        }
        const Packet_t * pk = &xPool[ idx ];
        r.type = pk->type;
        r.seq = pk->seq;
        switch( pk->type )
        {
            case T_SENSOR: r.value = prvSensor( pk, &r.status ); break;
            case T_KV_SET:
            case T_KV_DEL:
            case T_KV_GET: r.value = prvKvPacket( pk, &r.status ); break;
            case T_TEXT:   r.value = prvText( pk, &r.status ); break;
            case T_BLOB:   r.value = prvBlob( pk, &r.status ); break;
            case T_ROUTE:  r.value = prvRoute( pk, &r.status ); break;
            default:       r.status = 2; xStats.err_type++; break;
        }
        if( r.status == 1U ) { xStats.err_payload++; }
        if( ++since_age == 32U )
        {
            since_age = 0;
            xSemaphoreTake( xKvMutex, portMAX_DELAY );
            prvKvAge();
            xSemaphoreGive( xKvMutex );
        }
        xQueueSend( xFreeQ, &idx, portMAX_DELAY );
        xBatch.r[ xBatch.n++ ] = r;
        if( xBatch.n == RESULT_BATCH )
        {
            xQueueSend( xResultQ, &xBatch, portMAX_DELAY );
            xBatch.n = 0;
        }
    }
}

static void prvAggTask( void * pv )
{
    ( void ) pv;
    static ResultBatch_t xBatch;
    int eos = 0;
    while( !eos )
    {
        xQueueReceive( xResultQ, &xBatch, portMAX_DELAY );
        for( uint32_t i = 0; i < xBatch.n; i++ )
        {
            const Result_t * r = &xBatch.r[ i ];
            if( r->eos )
            {
                eos = 1;
                break;
            }
            xStats.processed++;
            xStats.digest = prvCrc32Update( xStats.digest, ( const uint8_t * ) r, sizeof( *r ) );
            if( ( xStats.processed & 0xFFU ) == 0U )
            {
                xTaskNotifyGive( xMonitorTask );
            }
        }
    }
    xTimerStop( xHeartbeatTimer, 0 );
    uint32_t kv;
    xSemaphoreTake( xKvMutex, portMAX_DELAY );
    kv = prvKvChecksum();
    xSemaphoreGive( xKvMutex );
    vComputeReport1( xStats.digest, xStats.frames_ok, xStats.processed, xStats.input_status );
    vComputeReport2( xStats.err_crc, xStats.err_len, xStats.err_seq, xStats.garbage_bytes );
    vComputeReport3( xStats.err_type, xStats.err_payload, xStats.err_text, xStats.kv_miss );
    vComputeReport4( xStats.sensor_digest ^ xStats.blob_digest, xStats.route_digest, xStats.vm_digest, kv ^ xStats.kv_evict );
    vComputeDone();
    for( ; ; ) { }
}

static void prvMonitorTask( void * pv )
{
    ( void ) pv;
    for( ; ; )
    {
        uint8_t hb;
        /* heartbeats from the timer, or a notification every 256 results */
        if( xQueueReceive( xMonitorQ, &hb, pdMS_TO_TICKS( 5 ) ) == pdPASS )
        {
            xStats.heartbeats++;
        }
        if( ulTaskNotifyTake( pdTRUE, 0 ) != 0U )
        {
            xSemaphoreTake( xKvMutex, portMAX_DELAY );
            xStats.monitor_kv_sum += prvKvChecksum();
            xSemaphoreGive( xKvMutex );
            xStats.monitor_scans++;
        }
    }
}

static void prvHeartbeat( TimerHandle_t t )
{
    ( void ) t;
    uint8_t hb = 1;
    xQueueSend( xMonitorQ, &hb, 0 );
}

void main_compute( void )
{
    prvCrc32Init();
    prvKvInit();
    prvRouteInit();
    xFreeQ = xQueueCreate( POOL_SIZE, sizeof( uint8_t ) );
    xWorkQ = xQueueCreate( POOL_SIZE + 1U, sizeof( uint8_t ) );
    xResultQ = xQueueCreate( 2, sizeof( ResultBatch_t ) );
    xMonitorQ = xQueueCreate( 4, sizeof( uint8_t ) );
    xKvMutex = xSemaphoreCreateMutex();
    for( uint8_t i = 0; i < POOL_SIZE; i++ )
    {
        xQueueSend( xFreeQ, &i, 0 );
    }
    xTaskCreate( prvMonitorTask, "mon", configMINIMAL_STACK_SIZE * 2, NULL, tskIDLE_PRIORITY + 1, &xMonitorTask );
    xTaskCreate( prvRxTask, "rx", configMINIMAL_STACK_SIZE * 4, NULL, tskIDLE_PRIORITY + 2, NULL );
    xTaskCreate( prvProcTask, "proc", configMINIMAL_STACK_SIZE * 4, NULL, tskIDLE_PRIORITY + 3, NULL );
    xTaskCreate( prvAggTask, "agg", configMINIMAL_STACK_SIZE * 2, NULL, tskIDLE_PRIORITY + 4, NULL );
    xHeartbeatTimer = xTimerCreate( "hb", pdMS_TO_TICKS( 2 ), pdTRUE, NULL, prvHeartbeat );
    xTimerStart( xHeartbeatTimer, 0 );
    vTaskStartScheduler();
    for( ; ; ) { }
}
