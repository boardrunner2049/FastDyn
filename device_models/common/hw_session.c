/*
 * hw_session: process-singleton wrapper around a libhw probe.
 *
 * See hw_session.h for the public contract.
 *
 *   - poll thread: poll hw_board_halted -> read R0 -> raise IRQ
 *   - serve:      write R1, resume, clear pending, signal cv
 *   - read/write: 100x retry at 1ms while board halted
 *
 * State that used to live as file-static globals in passthrough.c now
 * lives inside `struct hw_session`. Multiple device-model backends
 * targeting the same backend name share the same session, so there is
 * exactly one libusb claim, one mutex, one condvar, one poll thread.
 */

#include "hw_session.h"

#include <hw.h>
#include <utils.h>
#include <device.h>
#include <qemu/qemu-plugin.h>

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define FD_LOG_SUBSYSTEM "hw_session"
#include <fastdyn/log.h>

#define HW_SESSION_MAX_BACKENDS  4
#define HW_SESSION_NAME_MAX      32

#define HW_RETRY_MAX_ATTEMPTS    100
#define HW_RETRY_SLEEP_US        1000     /* 1ms */
#define POLL_IDLE_SLEEP_US       40000    /* 40ms - matches passthrough */
#define POLL_SPURIOUS_SLEEP_US   100000   /* 100ms - matches passthrough */
#define IRQ_FIRING_LINE_MIN      1
#define IRQ_FIRING_LINE_MAX      240
#define IRQ_QEMU_VECTOR_BASE     16       /* IRQ N -> QEMU exception N+16 */
#define R0                       0
#define R1                       1
#define DEV_DEBUG_REG_DUMP_COUNT 16

struct hw_session {
    char            backend_name[HW_SESSION_NAME_MAX];
    hw_t           *hw;
    pthread_mutex_t hw_mutex;
    pthread_cond_t  irq_cv;
    int             irq_pending;     /* board-side firing line; 0 = none */
    pthread_t       poll_thread;
    int             refcount;
    int             in_use;          /* slot occupancy flag */
};

static struct hw_session g_sessions[HW_SESSION_MAX_BACKENDS];
static pthread_mutex_t   g_registry_mutex = PTHREAD_MUTEX_INITIALIZER;

/* ------------------------------------------------------------------ */
/* Poll thread                                                         */
/* ------------------------------------------------------------------ */

static void *hw_session_poll_thread(void *arg)
{
    struct hw_session *s = (struct hw_session *)arg;

    for (;;) {
        pthread_mutex_lock(&s->hw_mutex);

        /* Wait until the previous IRQ has been served and cleared. */
        while (s->irq_pending) {
            pthread_cond_wait(&s->irq_cv, &s->hw_mutex);
        }

        if (hw_board_halted(s->hw)) {
            for (int i = 0; i < DEV_DEBUG_REG_DUMP_COUNT; i++) {
                FD_LOG_DEBUG("register %d: 0x%lx", i, hw_read_reg(s->hw, i));
            }

            int firing_line = (int)hw_read_reg(s->hw, R0);

            if (firing_line < IRQ_FIRING_LINE_MIN ||
                firing_line > IRQ_FIRING_LINE_MAX) {
                /*
                 * Spurious halt (e.g. debugger halt-on-connect, not a
                 * monitor BKPT). Resume the board so the monitor can
                 * start running, then back off briefly.
                 */
                FD_LOG_WARN("%s: spurious halt (r0=%d), resuming",
                            s->backend_name, firing_line);
                hw_board_run(s->hw);
                pthread_mutex_unlock(&s->hw_mutex);
                usleep(POLL_SPURIOUS_SLEEP_US);
                continue;
            }

            s->irq_pending = firing_line;

            FD_LOG_DEBUG("register %d: 0x%lx", R0, hw_read_reg(s->hw, R0));
            FD_LOG_DEBUG("register %d: 0x%lx", 15, hw_read_reg(s->hw, 15));
            pthread_mutex_unlock(&s->hw_mutex);

            qemu_plugin_raise_irq(firing_line + IRQ_QEMU_VECTOR_BASE, false);
        } else {
            pthread_mutex_unlock(&s->hw_mutex);
            usleep(POLL_IDLE_SLEEP_US);
        }
    }

    return NULL;
}

/* ------------------------------------------------------------------ */
/* Registry helpers (require g_registry_mutex held)                    */
/* ------------------------------------------------------------------ */

static struct hw_session *registry_find_locked(const char *backend)
{
    for (int i = 0; i < HW_SESSION_MAX_BACKENDS; i++) {
        if (g_sessions[i].in_use &&
            strncmp(g_sessions[i].backend_name, backend,
                    HW_SESSION_NAME_MAX) == 0) {
            return &g_sessions[i];
        }
    }
    return NULL;
}

static struct hw_session *registry_alloc_locked(const char *backend)
{
    for (int i = 0; i < HW_SESSION_MAX_BACKENDS; i++) {
        if (!g_sessions[i].in_use) {
            struct hw_session *s = &g_sessions[i];
            memset(s, 0, sizeof(*s));
            strncpy(s->backend_name, backend, HW_SESSION_NAME_MAX - 1);
            pthread_mutex_init(&s->hw_mutex, NULL);
            pthread_cond_init(&s->irq_cv, NULL);
            s->in_use = 1;
            return s;
        }
    }
    return NULL;
}

static void registry_free_locked(struct hw_session *s)
{
    pthread_cond_destroy(&s->irq_cv);
    pthread_mutex_destroy(&s->hw_mutex);
    memset(s, 0, sizeof(*s));
}

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */

hw_session_t *hw_session_acquire(const char *backend)
{
    if (!backend || !backend[0]) {
        FD_LOG_ERROR("acquire: NULL/empty backend name");
        return NULL;
    }

    pthread_mutex_lock(&g_registry_mutex);

    struct hw_session *s = registry_find_locked(backend);
    if (s) {
        s->refcount++;
        pthread_mutex_unlock(&g_registry_mutex);
        return s;
    }

    s = registry_alloc_locked(backend);
    if (!s) {
        FD_LOG_ERROR("acquire: registry full (max %d backends)",
                     HW_SESSION_MAX_BACKENDS);
        pthread_mutex_unlock(&g_registry_mutex);
        return NULL;
    }

    s->hw = hw_connect(backend, NULL, 0);
    if (!s->hw) {
        FD_LOG_ERROR("acquire: hw_connect(%s) failed", backend);
        registry_free_locked(s);
        pthread_mutex_unlock(&g_registry_mutex);
        return NULL;
    }

    if (pthread_create(&s->poll_thread, NULL,
                       hw_session_poll_thread, s) != 0) {
        FD_LOG_ERROR("acquire: pthread_create failed: %s",
                     strerror(errno));
        hw_close(s->hw);
        registry_free_locked(s);
        pthread_mutex_unlock(&g_registry_mutex);
        return NULL;
    }

    s->refcount = 1;
    pthread_mutex_unlock(&g_registry_mutex);
    return s;
}

int hw_session_read(hw_session_t *s, hwaddr addr, unsigned size,
                    uint64_t *out, uint64_t pc)
{
    if (!s || !s->hw || !out) return -1;

    for (int attempt = 0; ; attempt++) {
        uint32_t value_read = 0;
        int status;
        int halted;

        pthread_mutex_lock(&s->hw_mutex);
        if (size == 1) {
            uint8_t byte_val = 0;
            status = hw_read8(s->hw, addr, &byte_val);
            value_read = byte_val;
        } else {
            status = hw_read32(s->hw, addr, &value_read);
        }
        halted = hw_board_halted(s->hw);
        pthread_mutex_unlock(&s->hw_mutex);

        if (status == 0) {
            *out = value_read;
            return 0;
        }

        if (halted && attempt < HW_RETRY_MAX_ATTEMPTS) {
            usleep(HW_RETRY_SLEEP_US);
            continue;
        }

        FD_LOG_ERROR("%s: HW read FAILED: addr=0x%08lx size=%u "
                     "pc=0x%08lx irq_pending=%d halted=%d attempts=%d",
                s->backend_name,
                (unsigned long)addr, size, (unsigned long)pc,
                s->irq_pending, halted, attempt);
        return -1;
    }
}

int hw_session_write(hw_session_t *s, hwaddr addr, uint64_t value,
                     unsigned size, uint64_t pc)
{
    if (!s || !s->hw) return -1;

    for (int attempt = 0; ; attempt++) {
        int status;
        int halted;

        pthread_mutex_lock(&s->hw_mutex);
        if (size == 1) {
            status = hw_write8(s->hw, addr, (uint32_t)value);
        } else {
            status = hw_write32(s->hw, addr, (uint32_t)value);
        }
        halted = hw_board_halted(s->hw);
        pthread_mutex_unlock(&s->hw_mutex);

        if (status == 0) return 0;

        if (halted && attempt < HW_RETRY_MAX_ATTEMPTS) {
            usleep(HW_RETRY_SLEEP_US);
            continue;
        }

        FD_LOG_ERROR("%s: HW write FAILED: addr=0x%08lx size=%u "
                     "val=0x%08lx pc=0x%08lx irq_pending=%d halted=%d "
                     "attempts=%d",
                s->backend_name,
                (unsigned long)addr, size, (unsigned long)value,
                (unsigned long)pc, s->irq_pending, halted, attempt);
        return -1;
    }
}

int hw_session_serve(hw_session_t *s, int qemu_line)
{
    (void)qemu_line; /* see header: we resume on the captured board line */

    if (!s || !s->hw) return -1;

    pthread_mutex_lock(&s->hw_mutex);

    /*
     * Idempotent: if no IRQ is pending (e.g. duplicate dispatch from a
     * future dev.c IRQ fan-out), do nothing. The board is already running.
     */
    if (s->irq_pending == 0) {
        pthread_mutex_unlock(&s->hw_mutex);
        return 0;
    }

    /* Satisfy the monitor firmware's `cmp r1, <id>` check. */
    hw_write_reg(s->hw, R1, s->irq_pending);
    hw_board_run(s->hw);
    s->irq_pending = 0;
    pthread_cond_signal(&s->irq_cv);
    pthread_mutex_unlock(&s->hw_mutex);
    return 0;
}
