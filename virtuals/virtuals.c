#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdio.h>
#include <glib.h>
#include <stdatomic.h>

#include <qemu/qemu-plugin.h>
#include <sys/time.h>
#include <time.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#include <utils.h>
#include <device.h>
#if ENABLE_LIBPY
#include <python.h>
#endif
#include <virtuals.h>
#include <fastdyn_runtime.h>
// #include "ardupilot_virtuals.c"
#if ENABLE_LIBGZ
    #include "phy.h"
#endif

#include <sys/mman.h>
#include <fcntl.h>

// External dependencies from core.c
extern AddressList addressLists[];
extern size_t listCount;
extern uint32_t qemu_get_register(int reg);
extern void qemu_set_register(uint32_t value, int reg);

// Global to track last sim time from QEMU
_Atomic int64_t * last_sim_time_ns = NULL;
static uint64_t periodic_irq_period_ns = 1000000ULL;

//TODO: remove this.
#if ENABLE_LIBPY
#include <Python.h>
#include "../python/python.c"
#endif

#include <virtuals/virt_fuzz.h>
#include <virtuals/phy.h>

#define VIRTUALS_MAX_COUNT 256

// Track how many are currently used
static int registry_count = -1;
/**
 * @brief Callback registry
 *
 * To register a new callback, add a new entry to this list,
 * declare the function in virtuals.h and implement it here.
 *
 */
cb_entry_t cb_registry[VIRTUALS_MAX_COUNT] = {
	{ "printreg", printreg },
    { "updatemem", updatemem},
    { "randstate", randstate},
    { "raiseirq", raiseirq },
    { "pulseirq", pulseirq },
    { "dumplog", dumplogger},
    { "dyninst", dyninst},
    { "timer_start", timer_start},
    { "start_budgeting", start_budgeting},
    { "dyninst_lib", dyninst_lib},
    { "debug_log", debug_log},
    { "raise_periodic_irq", raise_periodic_irq},
    {"benchmark_start", benchmark_start},
	{"benchmark_end", benchmark_end},
	{"bench_tick", bench_tick},

    {NULL, NULL} //This should always be last entry.
};


// Function to lookup callbacks by name (moved from core.c)
cb_func_t lookup_callback(const char *name) {
    for (size_t i = 0; i < registry_count; i++) {
        if (strcmp(cb_registry[i].name, name) == 0)
            return cb_registry[i].func;
    }
    return NULL;
}

// Timer callback
static void my_timer_callback(void *opaque) {
    printf("Virtual Clock: %li\n", qemu_plugin_get_virtual_timer());
}

// Virtual instruction functions
void raiseirq(unsigned int cpu_index, void *udata) {
	 // Interpret udata as a string
    const char *str = (const char *)udata;

    // Convert string to integer (auto-detect base: 0x = hex, 0 = octal, else decimal)
    unsigned long num = strtoul(str, NULL, 0);

    qemu_plugin_raise_irq(num, false);
}

/**
 * Create a shared memory region holding the latest sim time so external processes,
 * like simulation observers, can read it. Also initializes the sim time to 0.
 * Returns 0 on success, -1 on failure.
 */
int shared_sim_time_init() {

    int fd = shm_open("/last_sim_time_ns", O_CREAT | O_RDWR, 0666);
    ftruncate(fd, sizeof(_Atomic int64_t));
    last_sim_time_ns = (_Atomic int64_t *) mmap(0, sizeof(_Atomic int64_t), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (!last_sim_time_ns) {
        return -1;
    }

    atomic_store(last_sim_time_ns, 0);
    close(fd);
    return 0;
}

void kick_irq(void *opaque) {
    int irq_num = *((int *)opaque);

#if ENABLE_PHY
    int64_t sim_time_ns = (int64_t)qemu_plugin_get_virtual_timer();

    // The FMU backend is lockstep with the firmware timer tick: publish the
    // physics state for this simulated timestamp before the OS handles the IRQ.
    if (phy_backend_is("fmu")) {
        double target_time_s = (double)sim_time_ns / 1e9;
        if (!phy_advance_simulation(target_time_s)) {
            fprintf(stderr, "Failed to advance FMU simulation to %.6f s\n", target_time_s);
        }
    }

    // Keep the legacy async catch-up path informed for other physics backends.
    atomic_store(last_sim_time_ns, sim_time_ns);

    // // TODO:
    // static bool physics_initialized = false;
    // if (!physics_initialized) {
    //     if (!phy_select_backend("gazebo")) {
    //         fprintf(stderr, "Failed to initialize physics backend\n");
    //         exit(EXIT_FAILURE);
    //     }
    //     physics_initialized = true;
    // }
#endif

    qemu_plugin_raise_irq(irq_num, false);
}

// void periodic_irq_func(void *arg) {
//     int irq_num = *((int *)arg);

//     while (1) {
//         kick_irq((void *)&irq_num);
//         usleep(100); // Sleep for 0.1 ms
//     }
// }

void raise_periodic_irq(unsigned int cpu_index, void *udata) {
    // Interpret udata as a string
    const char *str = (const char *)udata;

    // Convert string to integer (auto-detect base: 0x = hex, 0 = octal, else decimal)
    char *end = NULL;
    unsigned long num = strtoul(str, &end, 0);
    static uint32_t irq_num = 0;
    if (num != 0) {
        irq_num = num;
    }
    uint64_t period_ns = periodic_irq_period_ns;
    while (end && (*end == ',' || *end == ':' || isspace((unsigned char)*end))) {
        end++;
    }
    if (end && *end) {
        errno = 0;
        unsigned long long parsed_period = strtoull(end, NULL, 0);
        if (errno == 0 && parsed_period > 0) {
            period_ns = parsed_period;
        } else {
            fprintf(stderr, "Invalid periodic IRQ period '%s'; using %" PRIu64 " ns\n",
                    end, period_ns);
        }
    }

    printf("Periodic IRQ %u every %" PRIu64 " ns\n", irq_num, period_ns);
    qemu_plugin_timer_new_period_ns(kick_irq, (void *)&irq_num, period_ns);
    // create thread that raises irq every 1 ms
    // pthread_t periodic_irq_thread;
    // if (!pthread_create(&periodic_irq_thread, NULL, (void *(*)(void *))periodic_irq_func, (void *)&irq_num)) {
    //     pthread_detach(periodic_irq_thread);
    // } else {
    //     fprintf(stderr, "Failed to create periodic IRQ thread\n");
    // }
}

void pulseirq(unsigned int cpu_index, void *udata) {
	// Interpret udata as a string
    const char *str = (const char *)udata;

    // Convert string to integer (auto-detect base: 0x = hex, 0 = octal, else decimal)
    unsigned long num = strtoul(str, NULL, 0);
    qemu_plugin_pulse_irq(num);
}

void updatemem(unsigned int cpu_index, void *udata) {
    const char *input = (const char *) udata;
    MemAccess mem;

    if (parse_update_mem_arg(input, &mem) == 0) {
        if (mem.mode == 'r') {
            qemu_plugin_read_memory(mem.address, mem.buffer, mem.length);
            printf("Read %u bytes from 0x%08x: ", mem.length, mem.address);
            for (uint32_t i = 0; i < mem.length; i++) {
                printf("0x%02x", mem.buffer[i]);
                if (i < mem.length - 1) {
                    printf(", ");
                }
            }
            printf("\n");
        } else {
            qemu_plugin_write_memory(mem.address, mem.buffer, mem.length);
        }
    }
}

unsigned char get_random_byte(void) {
    //This will make things linux specific, but lot of hardcoded things.
    FILE *fp = fopen("/dev/urandom", "rb");
    if (!fp) {
        perror("fopen /dev/urandom");
        exit(EXIT_FAILURE);
    }

    unsigned char byte;
    size_t result = fread(&byte, 1, 1, fp);
    fclose(fp);

    if (result != 1) {
        fprintf(stderr, "Failed to read from /dev/urandom\n");
        exit(EXIT_FAILURE);
    }

    return byte;
}

uint32_t get_random_word(void) {
    //This will make things linux specific, but lot of hardcoded things.
    FILE *fp = fopen("/dev/urandom", "rb");
    if (!fp) {
        perror("fopen /dev/urandom");
        exit(EXIT_FAILURE);
    }

    uint32_t word;
    size_t result = fread(&word, sizeof(uint32_t), 1, fp);
    fclose(fp);

    if (result != 1) {
        fprintf(stderr, "Failed to read from /dev/urandom\n");
        exit(EXIT_FAILURE);
    }

    return word;
}

void randstate(unsigned int cpu_index, void *udata) {
    const char *input = (const char *) udata;

    size_t count = 0;
    unsigned long long *addrs = parse_addresses(input, &count);
    if (addrs) {
        for (size_t i = 0; i < count; i++) {
            if (addrs[i] < 100) {
                uint32_t val = get_random_word();
                //PC not supported
                if (addrs[i] != 15) {
                    qemu_plugin_set_register((uint8_t *)&val, addrs[i]);
                }
            } else {
                uint8_t val = get_random_byte();
                qemu_plugin_write_memory(addrs[i], &val, 1);
            }
        }
        free(addrs);
    } else {
        printf("Failed to parse addresses.\n");
    }
}

void debug_log(unsigned int cpu_index, void *udata) {
    // print sim time from qemu

    uint64_t sim_time_ns = qemu_plugin_get_virtual_timer();
    double fractional_seconds = (double)(sim_time_ns % 1000000000) / 1e9;

    const char *msg = (const char *)udata;

    printf("[%u][%.6f] : %s\n", cpu_index, fractional_seconds, msg);
}

static struct timespec g_bench_start_ts;
static bool g_bench_started = false;
static volatile uint64_t g_bench_tick_count = 0;

void benchmark_start(unsigned int cpu_index, void *udata) {
    (void)cpu_index;
    (void)udata;
    /* No I/O here: printfs would land inside the measured region and pollute
     * the sample. Just capture the monotonic timestamp. */
    g_bench_tick_count = 0;
    clock_gettime(CLOCK_MONOTONIC, &g_bench_start_ts);
    g_bench_started = true;
}

void benchmark_end(unsigned int cpu_index, void *udata) {
    (void)cpu_index;
    struct timespec end_ts;
    clock_gettime(CLOCK_MONOTONIC, &end_ts);

    if (!g_bench_started) {
        fprintf(stderr, "[BENCH] benchmark_end called before benchmark_start\n");
        return;
    }

    int64_t delta_ns = (int64_t)(end_ts.tv_sec - g_bench_start_ts.tv_sec) * 1000000000LL
                     + (int64_t)(end_ts.tv_nsec - g_bench_start_ts.tv_nsec);

    const char *tag = (udata && *(const char *)udata) ? (const char *)udata : "";
    printf("[BENCH][%s] elapsed_ns=%lld elapsed_us=%.3f elapsed_s=%.9f\n",
           tag, (long long)delta_ns,
           (double)delta_ns / 1000.0,
           (double)delta_ns / 1e9);
    fflush(stdout);

    g_bench_started = false;
    /* Bench is done — exit QEMU immediately so drivers don't need to wait on
     * the trailing `while(1)` with a fixed `timeout`. Each run finishes as
     * soon as the [BENCH] line is emitted. */
    exit(0);
}

void bench_tick(unsigned int cpu_index, void *udata) {
    (void)cpu_index;
    (void)udata;
    g_bench_tick_count++;
}

void printreg(unsigned int cpu_index, void *udata) {
	unsigned long reg = strtoul((char *)(udata), NULL, 16);
	printf("Reg %lu:0x%08x\n", reg, qemu_get_register(reg));
}

void timer_start(unsigned int cpu_index, void *udata) {
    const char *msg = "Hello from QEMU timer!";
#if 00
    //One shot
    uint64_t timer = qemu_plugin_timer_new_ns(my_timer_callback, (void *)msg);
    qemu_plugin_timer_alarm(timer, 1e6);
#else
    //Periodic
    qemu_plugin_timer_new_period_ns(my_timer_callback, (void *)msg, 1e6);
#endif
}

void start_budgeting(unsigned int cpu_index, void *udata) {
    qemu_plugin_wait_for_budget();
}

void dyninst_lib(unsigned int cpu_index, void *udata) {
    qemu_plugin_load_elf((char *) udata);
}

void dyninst(unsigned int cpu_index, void *udata) {
    AddrFilePair parsed = parse_addr_file((char *)udata);

    size_t file_len = 0;
    void *file_buf = read_file(parsed.filename, &file_len);
    if (file_buf) {
        qemu_plugin_write_memory(parsed.addr, file_buf, file_len);
        free(file_buf);
    }
}

void dumplogger(unsigned int cpu_index, void *udata) {
    FileEntry entry;
    parse_file_entry((const char*) udata, &entry);
    dump_log_buffer_to_file(&addressLists[entry.idx], entry.file_name);
}

unsigned long long* parse_addresses(const char *input, size_t *count) {
    // Make a copy of input so we don't modify the original
    char *input_copy = strdup(input);
    if (!input_copy) return NULL;

    size_t capacity = 8;
    *count = 0;
    unsigned long long *addresses = malloc(capacity * sizeof(unsigned long long));
    if (!addresses) {
        free(input_copy);
        return NULL;
    }

    char *token = strtok(input_copy, ",");
    while (token) {
        // Remove leading/trailing whitespace
        while (*token == ' ' || *token == '\t') token++;
        char *endptr;
        unsigned long long addr = strtoull(token, &endptr, 0);
        if (token == endptr) {
            // Invalid conversion
            free(addresses);
            free(input_copy);
            return NULL;
        }

        if (*count >= capacity) {
            capacity *= 2;
            addresses = realloc(addresses, capacity * sizeof(unsigned long long));
            if (!addresses) {
                free(input_copy);
                return NULL;
            }
        }

        addresses[(*count)++] = addr;
        token = strtok(NULL, ",");
    }

    free(input_copy);
    return addresses;
}

int parse_update_mem_arg(const char *input, MemAccess *out) {
    if (!input || !out) return -1;

    // Temporary copy of input string for tokenizing
    char *temp = malloc(strlen(input) + 1);
    if (!temp) return -1;
    strcpy(temp, input);

    char *token = strtok(temp, ":");
    if (!token) { free(temp); return -1;}
    out->address = strtoul(token, NULL, 0); // parse address

    token = strtok(NULL, ":");
    if (!token || (token[0] != 'r' && token[0] != 'w')) return -1;
    out->mode = token[0]; // parse mode

    token = strtok(NULL, ":");
    if (!token) { free(temp); return -1;}
    out->length = strtoul(token, NULL, 0); // parse length
    if (out->length > MAX_BUFFER_SIZE) return -1;

    token = strtok(NULL, ":");
    if (!token) { free(temp); return -1;}

    // Now parse comma-separated bytes
    uint32_t i = 0;
    char *byte_str = strtok(token, ",");
    while (byte_str && i < out->length) {
        out->buffer[i++] = (uint8_t)strtoul(byte_str, NULL, 0);
        byte_str = strtok(NULL, ",");
    }

    if (i != out->length) { printf("Invalid Argument \n"); free(temp); return -1;}

    free(temp);
    return 0; // success
}

AddrFilePair parse_addr_file(const char *input) {
    AddrFilePair result = {0, {0}};

    const char *colon = strchr(input, ':');
    if (!colon) {
        fprintf(stderr, "Invalid format: no ':' found.\n");
        return result;
    }

    // Parse address part
    char addr_str[32] = {0}; // Enough for 64-bit address string
    size_t addr_len = colon - input;

    if (addr_len >= sizeof(addr_str)) {
        fprintf(stderr, "Address string too long.\n");
        return result;
    }

    strncpy(addr_str, input, addr_len);
    addr_str[addr_len] = '\0';

    result.addr = strtoull(addr_str, NULL, 0); // auto-detect 0x

    // Copy filename part into fixed buffer
    const char *filename = colon + 1;

    if (strlen(filename) >= MAX_FILENAME_LEN) {
        fprintf(stderr, "Filename too long. Truncated.\n");
        strncpy(result.filename, filename, MAX_FILENAME_LEN - 1);
        result.filename[MAX_FILENAME_LEN - 1] = '\0'; // Null-terminate
    } else {
        strcpy(result.filename, filename);
    }

    return result;
}

void* read_file(const char *filename, size_t *length) {
    FILE *file = fopen(filename, "rb");
    if (!file) {
        perror("Error opening file");
        return NULL;
    }

    // Seek to end to find file size
    if (fseek(file, 0, SEEK_END) != 0) {
        perror("Error seeking file");
        fclose(file);
        return NULL;
    }

    long file_size = ftell(file);
    if (file_size < 0) {
        perror("Error telling file position");
        fclose(file);
        return NULL;
    }
    rewind(file); // Go back to start

    // Allocate buffer
    void *buffer = malloc(file_size);
    if (!buffer) {
        perror("Memory allocation failed");
        fclose(file);
        return NULL;
    }

    // Read entire file into buffer
    size_t read_size = fread(buffer, 1, file_size, file);
    if (read_size != file_size) {
        perror("Error reading file");
        free(buffer);
        fclose(file);
        return NULL;
    }

    fclose(file);
    *length = file_size; // Return size
    return buffer;
}

bool parse_file_entry(const char* line, FileEntry* entry) {
    char* colonPos = strchr(line, ':');
    if (!colonPos) {
        return false; // No colon found — invalid format
    }

    // Split into index and file_name parts
    size_t idxLen = colonPos - line;
    char idxStr[32]; // Enough for int
    if (idxLen >= sizeof(idxStr)) return false; // Index too big to fit

    strncpy(idxStr, line, idxLen);
    idxStr[idxLen] = '\0';

    // Parse integer index
    entry->idx = atoi(idxStr);

    // Copy file name part
    strncpy(entry->file_name, colonPos + 1, sizeof(entry->file_name) - 1);
    entry->file_name[sizeof(entry->file_name) - 1] = '\0'; // Ensure null-terminated

    return true;
}

void dump_log_buffer_to_file(const AddressList* list, const char* filename) {
    FILE* file = fopen(filename, "wb");
    if (!file) {
        perror("fopen");
        return;
    }

    // Write the entire buffer
    size_t written = fwrite(list->log_buf.buffer, sizeof(uint32_t), (LOG_BUFFER_SIZE/sizeof(uint32_t)), file);
    if (written != LOG_BUFFER_SIZE) {
        fprintf(stderr, "Warning: Only wrote %zu words out of %u\n", written, LOG_BUFFER_SIZE);
    }

    fclose(file);
}


/**
 * Scans the static portion of the array to find the
 * initial count of hardcoded functions.
 */
static int virtuals_init_registry() {
    if (registry_count != -1) return -1;

    registry_count = 0;
    while (registry_count < VIRTUALS_MAX_COUNT &&
           cb_registry[registry_count].name != NULL) {
        registry_count++;
    }

	return 0;
}

/**
 * Registers a new function at runtime or updates an existing one.
 */
int virtual_register(const char *name, cb_func_t func) {
	virtuals_init_registry();

    if (!name || !func) return -1;

    // 1. Check if we're updating an existing entry
    for (int i = 0; i < registry_count; i++) {
        if (strcmp(cb_registry[i].name, name) == 0) {
			fprintf(stderr, "Registry Error: Entry '%s' already exists.\n", name);
            return -1; // Registration rejected
        }
    }

    // 2. Check for capacity (leaving room for a trailing sentinel)
    if (registry_count >= VIRTUALS_MAX_COUNT - 1) {
        fprintf(stderr, "Error: VIRTUALS_MAX_COUNT reached\n");
        return -1;
    }

    // 3. Append the new virtual
    cb_registry[registry_count].name = name;
    cb_registry[registry_count].func = func;
    registry_count++;

    // 4. Move the sentinel forward
    cb_registry[registry_count].name = NULL;
    cb_registry[registry_count].func = NULL;

    return 0;
}

int virtuals_init(int argc, char **argv) {
	int status = -1;
    const char *timer_period_arg = utils_get_arg("timer_irq_period_ns", argc, argv);
    if (timer_period_arg && timer_period_arg[0] != '\0') {
        errno = 0;
        unsigned long long parsed_period = strtoull(timer_period_arg, NULL, 0);
        if (errno == 0 && parsed_period > 0) {
            periodic_irq_period_ns = parsed_period;
        } else {
            fprintf(stderr, "Invalid timer_irq_period_ns '%s'; using %" PRIu64 " ns\n",
                    timer_period_arg, periodic_irq_period_ns);
        }
    }

	// Initialize registry
	status = virtuals_init_registry();

	if (status == -1) {
			utils_die("Couldn't init registry");
	}

    if (status != -1) {
        /* Compiled-in virtual modules self-register through the public runtime SDK. */
		if ((status = virtual_initialize_plugins()) < 0)
				utils_warn("One or more runtime plugins failed to initialize");
        /* Plugins from outside the tree were asked for explicitly (source = ...). */
		if (virtual_load_external_plugins(utils_get_arg("virtual_libraries", argc, argv)) < 0)
				utils_die("Could not load an out-of-tree plugin library (see the message above)");
#if ENABLE_PHY
        // It is ok to conditonally initialize here since sim_time is only used when PHY is enabled
		if ((status = shared_sim_time_init() < 0)) {
                utils_warn("Shared simulation time initialization failed");
        }
		if ((status = phy_init(argc, argv)) < 0)
				utils_warn("Physics Engine failed");
#endif
#if ENABLE_LIBFUZZ || ENABLE_AFLNET
		if ((status = fuzz_init(argc, argv)) < 0)
				utils_warn("Fuzzer Failed");
#endif
	}

    return 0;
}
