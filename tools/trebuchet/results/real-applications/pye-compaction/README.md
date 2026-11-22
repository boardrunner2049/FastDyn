# Variable-length host-code compaction on Pye

Both independent original and FirmBOLT training profiles selected guest PC `0xe40`. Its ordinary x86-64 translation contains a three-byte NOP immediately before the mutable jump for slot 0. The original profile bounds executions of this physical NOP at 279,567–279,571. The top two original guest TBs by entries were ineligible for this specific rewrite. [Source-site census](../pye-host-sites/README.md) · [Original generation](original.json) · [FirmBOLT generation](firmbolt.json)

The emitter shortened a six-byte conditional branch to its equivalent two-byte form and used the longer, equivalent displacement encoding for the preceding guest-PC store. That encoding consumed the three bytes formerly occupied by the NOP. The resulting code contains one fewer instruction and occupies 124 bytes, down from 128. The mutable jump displacement moved from +100 to +96 and remains four-byte aligned. Search padding absorbed the four saved code bytes, leaving the record’s allocated extent unchanged.

The emitter reconstructs relative branches, RIP references, both slot fields, guest instruction intervals, QEMU search rows, operation marks, code hashes and derived capture files. It independently decodes the result and compares instruction operands, targets and unwind state at every surviving instruction boundary. Both input and output undergo the importer’s per-unit checks. The selected unit has two guest instruction rows and zero exceptional recovery points. The bounded rule admits ordinary pool-free direct exits with classified references and declines exceptional slow paths, auxiliary capture streams, unsupported branch targets and entries into changed instruction interiors.

Run from the FastDyn root with a real original v5 snapshot and its matched merged training profile. The output directory must be fresh.

```sh
scopt-env/bin/python -m scopt compact-nops "$snapshot" --profile "$profile" --out "$candidate"
scopt-env/bin/python -m pytest -q tools/scopt/tests/unit/test_nop_compaction.py
```

The receipts describe an offline re-emission from actual archived cache bytes. They establish physical savings and search/patch correctness for this unit. A fresh capture then reproduced the same physical rewrite on the qualified integrated runtime. All 12 compacted training observations and their 12 ordinary matched controls passed the full document oracle. Median edit latency was 17.015 ms for ordinary and 17.096 ms for compacted. The median of paired gains was 1.013× with an exploratory bootstrap interval of 0.934–1.257×. This single-site change has no resolved training speedup. [Resolved trial receipts](../pye-incremental/integrated/verified.json) · [Matched timing](../pye-incremental/integrated/summary.json) [Raw input TB and capture](source-unit.json) · [Emitter](../../../../scopt/scopt/compact_nops.py)
