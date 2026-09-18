#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include "fuzz.h"
#include "afl-fastdyn.h"

#define AFLNET_POLL_TIMEOUT_MS 5000

extern uint8_t CVG[MAP_SIZE];

extern void *afl_main(void *arg);

#if ENABLE_AFLNET && ENABLE_LIBFUZZ
#error "AFLNet and LibAFL backends are mutually exclusive; configure one backend."
#endif

#if ENABLE_AFLNET && !ENABLE_LIBFUZZ
/* AFLNet targets use explicit protocol virtuals, not a schema callback. */
void generic_configure(int argc, char **argv)
{
    (void)argc;
    (void)argv;
}

void generic_callback(void)
{
}

void schema_activate_post_snapshot_modifiers(void)
{
}
#endif

static pthread_t g_aflnet_thread;
static int g_tap_fd = -1;
static uint64_t g_active_seq = 0;
static uint64_t g_restore_seq = 0;
static uint8_t g_msg_buf[FASTDYN_MAX_FRAME];
static uint8_t g_wire_buf[sizeof(fastdyn_msg_hdr_t) + FASTDYN_MAX_FRAME];

static int dir_exists(const char *path)
{
    struct stat st;
    return (stat(path, &st) == 0) && S_ISDIR(st.st_mode);
}

static const char *env_or_default(const char *name, const char *fallback)
{
    const char *value = getenv(name);
    return (value && value[0]) ? value : fallback;
}

static bool build_default_path(char *out, size_t out_size, const char *leaf)
{
    const char *work_dir = env_or_default("FASTDYN_WORK_DIR", "fastdyn_work");
    int written = snprintf(out, out_size, "%s/%s", work_dir, leaf);
    return written >= 0 && (size_t)written < out_size;
}

static bool launch_aflnet(void)
{
    static char default_in_dir[4096];
    static char default_out_dir[4096];
    static char *argv[] = {
        "afl-fuzz",
        "-m", "none",
        "-d",
        "-i", NULL,
        "-o", NULL,
        "-P", NULL,
        "-t", NULL,
        "-D", NULL,
        "-W", NULL,
        "-h", "1",
        "-K",
        "-R",
        "-X",
        NULL
    };

    if (!build_default_path(default_in_dir, sizeof(default_in_dir), "aflnet-in") ||
        !build_default_path(default_out_dir, sizeof(default_out_dir), "aflnet-out")) {
        fprintf(stderr, "[aflnet] FASTDYN_WORK_DIR is too long\n");
        return false;
    }

    const char *out_dir = env_or_default("FASTDYN_AFLNET_OUT_DIR", default_out_dir);
    const char *in_dir = getenv("FASTDYN_AFLNET_IN_DIR");
    if (!in_dir || !in_dir[0]) {
        if (dir_exists(out_dir)) {
            in_dir = "-";
        } else if (dir_exists(default_in_dir)) {
            in_dir = default_in_dir;
        } else {
            fprintf(stderr,
                    "[aflnet] Set FASTDYN_AFLNET_IN_DIR (or create %s) for initial seeds\n",
                    default_in_dir);
            return false;
        }
    }

    const char *protocol = getenv("FASTDYN_AFLNET_PROTOCOL");
    if (!protocol || !protocol[0]) {
        fprintf(stderr, "[aflnet] Set FASTDYN_AFLNET_PROTOCOL for the target protocol\n");
        return false;
    }

    /* These campaigns retain only execution coverage. Do not reject an
     * otherwise runnable target merely because the host routes core dumps to
     * a crash-reporting helper or uses a dynamic CPU governor: AFLNet's
     * crash-accounting and host-frequency warnings are not relevant to the
     * in-process FastDyn transport, and changing host kernel settings would
     * make a target configuration non-self-contained. Preserve an explicit
     * caller choice when one is already present. */
    if (getenv("AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES") == NULL &&
        setenv("AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES", "1", 0) != 0) {
        perror("[aflnet] failed to configure coverage-only crash handling");
        return false;
    }
    if (getenv("AFL_SKIP_CPUFREQ") == NULL &&
        setenv("AFL_SKIP_CPUFREQ", "1", 0) != 0) {
        perror("[aflnet] failed to skip host CPU-frequency validation");
        return false;
    }

    argv[5] = (char *)in_dir;
    argv[7] = (char *)out_dir;
    argv[9] = (char *)protocol;
    argv[11] = (char *)env_or_default("FASTDYN_AFLNET_TIMEOUT_MS", "100000");
    argv[13] = (char *)env_or_default("FASTDYN_AFLNET_NET_TIMEOUT_MS", "100000000");
    argv[15] = (char *)env_or_default("FASTDYN_AFLNET_WAIT_TIMEOUT_MS", "100000");

    if (pthread_create(&g_aflnet_thread, NULL, afl_main, (void *)argv) != 0) {
        perror("Failed to create AFLNet thread");
        return false;
    }

    return true;
}

static bool send_fastdyn_msg(fastdyn_msg_type_t type,
                             uint64_t seq,
                             const uint8_t *data,
                             size_t len)
{
    if (g_tap_fd < 0 || len > FASTDYN_MAX_FRAME) {
        return false;
    }

    size_t total = sizeof(fastdyn_msg_hdr_t) + len;
    uint8_t *packet = malloc(total);
    if (packet == NULL) {
        return false;
    }

    fastdyn_msg_hdr_t hdr = {
        .magic = FASTDYN_MSG_MAGIC,
        .type = (uint32_t)type,
        .seq = seq,
        .len = (uint32_t)len,
    };

    memcpy(packet, &hdr, sizeof(hdr));
    if (len != 0) {
        memcpy(packet + sizeof(hdr), data, len);
    }

    ssize_t written;
    do {
        written = send(g_tap_fd, packet, total, MSG_NOSIGNAL);
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd pfd = { .fd = g_tap_fd, .events = POLLOUT };
            int ready = poll(&pfd, 1, AFLNET_POLL_TIMEOUT_MS);
            if (ready > 0) {
                continue;
            }
        }
        break;
    } while (true);
    free(packet);

    return written == (ssize_t)total;
}

static bool recv_fastdyn_msg(fastdyn_msg_hdr_t *hdr,
                             uint8_t **payload)
{
    ssize_t rd = read(g_tap_fd, g_wire_buf, sizeof(g_wire_buf));
    if (rd < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            return false;
        }
        perror("[aflnet] failed to read input");
        return false;
    }

    if (rd < (ssize_t)sizeof(*hdr)) {
        return false;
    }

    memcpy(hdr, g_wire_buf, sizeof(*hdr));
    if (hdr->magic != FASTDYN_MSG_MAGIC ||
        hdr->len > FASTDYN_MAX_FRAME ||
        rd != (ssize_t)(sizeof(*hdr) + hdr->len)) {
        return false;
    }

    *payload = g_wire_buf + sizeof(*hdr);
    return true;
}

static bool setup_aflnet_backend(void)
{
    memset(CVG, 0, sizeof(CVG));

    int fds[2];
    if (socketpair(AF_UNIX, SOCK_DGRAM, 0, fds) < 0) {
        perror("[aflnet] socketpair() failed");
        return false;
    }

    fcntl(fds[0], F_SETFL, O_NONBLOCK);
    fcntl(fds[1], F_SETFL, O_NONBLOCK);

    if (fastdyn_attach_fd(fds[1]) != 0) {
        perror("[aflnet] failed to attach in-process transport");
        close(fds[0]);
        close(fds[1]);
        return false;
    }

    g_tap_fd = fds[0];
    g_active_seq = 0;
    g_restore_seq = 0;
    return true;
}

static bool complete_previous_message(void)
{
    if (g_active_seq == 0) {
        return true;
    }

    if (!send_fastdyn_msg(FASTDYN_MSG_DONE, g_active_seq, NULL, 0)) {
        return false;
    }

    g_active_seq = 0;
    return true;
}

static bool read_next_frame(fuzz_backend_msg_t *msg)
{
    while (true) {
        struct pollfd pfd = {
            .fd = g_tap_fd,
            .events = POLLIN,
        };

        int ret = poll(&pfd, 1, AFLNET_POLL_TIMEOUT_MS);

        if (ret == 0) {
            continue;
        }

        if (ret < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("[aflnet] failed to poll for input");
            return false;
        }

        fastdyn_msg_hdr_t hdr;
        uint8_t *payload = NULL;
        if (!recv_fastdyn_msg(&hdr, &payload)) {
            continue;
        }

        if (hdr.type == FASTDYN_MSG_RESTORE) {
            msg->data = NULL;
            msg->len = 0;
            msg->restore = true;
            g_restore_seq = hdr.seq;
            return true;
        }

        if (hdr.type != FASTDYN_MSG_INPUT) {
            continue;
        }

        memcpy(g_msg_buf, payload, hdr.len);
        g_active_seq = hdr.seq;
        msg->data = g_msg_buf;
        msg->len = hdr.len;
        msg->restore = false;

        return true;
    }
}

bool fuzz_backend_init(void)
{
    if (!setup_aflnet_backend()) {
        return false;
    }

    return launch_aflnet();
}

bool fuzz_backend_next(fuzz_backend_msg_t *msg)
{
    if (msg == NULL || g_tap_fd < 0) {
        return false;
    }

    if (!complete_previous_message()) {
        return false;
    }
    return read_next_frame(msg);
}

void fuzz_backend_report_assert(bool fatal)
{
    if (fatal) {
        complete_previous_message();
    }
}

void fuzz_backend_restore_complete(void)
{
    if (g_restore_seq != 0 &&
        send_fastdyn_msg(FASTDYN_MSG_RESTORE_DONE, g_restore_seq, NULL, 0)) {
        g_restore_seq = 0;
    }
}

void fuzz_backend_set_data(const uint8_t *buf, size_t len)
{
    if (g_tap_fd < 0 || buf == NULL || len == 0) {
        return;
    }

    if (!send_fastdyn_msg(FASTDYN_MSG_RESPONSE, g_active_seq, buf, len) &&
        errno != EAGAIN && errno != EWOULDBLOCK) {
        perror("[aflnet] failed to write response");
    }
}
