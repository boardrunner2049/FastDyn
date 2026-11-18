/* Callgrind-only SmartSpeaker MMIO backing.
 *
 * Valgrind reserves the guest ranges used by the ordinary
 * MAP_FIXED_NOREPLACE shim, so that shim observes EEXIST although the guest
 * program still has no mapping.  This diagnostic-only variant uses MAP_FIXED
 * inside Valgrind's virtual client address space.  It is a separately hashed
 * environment and is not a device model or correctness workload.
 */

#define _GNU_SOURCE
#include <stdint.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <valgrind/callgrind.h>

static void *dump_and_exit(void *unused)
{
    struct timespec deadline;
    int status;
    (void) unused;
    clock_gettime(CLOCK_MONOTONIC, &deadline);
    deadline.tv_sec += 5;
    do
    {
        status = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME,
                                 &deadline, NULL);
    }
    while (status == EINTR);
    if (status != 0)
        _exit(3);
    CALLGRIND_DUMP_STATS;
    exit(2);
}

static void map_region(uintptr_t address, size_t length)
{
    void *result = mmap((void *)address,
                        length,
                        PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED,
                        -1,
                        0);
    if (result == MAP_FAILED)
    {
        perror("lemix_mmio_map_valgrind mmap");
        abort();
    }
}

__attribute__((constructor))
static void initialize_mmio_backing(void)
{
    pthread_t dumper;
    map_region(UINT32_C(0x08000000), UINT32_C(0x01000000));
    map_region(UINT32_C(0x10000000), UINT32_C(0x00100000));
    map_region(UINT32_C(0x20000000), UINT32_C(0x01000000));
    map_region(UINT32_C(0x40000000), UINT32_C(0x20000000));
    map_region(UINT32_C(0xe0000000), UINT32_C(0x01000000));
    if (pthread_create(&dumper, NULL, dump_and_exit, NULL) != 0)
    {
        perror("lemix_mmio_map_valgrind pthread_create");
        abort();
    }
    pthread_detach(dumper);
}
