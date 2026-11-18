/* Diagnostic-only FreeRTOS POSIX tick accelerator for the LEMIX fixture.
 *
 * The unchanged SmartSpeaker binary requests a 1,000 us periodic ITIMER_REAL
 * tick.  This interposer changes only that exact request to 10 us.  It creates
 * a separately identified event schedule and must not be treated as equivalent
 * to the original host-timed execution or to MCU timing.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>

typedef int (*setitimer_fn)(__itimer_which_t, const struct itimerval *,
                           struct itimerval *);

int setitimer(__itimer_which_t which,
              const struct itimerval *restrict new_value,
              struct itimerval *restrict old_value)
{
    static setitimer_fn real_setitimer;
    struct itimerval accelerated;

    if (real_setitimer == NULL)
    {
        *(void **)(&real_setitimer) = dlsym(RTLD_NEXT, "setitimer");
        if (real_setitimer == NULL)
        {
            fputs("lemix_fast_tick: cannot resolve setitimer\n", stderr);
            abort();
        }
    }
    if (which == ITIMER_REAL && new_value != NULL
        && new_value->it_interval.tv_sec == 0
        && new_value->it_interval.tv_usec == 1000
        && new_value->it_value.tv_sec == 0
        && new_value->it_value.tv_usec == 1000)
    {
        accelerated = *new_value;
        accelerated.it_interval.tv_usec = 10;
        accelerated.it_value.tv_usec = 10;
        return real_setitimer(which, &accelerated, old_value);
    }
    return real_setitimer(which, new_value, old_value);
}
