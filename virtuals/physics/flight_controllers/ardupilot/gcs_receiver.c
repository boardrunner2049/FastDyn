#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <errno.h>
#include <core.h>
#include "ring_buffer.h"
#include "mavlink_lib.h"
#include "mavlink.h"

#define RING_BUFFER_SIZE 512
#define DEFAULT_GCS_UDP_PORT 14551
#define DEFAULT_GPS_INPUT_UDP_PORT 14553
#define MAX_PACKET_SIZE 1024
#define RED   "\033[31m"
#define RESET "\033[0m"


static pthread_t gcs_listener_tid;
static pthread_t gps_input_listener_tid;
static bool gcs_listener_running = false;
static bool gcs_shutdown_hook_registered = false;
static bool gcs_shutdown_complete = false;

static struct sockaddr_in gcs_addr;
static int send_sockfd = -1;
static bool send_sockfd_initialized = false;

static struct sockaddr_in gps_input_addr;
static int gps_input_sockfd = -1;
static bool gps_input_sockfd_initialized = false;

static int flight_log_fd_array[100] = {-1};
static int flight_log_fd_index = 0;

fuzzed_input_t g_fuzzed_input = {0};

typedef struct {
    RingBuffer *ring_buffer;
    uint16_t port;
    bool updates_gcs_endpoint;
    const char *name;
} udp_listener_config_t;

static udp_listener_config_t gcs_listener_config;
static udp_listener_config_t gps_input_listener_config;

static uint16_t udp_port_from_env(const char *name, uint16_t default_port) {
    const char *value = getenv(name);
    if (!value || value[0] == '\0') {
        return default_port;
    }

    char *end = NULL;
    errno = 0;
    long port = strtol(value, &end, 10);
    if (errno != 0 || *end != '\0' || port <= 0 || port > 65535) {
        fprintf(stderr, "Invalid %s=%s, using default port %u\n",
                name, value, default_port);
        return default_port;
    }

    return (uint16_t)port;
}

static uint16_t gcs_udp_port(void) {
    const char *mavlink_in = getenv("MAVLINK_IN_PORT");
    if (mavlink_in && mavlink_in[0] != '\0') {
        return udp_port_from_env("MAVLINK_IN_PORT", DEFAULT_GCS_UDP_PORT);
    }
    return udp_port_from_env("FASTDYN_MAVLINK_FIRMWARE_PORT", DEFAULT_GCS_UDP_PORT);
}

void print_fuzzed_input(const fuzzed_input_t* input) {
    printf("Last Fuzzed Input:\n");
    printf("  MsgID: %u\n", input->msgid);
    printf("  CRC Extra: %u\n", input->crc_extra);
    printf("  Length: %u\n", input->length);
    printf("  Real Length: %u\n", input->real_length);
    printf("  SysID: %u\n", input->sysid);
    printf("  CompID: %u\n", input->compid);
    printf("  Payload: ");
    for (int i = 0; i < input->real_length; i++) {
        printf("%02X ", input->payload[i]);
    }
    printf("\n");
}

void assign_fuzzed_input(uint32_t msgid,
                        uint8_t crc_extra,
                        const uint8_t *payload,
                        uint8_t length,
                        uint8_t real_length,
                        uint8_t sysid,
                        uint8_t compid)
{
    g_fuzzed_input.msgid = msgid;
    g_fuzzed_input.crc_extra = crc_extra;
    g_fuzzed_input.length = length;
    g_fuzzed_input.real_length = real_length;
    g_fuzzed_input.sysid = sysid;
    g_fuzzed_input.compid = compid;
    // clear previous payload
    memset(g_fuzzed_input.payload, 0, sizeof(g_fuzzed_input.payload));
    // copy new payload
    memcpy(g_fuzzed_input.payload, payload, real_length);
}

void mark_open_flight_log_fd(int fd) {
    flight_log_fd_array[flight_log_fd_index] = fd;
    flight_log_fd_index++;
}

void mark_close_flight_log_fd(int fd) {
    for (int i = 0; i < flight_log_fd_index; i++) {
        if (flight_log_fd_array[i] == fd) {
            flight_log_fd_array[i] = -1;
            break;
        }
    }
}

static void close_all_flight_log_fds(void) {
    for (int i = 0; i < flight_log_fd_index; i++) {
        if (flight_log_fd_array[i] != -1) {
            close(flight_log_fd_array[i]);
            flight_log_fd_array[i] = -1;
        }
    }
    flight_log_fd_index = 0;
}

static void gcs_shutdown(void) {
    if (gcs_shutdown_complete) {
        return;
    }
    gcs_shutdown_complete = true;

    printf("[Main] Closing all flight log file descriptors...\n");
    close_all_flight_log_fds();
    printf("[Main] Flight log file descriptors closed.\n");

    if (gcs_listener_running) {
        printf("[Main] Sending cancellation request to MAVLink listener threads...\n");
        fflush(stdout);
        pthread_cancel(gcs_listener_tid);
        pthread_cancel(gps_input_listener_tid);
        pthread_join(gcs_listener_tid, NULL);
        pthread_join(gps_input_listener_tid, NULL);
        gcs_listener_running = false;
    }

    print_fuzzed_input(&g_fuzzed_input);
    printf("[Main] MAVLink listener threads joined.\n");
    fflush(stdout);
}

// const char * mavlink_dictionary(uint32_t message_id){
//     switch(message_id){
//         case COMMAND_LONG:
//             return "COMMAND_LONG";
//         default:
//             return "UNKNOWN_MESSAGE_ID";
//     }
// }

void translate_and_forward(const mavlink_message_t* msg) {
    // Example: Print message ID and system/component IDs
    if (msg->compid == 220) {
        // Ignore messages from companion computer
        return;
    }

    // // write every message out to a file "/root/rooney/FastDyn/courbet/mavlink/mavlink_received.log"
    // FILE *log_file = fopen("/root/rooney/FastDyn/courbet/mavlink/mavlink_received.log", "a");
    // if (log_file) {
    //     if (msg->msgid == MAVLINK_MSG_ID_COMMAND_LONG) {
    //         mavlink_command_long_t command;
    //         mavlink_msg_command_long_decode(msg, &command);
    //         fprintf(log_file, "[GCS -> Drone] Received COMMAND_LONG: Command=%d, Param1=%.2f, Param2=%.2f, Param3=%.2f, Param4=%.2f, Param5=%.2f, Param6=%.2f, Param7=%.2f\n",
    //                 command.command,
    //                 command.param1,
    //                 command.param2,
    //                 command.param3,
    //                 command.param4,
    //                 command.param5,
    //                 command.param6,
    //                 command.param7);
    //     } else {
    //         fprintf(log_file, "[GCS -> Drone] Received MAVLink Message ID: %d from SYSID: %d, COMPID: %d, Length: %d\n",
    //                 msg->msgid, msg->sysid, msg->compid, msg->len);
    //         fclose(log_file);
    //     }
    // }
}

// Parser state (can be global or per-connection)
static mavlink_status_t status;
static mavlink_message_t msg;

// Feed one byte at a time
void mavlink_input_byte(uint8_t c) {
    if (mavlink_parse_char(MAVLINK_COMM_0, c, &msg, &status)) {
        // Full message received
        translate_and_forward(&msg);
    }
}

// UDP listener process
static void* udp_listener(void *arg) {
    udp_listener_config_t *config = (udp_listener_config_t *)arg;
    RingBuffer * ring_buffer = config->ring_buffer;

    int sockfd;
    struct sockaddr_in servaddr, cliaddr;
    unsigned char buffer[MAX_PACKET_SIZE];

    if ((sockfd = socket(AF_INET, SOCK_DGRAM, 0)) < 0) {
        perror("socket creation failed");
        exit(EXIT_FAILURE);
    }

    memset(&servaddr, 0, sizeof(servaddr));
    servaddr.sin_family = AF_INET;
    servaddr.sin_addr.s_addr = INADDR_ANY;
    servaddr.sin_port = htons(config->port);

    if (bind(sockfd, (const struct sockaddr *)&servaddr, sizeof(servaddr)) < 0) {
        perror("bind failed");
        close(sockfd);
        exit(EXIT_FAILURE);
    }

    printf("[%s]: Listening for incoming data on UDP port %u...\n",
           config->name, config->port);

    while (1) {
        socklen_t len = sizeof(cliaddr);
        ssize_t n = recvfrom(sockfd, buffer, MAX_PACKET_SIZE, 0,
                             (struct sockaddr *)&cliaddr, &len);
        // printf("GCSReceiver: Received %zd bytes\n", n);
        if (n < 0) {
            perror("recvfrom failed");
            continue;
        }

        if (n == 0) {
            // No data received
            sleep(1);
            continue;
        }

        // fprintf(stderr, "[GCSReceiver]: Received %zd bytes from %s:%d\n", n,
        //         inet_ntoa(cliaddr.sin_addr), ntohs(cliaddr.sin_port));

        if (config->updates_gcs_endpoint && !send_sockfd_initialized) {
            send_sockfd = socket(AF_INET, SOCK_DGRAM, 0);
            if (send_sockfd < 0) {
                perror("send socket creation failed");
                close(sockfd);
                exit(EXIT_FAILURE);
            }

            memset(&gcs_addr, 0, sizeof(gcs_addr));
            gcs_addr.sin_family = AF_INET;
            // use sender's src port and send back
            gcs_addr.sin_port = cliaddr.sin_port; // Use sender's port
            gcs_addr.sin_addr = cliaddr.sin_addr; // Use sender's IP

            send_sockfd_initialized = true;

            printf("[GCSReceiver]: Initialized send socket to GCS at %s:%d\n" RESET,
                   inet_ntoa(gcs_addr.sin_addr), ntohs(gcs_addr.sin_port));
        }

        // Put received bytes into the ring buffer
        size_t count = ring_buffer_count(ring_buffer);
        if (count + n > RING_BUFFER_SIZE)
        {
            // Buffer overflow, drop incoming packet
            // printf("GCSReceiver: Buffer overflow, dropping packet\n");
        }
        else
        {
            for (ssize_t i = 0; i < n; i++) {
                if (!ring_buffer_put(ring_buffer, buffer[i])) {
                    printf("GCSReceiver: Failed to put byte into buffer\n");
                    continue;
                }
                // Also feed byte to MAVLink parser
                // mavlink_input_byte(buffer[i]);
            }
        }
    }

    close(sockfd);
}

void start_gcs_listener(RingBuffer *rb) {
    if (!gcs_shutdown_hook_registered) {
        core_register_exit_hook(gcs_shutdown);
        gcs_shutdown_hook_registered = true;
    }

    gcs_listener_config = (udp_listener_config_t) {
        .ring_buffer = rb,
        .port = gcs_udp_port(),
        .updates_gcs_endpoint = true,
        .name = "GCSReceiver"
    };
    gps_input_listener_config = (udp_listener_config_t) {
        .ring_buffer = rb,
        .port = udp_port_from_env("GPS_INPUT_PORT", DEFAULT_GPS_INPUT_UDP_PORT),
        .updates_gcs_endpoint = false,
        .name = "GPSInputReceiver"
    };

    pthread_create(&gcs_listener_tid, NULL, udp_listener, (void *)&gcs_listener_config);
    pthread_create(&gps_input_listener_tid, NULL, udp_listener, (void *)&gps_input_listener_config);

    gcs_listener_running = true;
}

// Example function to read a byte
void read_byte(RingBuffer *rb, unsigned char *byte) {
    if (!gcs_listener_running)
    {
        start_gcs_listener(rb);
        usleep(1000);
    }
    if (!ring_buffer_get(rb, byte)) {
        *byte = 0; // Return null byte if empty
    }
    return;
}

// Example function to check bytes available
size_t bytes_available(RingBuffer *rb) {
    if (!gcs_listener_running)
    {
        start_gcs_listener(rb);
        usleep(1000);
    }
    return ring_buffer_count(rb);
}

int send_mavlink_payload(uint32_t message_id,
                         const uint8_t *payload,
                         uint8_t length,
                         uint8_t crc_extra,
                         uint8_t *sequence) {
    if (!send_sockfd_initialized) {
        fprintf(stderr, "Send socket not initialized. Cannot send MAVLink message.\n");
        return -1;
    }
    return mav_finalize_message_chan_send(send_sockfd, &gcs_addr, message_id, payload, length, crc_extra, sequence);
}

void send_mavlink_gps_input(uint8_t system_id, uint8_t component_id, const gps_input_t *gps) {
    if (!gps) {
        fprintf(stderr, "Invalid GPS data. Cannot send GPS_INPUT message.\n");
        return;
    }

    mavlink_message_t msg;

    // Convert latitude and longitude to int32 (degrees * 1E7)
    int32_t lat = (int32_t)llround(gps->latitude_deg * 1e7);
    int32_t lon = (int32_t)llround(gps->longitude_deg * 1e7);

    // GPS time-of-week in ms
    uint32_t gps_tow_ms = gps->timestamp_sec * 1000 + gps->timestamp_nsec / 1000000;
    uint64_t time_usec = (uint64_t)gps_tow_ms * 1000ULL;

    // Hardcoded GPS week (replace with real computation if desired)
    uint16_t gps_week = 15;

    // calculate yaw from velocity components
    float yaw_rad = atan2f(gps->velocity_e, gps->velocity_n);
    float yaw_deg = yaw_rad * (180.0f / 3.14159265f);
    if (yaw_deg < 0) {
        yaw_deg += 360.0f;
    }

    // Pack the MAVLink GPS_INPUT message (21 arguments)
    mavlink_msg_gps_input_pack(
        system_id,
        component_id,
        &msg,
        time_usec,                        // time_usec
        0,                                // gps_id
        0,                                // ignore_flags
        gps_tow_ms,                        // time_week_ms
        gps_week,                           // time_week
        gps->fix_type,                     // fix_type
        lat,                               // lat
        lon,                               // lon
        (float)(gps->altitude_m),          // alt in m
        0.0f,                              // hdop
        0.0f,                              // vdop
        gps->velocity_n,                   // vn
        gps->velocity_e,                   // ve
        gps->velocity_d,                   // vd
        0.0f,                              // speed_accuracy
        0.0f,                              // horiz_accuracy
        0.0f,                              // vert_accuracy
        gps->satellites_visible,           // satellites_visible
        (int)(yaw_deg * 100)          // yaw in centi-degrees
        // 100                                // 1 degree fixed yaw for testing
        // 2200                               // yaw in centi-degrees (fixed to 2200 for testing)
    );

    // Serialize the message into a buffer
    uint8_t buffer[MAVLINK_MAX_PACKET_LEN];
    uint16_t len = mavlink_msg_to_send_buffer(buffer, &msg);

    // Send over the global UDP socket to the dedicated GPS_INPUT port.
    if (!gps_input_sockfd_initialized) {
        gps_input_sockfd = socket(AF_INET, SOCK_DGRAM, 0);
        if (gps_input_sockfd < 0) {
            perror("GPS input socket creation failed");
            return;
        }

        memset(&gps_input_addr, 0, sizeof(gps_input_addr));
        gps_input_addr.sin_family = AF_INET;
        gps_input_addr.sin_port = htons(udp_port_from_env("GPS_INPUT_PORT", DEFAULT_GPS_INPUT_UDP_PORT));
        gps_input_addr.sin_addr.s_addr = inet_addr("127.0.0.1");

        gps_input_sockfd_initialized = true;
    }

    ssize_t sent = sendto(gps_input_sockfd, buffer, len, 0, (const struct sockaddr*)&gps_input_addr, sizeof(gps_input_addr));
    if (sent < 0) {
        perror("sendto");
    }
}


// int main() {
//     RingBuffer rb;
//     if (!ring_buffer_init(&rb, RING_BUFFER_SIZE)) {
//         fprintf(stderr, "Failed to initialize ring buffer\n");
//         return EXIT_FAILURE;
//     }

//     start_gcs_listener(&rb);

//     while (1) {
//         unsigned char b;
//         read_byte(&rb, &b);
//         if (b != 0x00) {
//             printf("Received byte: 0x%02x\n", b);
//         } else {
//             // No data, sleep a bit
//             usleep(1000);
//         }
//     }

//     pthread_join(gcs_listener_tid, NULL);

//     return 0;
// }
