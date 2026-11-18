# Contracts frozen by WP0

`docs/design/B1-design.md` is authoritative. This file records how WP0 pinned down
the points the design leaves open, and the places where the design text
disagreed with the source. The Python modules named below are the executable
form of these rules. Changing any of them is a contract change: it needs a
design-doc edit and a review from every dependent WP.

| Contract | Where it lives |
|---|---|
| canonical JSON, sha256 ids, FNV, record-checksum fold | `scopt/canon.py` |
| TBKey, tb_uid, UnitId, SnapshotId, content ids (§4.1) | `scopt/ids.py` |
| enumerations (§2.4, §3.5) | `scopt/formats/enums.py` |
| `.sc` v5, `.scm` v1, `.qprof` v1 byte layouts (§2.5, §3.5) | `scopt/formats/layouts.py` |
| CacheRegion v1, Profile v1, ExecutionBundle v1, artifact sidecar JSON schemas | `scopt/formats/schemas/*.json`, `scopt/formats/schemas.py` |
| core unit types (§5.2) | `scopt/model/unit.py` |
| analysis-store types (§5.3) | `scopt/analysis/api.py` |
| pass API (§5.4-5.5) | `scopt/passes/base.py` |
| QEMU base hooks | QEMU commit `2dfce24b2a` (patch `qemu-sc-base`) |
| `.qprof` v1 producer semantics (WP5) | `docs/qprof-producer.md`, QEMU commit `a0681a6ac3` (patch `qemu-sc-profile`) |

## Clarifications and errata

1. **FNV-1a-64 uses QEMU's offset basis, not the standard one.** In
   `tcg/supercache.c`, `SUPERCACHE_FNV_OFFSET` is `1469598103934665603`
   (`0x14650fb0739d0383`). The standard FNV-1a-64 offset basis is
   `14695981039346656037` (`0xcbf29ce484222325`); QEMU's value is missing one
   decimal digit. The prime is the standard one. Every "FNV-1a-64" in the
   design means QEMU's variant: the `.sc` checksums, `guest_hash`, `code_fnv64`,
   the `.scm` header and chunk trailers, and the `.qprof` checksums. The C
   capture and profile code must reuse `SUPERCACHE_FNV_OFFSET`. Published
   FNV-1a test vectors do not apply here; `tests/unit/test_ids.py` pins vectors
   computed with QEMU's constant.
2. **tb_uid width.** `sha256(...)[:16]` means the first 16 *bytes* of the
   digest, which is 32 lowercase hex characters: `tb:` followed by 32 hex.
3. **"No second page".** In the record and in the tb_uid message,
   `page_addr1` is the raw `tb_page_addr_t` value; "none" is
   `0xffffffffffffffff`. In Python, `TBKey.page_addr1 is None` stands for that
   value. In JSON it is `null`.
4. **SnapshotId.** The per-slot code hash is the raw 32-byte sha256 digest,
   not hex. The slot index is not hashed; slot order is implied by the order
   of the records.
5. **JSON value conventions** (all four schemas):
   - every id and content hash is `"sha256:<64 lowercase hex>"`, including
     `code_sha256`, `params_sha256`, `validation_sha256` and input file hashes;
   - addresses, flags, keys, guest hashes and absolute host values are
     lowercase `"0x…"` strings without padding;
   - sizes, counts, indices and unit-relative offsets are JSON integers;
   - arena offsets such as `tb_offset` are `"0x…"` strings;
   - UnitIds match `^tb:[0-9a-f]{32}#(0|[1-9][0-9]*)$`;
   - unknown keys are rejected, except extension keys matching `^x_`, which
     are covered by the content ids like any other key.
6. **`.qprof` TB row size (erratum to §3.5).** The design says the fixed part is
   72 bytes, but the fields it lists add up to 64. The frozen layout keeps 72
   and widens the final `u64 reserved` to `u64 reserved[2]`:
   `u64 tb_offset, first_vaddr, page_addr0, page_addr1, cs_base; u32 flags, cflags; u16 size, icount; u32 pad; u64 reserved[2]`
   (`QPROF_TB_ROW = "<5QIIHHI2Q"`).
7. **`.scm` END payload** is 52 bytes (`6×u64 + u32 clean`). Chunk framing pads
   it to 56.
8. **Profile phase names** are at most 15 bytes, so that the `.qprof`
   `char name[16]` can always be NUL-terminated.
9. **ExecutionBundle v1** has `schema_version: 1`. The `entry_points` keys are
   `elf_entry`, `vtor`, `reset_vector` and `initial_sp`. Hashes use the
   `sha256:` prefix as in item 5.
10. **Additions to the §5.2/§5.4 types:**
    - `TBUnit.pool`, because POOL_REF sites index the pool array;
    - `TBUnit.annotations`;
    - `PassDescriptor.overrides`, the `overrides=("layout",)` that §5.6 refers
      to;
    - typed records `Insn`, `Site`, `PoolEntry`, `Ldst`, `OpMark`,
      `StaticExit`, `RecoveryPoint`, `Bindings`, `Entry`, `Exit` and
      `GuestOrigin`, mirroring the `.scm` arrays and CacheRegion v1;
    - the ProposalBuilder methods as the protocol `ProposalBuilderAPI`; the
      concrete class is `scopt.proposal.ProposalBuilder` (WP9);
    - the §5.3 types in `scopt/analysis/api.py`; WP9's `analysis/store.py`
      implements them.
11. **QEMU base hooks (erratum to §2.1, H1).** §2.1 places the H1 call after
    `supercache.flat = flat;`, but `tcg_supercache_configure` returns early
    when no supercache path is set. A call placed only there could never
    report "`QEMU_SC_CAPTURE` set but no supercache configured". The base
    patch therefore calls `scc_configure_from_env()` and then
    `scp_configure_from_env()` in two places: at the end of the function, and
    on the `!path` early return. The callees tell the two cases apart with
    `tcg_supercache_enabled()`. In both places the calls run before
    `tcg_init()`, so before supercache init registers `atexit(sync_cache)`.
12. **`tcg_supercache_loaded_max_end()`** returns `max_end` as it stood right
    after `load_records()` on a reused cache. It returns 0 for a new cache and
    before init. `tcg_supercache_buffer()` returns NULL before init.
    `tcg_target_tb_ret_addr()` is declared in `include/tcg/supercache.h` and
    returns NULL on non-x86-64 hosts.
