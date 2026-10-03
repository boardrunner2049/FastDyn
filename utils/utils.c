#include <utils.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>

#include <fastdyn/log.h>

// Global log handle
static FILE *fp;

void utils_die(const char *msg) {
    fprintf(stderr, "%s\n", msg);
    exit(EXIT_FAILURE);
}

void utils_warn(const char *msg) {
    fprintf(stderr, "%s\n", msg);
}

char * utils_get_arg(const char * key, int argc, char **argv) {
    int len = strlen(key);
    for (int i =0; i < argc; i ++) {
        if (strncmp(argv[i], key, len) == 0) {
            return (argv[i] + len + 1);
        }
    }

    return NULL;
}

void utils_log_to_file(FILE *fp, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vfprintf(fp, fmt, args);
    va_end(args);

	fflush(fp);  // ensure log is flushed to disk
}

FILE *utils_log_fp(void) {
    return fp;
}

int utils_init(int argc, char** argv) {
	/* The frontend passes the log level as a Python logging number. */
	const char *log_level = utils_get_arg("log_level", argc, argv);
	if (log_level) {
		fastdyn_log_set_level((int)strtol(log_level, NULL, 10));
	}
	fp = fopen("debug.log", "a");
	if (!fp) return -1;
	return 0;
}

void utils_parse_ranges(int range_count, const AddrRange *overall_ranges, Range *ranges) {
    if (!ranges) return;
    if (range_count <= 0 || !overall_ranges) return;

    for (int i = 0; i < range_count; i++) {
        uint64_t start = overall_ranges[i].start;
        uint64_t end   = overall_ranges[i].end;

        // Optional safety: normalize/validate
        if (end < start) {
            // You can choose to swap, skip, or hard-fail.
            // Swapping is often safer than silently zeroing.
            uint64_t tmp = start;
            start = end;
            end = tmp;
        }

        ranges[i].start = start;
        ranges[i].end   = end;
    }
}

int* utils_parse_interrupt_ranges(const char *s, int *int_nums){
    int capacity = 10;      //initially allocate only 10 interrupts
    int *int_lst = malloc(capacity * sizeof(int));
    int count = 0;

    //Supported format -> 0-10:15:20-30:40
    char *str = strdup(s);
    char *tok = strtok(str, ":"); //get the first irq/irq_range
    if (!int_lst) return NULL;

    while (tok != NULL) {
        char *dash = strchr(tok, '-');
        int low_val, high_val;
        if (dash != NULL) { //range case
            *dash = '\0';
            low_val = atoi(tok);
            high_val = atoi(dash + 1);
        } else { // single value
            low_val = high_val = atoi(tok);
        }

        for (int i = low_val; i <= high_val; i++) {
            if (count >= capacity) {
                capacity *= 2;
                int *tmp = realloc(int_lst, capacity * sizeof(int));
                if (!tmp) { free(int_lst); free(str); return NULL; }
                int_lst = tmp;
            }
            int_lst[count++] = i;
        }
        tok = strtok(NULL, ":"); // get the next irq/irq_range
    }

    free(str);
    *int_nums = count;
    return int_lst;
}
