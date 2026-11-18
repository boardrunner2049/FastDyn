# `.qprof` v1 producer semantics (WP5, `qemu-sc-profile.patch`)

`docs/design/B1-design.md` §3 is authoritative. The byte layout is frozen in
`scopt/formats/layouts.py` (QPROF_*; see `docs/contracts.md` item 6 for the
72-byte TB row). This page records what the QEMU side (`accel/tcg/sc-profile.c`)
actually writes, including the points §3 leaves open. WP6 (ingest) should
read the file with these rules.

## Switches

| Variable | Meaning |
|---|---|
| `QEMU_SC_PROFILE=/abs/run.qprof` | turns profile mode on. The path must be absolute and must not exist (`O_EXCL`). |
| `QEMU_SC_PROFILE_PHASES=name@0xVADDR[,...]` | optional. Phase 0 is always `boot` (marker 0). At most 7 markers (8 phases). Names are 1..15 bytes of `[A-Za-z0-9_.-]`, unique, and not `boot`. Markers are `0x`-prefixed hex. |

QEMU exits non-zero at configure time, before any file is created, when:
- `QEMU_SC_CAPTURE` is also set (`... are mutually exclusive`);
- no supercache is configured;
- `supercache-flat=on`;
- the path is relative, or the file already exists;
- the phase spec is malformed, or `QEMU_SC_PROFILE_PHASES` is set without `QEMU_SC_PROFILE`.

On success QEMU prints `supercache-profile: path=... phases=N chaining=off` on stderr.

## File life cycle

- The fd is moved to ≥ 1000 (`F_DUPFD_CLOEXEC`), as for capture.
- A **provisional header** is written at configure time. It has valid magic and checksum, flags 0 and zero counts, and nothing follows it.
- At exit (`atexit`, P7) the body is written from offset 128: phase rows, TB rows, edge rows, then the u64 FNV-1a-64 trailer over the body. The final header is `pwrite`n last.
- A process that dies without running atexit leaves only the provisional header. A reader must reject a file whose size differs from `128 + body + 8`.
- `clean_exit` (flag bit 0) means all of:
  - the atexit writer ran;
  - every write succeeded;
  - only one vCPU dispatched.

  A QEMU shut down by SIGTERM also runs atexit, so `clean_exit` does not prove that the workload ran to completion. The harness's exit-code and output checks decide that.

## Header

- `code_address`: the arena base. Every `tb_offset` is `TB pointer − code_address`.
- `runtime_signature`: the value QEMU computed for this run, which equals the `.sc` header value on a reused cache.
- `loaded_record_count`: the `.sc` header `record_count` as read from disk.
- `loaded_header_checksum`: the `.sc` header `checksum` as read from disk.
- `loaded_max_end`: the max `end_offset` over the valid loaded records.
- For a new (non-reused) cache, all three `loaded_*` fields are 0.
- `total_dispatches`: the number of counted TB entries. It equals Σ entries over all rows and phases, and it equals the number of `-d exec` `Trace` lines of an equivalent nochain run.

## Phase rows

`reached` is a u32 with three values:

| Value | Meaning |
|---|---|
| 0 | not reached |
| 1 | reached, `match = exact_start` (marker == dispatch pc) |
| 2 | reached, `match = contained` (`pc < marker < pc + tb->size`) |

§3.4 asks for `match` to be recorded, but the 40-byte phase row has no other field for it, so it is carried in `reached`.

Other phase-row rules:
- Only the next pending marker is tested, at every dispatch (P1 and P3).
- On a hit, the phase advances before the entry is counted.
- If one TB contains several pending markers, all of them advance on that dispatch. They all get the same `first_dispatch_index`, and the skipped phases have zero entries.
- `first_dispatch_index` is the 0-based index of the dispatch that entered the phase. For `boot` it is 0. A reached phase stays reached; the reached phases always form a prefix of the phase list.

## TB rows

- One row per distinct TB pointer, in first-dispatch order.
- The key fields (`page_addr0/1`, `cs_base`, `flags`, `cflags`, `size`, `icount`) are copied from the TB struct at first dispatch.
- `first_vaddr` is the guest pc of the first dispatch.
- `pad` and `reserved[2]` are 0.
- Per phase:
  - `entries`: P1 main-loop entries plus P3 lookup hits.
  - `lookup_hits`: the P3 subset of `entries`.
  - `aborted`: entries that returned with `TB_EXIT_REQUESTED`.
  - `longjmp`: entries left by a `siglongjmp` while the TB was running.
- One-shot TBs (`page_addr0 == -1`) appear as ordinary rows; ingest classifies them as TRANSIENT.

## Edge rows

- One row per distinct `(src, dst, kind, flags, phase)`, in first-seen order.
- Every counted entry adds exactly one in-edge.
- `phase` is the phase of the **destination** entry. So:
  - in-flow is conserved per `(t, phase)`;
  - out-flow is conserved per `t` only when summed over phases (`Σ out(t) = Σ_p entries(t, p) − [t is the last TB]`), because the edge that crosses a phase marker carries the new phase.

| kind | Produced when the previous entry ended by |
|---|---|
| FIRST (7) | the first entry of the run (`src = UINT64_MAX`); exactly one, count 1 |
| CHAIN0 / CHAIN1 (1/2) | `exit_tb` with index 0/1, i.e. a goto_tb slot that production would chain |
| LOOKUP_HIT (3) | a `lookup_tb_ptr` hit (direct transfer, as in production) |
| LOOKUP_MISS (4) | a `lookup_tb_ptr` miss followed by the epilogue (`ret == 0`) |
| EXIT_NULL (5) | `ret == 0` without a preceding miss (`exit_tb(0)`), **or a longjmp out of the TB** (then flag LONGJMP is also set) |
| EXIT_REQUESTED (6) | `TB_EXIT_REQUESTED` (the entry is also counted in `aborted`) |

Flags are accumulated between the previous TB's exit and the next entry:
- IRQ: `cpu_exec_interrupt` delivered an interrupt.
- EXCEPTION: `do_interrupt` ran from `cpu_handle_exception`.
- LONGJMP: the previous TB was left by a longjmp.

A longjmp that happens outside TB execution, during translation or interrupt delivery, changes no count and sets no flag.

## Capacities and overflow

| Table | Capacity | Flag |
|---|---|---|
| TB rows | 65536 | `tb_overflow` |
| Edge rows | 262144 | `edge_overflow` |

All tables are static `.bss`, about 30 MiB of virtual memory that stays untouched unless profiling runs. On overflow the observation is dropped, the flag is set, and nothing is allocated.

## Not covered

- `cpu_exec_step_atomic` entries are not counted. They are reachable only with parallel TCG; supercache forces `thread=single`.
- Multi-vCPU guests are not profiled: the file is written without `clean_exit`.
