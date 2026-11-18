# Exhaustive host inlining

`host-inline-extreme@1` tries the checked fall-through splice at every captured
host call. A profile influences order. Cold and unprofiled sites are still
considered. The pass independently scans native CALL instructions for missing
coverage and uses the same callee-binding, ABI, relocation, and proof checks
as [host-inline-splice](host-inline-splice.md). It also has a framed splice for
the audited `benchmark_start` build.

The default is strict. The pass stages proposals, calculates coverage, and
publishes only when it can account for and splice every site. A refusal leaves
the baseline and WorkingSet intact. The rejection record has the site and
reason for every decision, including safe proposals that were withheld. An
explicit `allow_partial` setting publishes only the proved edits and marks
coverage as partial in the pass provenance. Use the `identity` pipeline for
a no-optimization baseline. Disabling the extreme pass refuses publication.

```sh
scopt optimize "$SNAP" --pipeline host-inline-extreme \
  --profile "$W/profile/merged.profile.json" --store "$W/extreme-strict" --json

printf '%s\n' '{"host-inline-extreme":{"allow_partial":true}}' > "$W/extreme-partial.json"
scopt optimize "$SNAP" --pipeline host-inline-extreme \
  --profile "$W/profile/merged.profile.json" --params "$W/extreme-partial.json" \
  --store "$W/extreme-partial"
```

Compare an existing artifact against the original capture with a
fresh static check and reimport.

```sh
scopt host-calls "$SNAP" --coverage --artifact "$SPLICE_ARTIFACT" \
  --profile "$W/profile/merged.profile.json" --out "$W/splice-coverage.json"
```

The report separates the count of captured static sites from the profile’s
TB-entry-weighted proxy. One TB entry can execute zero or multiple calls.

## Framed splice and depth

The prototype replaces the translation block's direct call to
`benchmark_start` with the body of that virtual. It leaves the caller's
argument setup in place. A flag-neutral `lea` reserves the eight-byte stack
slot that the removed x86-64 `call` would have pushed. The copied prologue,
spills, body, and `leave` run with the original stack layout. A second
`lea` restores the stack pointer and execution falls through to the next
translation block instruction. The compiler's PC-relative references point
to the same shared state and retained PLT targets after relocation.

`max_inline_depth` is an integer. The default of `1` expands one native
callee at an original capture site. Set it to `2` to also expand an audited
direct call inside that body, when the exact plugin build supports it.
The provenance records the depth reached. The original fixture's clock
call remains at depth two because its PLT target depends on the system
loader and libc or vDSO implementation.

```sh
printf '%s\n' '{"host-inline-extreme":{"allow_partial":true,"max_inline_depth":2}}' \
  > "$W/extreme-depth2.json"
scopt optimize "$SNAP" --pipeline host-inline-extreme \
  --profile "$W/profile/merged.profile.json" --params "$W/extreme-depth2.json" \
  --store "$W/extreme-depth2"
```

The exact-image proof assumes normal SysV returns from the retained calls,
with no observation of the removed return addresses. Its test runtime
uses ordinary dynamic linking, with no interposition, PLT audit hooks,
asynchronous unwinding, or stack inspection. The capture's module
mappings alone do not identify the libc or vDSO body behind a PLT slot.

### Two-level canary

Run this in the source-locked, already-built x86-64 FastDyn checkout.
The script builds a separate `libfastdyn.so` from a copy of the virtual
source, then captures a fresh baseline, identities, and training profile.

```sh
W=/opt/host-inline-depth2
scopt-env/bin/python tools/scopt/scripts/bootstrap_depth2_canary.py --work-dir "$W"
export SCOPT_ENVLAYOUT_PATH="$W/envlayouts"
SNAP=$(find "$W/store/snapshots" -mindepth 1 -maxdepth 1 -type d -print -quit)
```

Apply the depth setting above to this snapshot and its profile, with
`SCOPT_HOST_INLINE_BRIDGE` pointing to the same audited bridge executable
used for the original fixture. The original virtual delegates its three
benchmark operations to a private direct worker in this canary. At depth
one the emitted code calls that worker. At depth two both frames are
stitched into the same translation block and the sole call in the copied
span goes to `clock_gettime@plt`. Disassembly of the published bytes
confirmed both targets. Both canary artifacts passed V1–V8, V24, and V10 on
the two held-out inputs with zero cache appends. The callback runs once
at the start of the timed interval, so this trial validates the splice.

## Six captured sites in the original fixture

| Site | Count | Current proof |
| --- | ---: | --- |
| QEMU `helper_rbit` | 2 | Approved register-only fall-through splices. |
| FastDyn `benchmark_start` | 1 | Approved framed fall-through splice. Keeps the existing `clock_gettime@plt` call. |
| FastDyn `printreg` | 1 | Stack frame and nested `strtoul`, QEMU register-read, and `printf` calls. |
| FastDyn `benchmark_end` | 1 | Stack and TLS, nested time and output calls, and a conditional `exit` path. |
| QEMU `helper_ldul_mmu` slow path | 1 | Nested MMU wrapper and explicit guest return address used for fault, device, and watchpoint recovery. |

The strict trial rejects this fixture at 2 approved of 6 observed. Partial
mode splices `benchmark_start` and the hot `helper_rbit`. The cold RBIT
shares the benchmark virtual's TB. The current format supports one
splice per TB. V1–V8 and V24 passed. V10 produced the same guest output as
the baseline on two held-out inputs and four randomized inputs, with zero
cache appends throughout. Coverage reports the original four surviving
calls and the retained nested clock call separately.

Further coverage needs a closed audit for the other virtuals and an MMU
proof that preserves its explicit guest return address and behavior under
fault, MMIO, and watchpoints. Multiple original call sites in one TB need a
combined offset map and matching metadata.
