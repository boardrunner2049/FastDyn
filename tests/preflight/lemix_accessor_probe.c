#define _GNU_SOURCE
#include <inttypes.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/ucontext.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

typedef uint64_t (*accessor_fn)(void *);

extern uint64_t lemix_accessor_original(void *);
extern uint64_t lemix_accessor_fop(void *);
extern uint64_t lemix_accessor_frs(void *);
extern uint64_t lemix_accessor_short(void *);
extern uint64_t lemix_alias_call(accessor_fn, uint64_t);
extern uint64_t lemix_alias_pointer;
extern uint64_t lemix_alias_observed;

struct fault_record {
  uintptr_t address;
  uintptr_t pc;
};

static int fault_fd = -1;

static void fault_handler(int signal_number, siginfo_t *info, void *context) {
  (void)signal_number;
  ucontext_t *uc = context;
  struct fault_record record = {
      .address = (uintptr_t)info->si_addr,
      .pc = (uintptr_t)uc->uc_mcontext.gregs[REG_RIP],
  };
  if (fault_fd >= 0)
    (void)write(fault_fd, &record, sizeof(record));
  _exit(128 + SIGSEGV);
}

static accessor_fn select_function(const char *name) {
  if (!strcmp(name, "original"))
    return lemix_accessor_original;
  if (!strcmp(name, "fop"))
    return lemix_accessor_fop;
  if (!strcmp(name, "frs"))
    return lemix_accessor_frs;
  if (!strcmp(name, "short"))
    return lemix_accessor_short;
  return NULL;
}

static struct fault_record fault_probe(accessor_fn function, int *status_out) {
  int descriptors[2];
  if (pipe(descriptors)) {
    perror("pipe");
    exit(2);
  }
  pid_t child = fork();
  if (child < 0) {
    perror("fork");
    exit(2);
  }
  if (!child) {
    close(descriptors[0]);
    fault_fd = descriptors[1];
    struct sigaction action = {0};
    action.sa_sigaction = fault_handler;
    action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    sigaction(SIGSEGV, &action, NULL);
    (void)function((void *)1);
    _exit(0);
  }
  close(descriptors[1]);
  struct fault_record record = {0};
  ssize_t count = read(descriptors[0], &record, sizeof(record));
  close(descriptors[0]);
  int status = 0;
  waitpid(child, &status, 0);
  *status_out = status;
  if (count != (ssize_t)sizeof(record)) {
    record.address = UINTPTR_MAX;
    record.pc = UINTPTR_MAX;
  }
  return record;
}

static int run_check(void) {
  uint64_t pointee = UINT64_C(0x123456789abcdef0);
  uint64_t ordinary_original = lemix_accessor_original(&pointee);
  uint64_t ordinary_fop = lemix_accessor_fop(&pointee);
  uint64_t ordinary_short = lemix_accessor_short(&pointee);
  uint64_t ordinary_frs = lemix_accessor_frs(&pointee);

  uint64_t alias_original = lemix_alias_call(lemix_accessor_original, 0x1234);
  uint64_t original_pointer = lemix_alias_pointer;
  uint64_t original_observed = lemix_alias_observed;
  uint64_t alias_fop = lemix_alias_call(lemix_accessor_fop, 0x1234);
  uint64_t fop_pointer = lemix_alias_pointer;
  uint64_t fop_observed = lemix_alias_observed;
  uint64_t alias_short = lemix_alias_call(lemix_accessor_short, 0x1234);
  uint64_t short_pointer = lemix_alias_pointer;
  uint64_t short_observed = lemix_alias_observed;
  uint64_t alias_frs = lemix_alias_call(lemix_accessor_frs, 0x1234);
  uint64_t frs_pointer = lemix_alias_pointer;
  uint64_t frs_observed = lemix_alias_observed;

  int original_status, fop_status, short_status;
  struct fault_record original_fault =
      fault_probe(lemix_accessor_original, &original_status);
  struct fault_record fop_fault = fault_probe(lemix_accessor_fop, &fop_status);
  struct fault_record short_fault =
      fault_probe(lemix_accessor_short, &short_status);
  int frs_status;
  struct fault_record frs_fault = fault_probe(lemix_accessor_frs, &frs_status);

  int ok = ordinary_original == pointee + 8 && ordinary_fop == ordinary_original &&
           ordinary_short == ordinary_original && ordinary_frs == ordinary_original &&
           alias_original == original_pointer + 8 &&
           original_observed == original_pointer &&
           alias_fop == fop_pointer + 8 && fop_observed == fop_pointer &&
           alias_short == UINT64_C(0x123c) &&
           short_observed == UINT64_C(0x1234) &&
           alias_frs == UINT64_C(0x123c) && frs_observed == UINT64_C(0x1234) &&
           original_fault.address == 1 && fop_fault.address == 1 &&
           short_fault.address == 1 && frs_fault.address == 1;

  printf("{\"ordinary\":{\"original\":%" PRIu64
         ",\"fop\":%" PRIu64 ",\"short\":%" PRIu64
         ",\"frs\":%" PRIu64 "},\"alias\":{\"original_return\":%" PRIu64
         ",\"original_pointer\":%" PRIu64
         ",\"original_observed\":%" PRIu64
         ",\"fop_return\":%" PRIu64 ",\"fop_pointer\":%" PRIu64
         ",\"fop_observed\":%" PRIu64
         ",\"short_return\":%" PRIu64
         ",\"short_pointer\":%" PRIu64
         ",\"short_observed\":%" PRIu64
         ",\"frs_return\":%" PRIu64 ",\"frs_pointer\":%" PRIu64
         ",\"frs_observed\":%" PRIu64
         "},\"faults\":{\"original\":{\"address\":%" PRIuPTR
         ",\"pc_offset\":%" PRIuPTR "},\"fop\":{\"address\":%" PRIuPTR
         ",\"pc_offset\":%" PRIuPTR "},\"short\":{\"address\":%" PRIuPTR
         ",\"pc_offset\":%" PRIuPTR "},\"frs\":{\"address\":%" PRIuPTR
         ",\"pc_offset\":%" PRIuPTR "}},\"ok\":%s}\n",
         ordinary_original, ordinary_fop, ordinary_short, ordinary_frs, alias_original,
         original_pointer, original_observed, alias_fop, fop_pointer,
         fop_observed, alias_short, short_pointer, short_observed,
         alias_frs, frs_pointer, frs_observed,
         original_fault.address,
         original_fault.pc - (uintptr_t)lemix_accessor_original,
         fop_fault.address, fop_fault.pc - (uintptr_t)lemix_accessor_fop,
         short_fault.address, short_fault.pc - (uintptr_t)lemix_accessor_short,
         frs_fault.address, frs_fault.pc - (uintptr_t)lemix_accessor_frs,
         ok ? "true" : "false");
  return ok ? 0 : 1;
}

static int run_benchmark(const char *name, uint64_t iterations) {
  accessor_fn function = select_function(name);
  if (!function || !iterations)
    return 2;
  uint64_t pointee = UINT64_C(0x123456789abcdef0);
  uint64_t checksum = 0;
  struct timespec before, after;
  clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &before);
  for (uint64_t i = 0; i < iterations; ++i)
    checksum += function(&pointee);
  clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &after);
  double cpu = (double)(after.tv_sec - before.tv_sec) +
               (double)(after.tv_nsec - before.tv_nsec) / 1e9;
  printf("{\"variant\":\"%s\",\"iterations\":%" PRIu64
         ",\"cpu_seconds\":%.9f,\"checksum\":%" PRIu64 "}\n",
         name, iterations, cpu, checksum);
  return 0;
}

int main(int argc, char **argv) {
  if (argc == 2 && !strcmp(argv[1], "check"))
    return run_check();
  if (argc == 4 && !strcmp(argv[1], "bench"))
    return run_benchmark(argv[2], strtoull(argv[3], NULL, 0));
  fprintf(stderr, "usage: %s check | bench {original|fop|frs|short} ITERATIONS\n",
          argv[0]);
  return 2;
}
