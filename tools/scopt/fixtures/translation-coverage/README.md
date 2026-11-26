# Cortex-M3 translation coverage fixture

Run from the FastDyn checkout after building the pinned ARM QEMU and FastDyn
plugin. The bootstrap copies the existing QEMU build, recompiles the dispatch
hook, and produces a dedicated EnvLayout and training profile.

```sh
scopt-env/bin/python tools/scopt/scripts/bootstrap_translation_coverage.py \
  --work-dir /path/to/fresh/coverage-work
```

The no-probe control translates five TBs. At the fixture's reset entry, the
probe calls QEMU's regular TCG translator for every halfword in a bounded ROM
range. It covers the reset translation context with both ordinary and one-
instruction count flags. The fixture contains a 32-bit MOVW, an independently
decodable second halfword, an unresolved BX, and a UDF terminal. The bootstrap
checks the captured keys and runs a separate zero-input oracle through the
untrained branch. A calibration input raises PendSV and captures its handler
in the actual exception context. Training excludes that input. The identity
artifact and baseline both reuse the cache with zero appends for the cold
branch and the exception oracle.

The work directory contains the raw translator trace, captures, checked
`translation_coverage.json`, and a `fixture.json` index. The coverage record
binds the existing SnapshotId, ExecutionBundle and RuntimeIdentity, including
the image, patch plan, memory map, runtime binaries and exact SCM files.
It records each admitted key, reset and exception vector roots, and the conservative policy for
entries outside the domain, other translator contexts, unresolved transfers
and mutable code. The ROM check reads the running QEMU memory map during the
probe. The record preserves general entry semantics for all compiled TBs.
The emitted `reachability.json` records the actual program-graph result.
It retains all 17 fixture TBs, including eight covered entries and the
PendSV handler with zero training entries. External roots and four unresolved
indirect edges justify keeping every general entry, with zero removals and
zero privatizations.

For global specialization, set `coverage_mode` and `coverage_evidence` in the
`specialize-edges` params. `complete` requires a checked producer record.
`partial` always uses conservative general entries and available graph
information. `auto` uses a verified record when supplied and gives a reason
when it selects partial. The analysis retains unresolved successors.

```json
{"specialize-edges":{"coverage_mode":"complete","coverage_evidence":"/path/to/fresh/coverage-work/translation_coverage.json"}}
```

The same probe can cover the dormant ROM domain in the global specialization
fixture. Use the separate QEMU with both patches, then run the two-mode
bootstrap and the live revocation regression.

```sh
PYTHONPATH=tools/scopt/scripts scopt-env/bin/python -c '
from pathlib import Path
from bootstrap_translation_coverage import build_probe_qemu
print(build_probe_qemu(Path("third_party/common/qemu").resolve(),
                      Path("/path/to/fresh/coverage-qemu"), revocation=True))
'
scopt-env/bin/python tools/scopt/scripts/bootstrap_specialization_coverage.py \
  --work-dir /path/to/fresh/global-coverage \
  --qemu /path/to/fresh/coverage-qemu/build/qemu-system-arm
scopt-env/bin/python tools/scopt/scripts/check_coverage_revocation.py \
  --qualification /path/to/fresh/global-coverage/specialization-coverage.json \
  --out /path/to/fresh/revocation
```

On emulated x86-64 hosts, use the repository's QEMU hostsym oracle to generate
the V18 witness and pass it to `bootstrap_specialization_coverage.py --selftest`.

The revocation regression binds an optional dispatch guard to the loaded semantic
extension checksum and runtime signature, and exercises it at one compiled
fixture entry. It injects an unexpected count-limited key after a real optimized
link has armed. The loader checks synchronous unlinking, persistent no-rearm
state, and guest output after baseline reentry. A changed extension checksum
is refused during startup. The verified run armed one incoming link, unlinked
it and both internal slots, and appended one baseline TB for the new key.
The optional guard exercises the existing semantic-slot disarm path. Closure-dependent
entry removal remains disabled pending mandatory runtime binding and checks for
direct JIT transfers.

## Paired timings

Use the two-mode qualification directory and the global specialization
fixture's independent oracle. The timing harness takes the same lock used by
other fixed-work measurements and randomizes arm order within each block.

```sh
scopt-env/bin/python tools/scopt/scripts/measure_program.py \
  /path/to/fresh/global-coverage /path/to/fresh/global-timings \
  --oracle tools/scopt/fixtures/global-specialization/inputs.json --pairs 5
```

`runs.jsonl` preserves the individual runs. `ratios.json` reports paired
speed ratios, median absolute deviations, and 95% bootstrap intervals.
`mechanism.json` checks the emitted cache bytes and v6 tables. The measured
partial and complete artifacts had identical cache bytes. The specialized
input regressed against warm baseline, and the general-path input stayed
within measurement uncertainty.
