# Retire low-reuse ordinary exits

The `ordinary-exit` pipeline applies `retire-ordinary-exits@1` to a v5 x86-64 warm cache. It leaves the original TB addresses and guest recovery map in place. Selected direct exits keep their guest-state updates and return through QEMU's common null-TB epilogue. QEMU resolves the same guest continuation through ordinary dispatch and skips the optional native link install for that slot.

The pass currently supports QEMU build `sha256:2da1d94f82da8f182282877b47959507b1282e11469e7b418fdf7913e99f28b6`. The captured RUN_INFO must match the runtime's prologue digest, base and signature. V12 checks the adjacent EAX-zeroing epilogue and validates the exact old and new native exits, v5 record and SCM changes. V8 independently reimports the written cache with its updated capture. A second QEMU build would need review of its tagged/null return ABI, optional `tb_add_jump` path, standard x86 `GOTO_TB` shape and captured epilogue. The EnvLayout describes CPU state fields and supplies no proof of that dispatcher contract.

The pass takes an exact, complete, chain-disabled steady profile and a `scopt.ordinary_exit_training/1` manifest with `input_snapshot_id`, `qemu_sha256`, `profile_id`, `trace`, `trace_sha256` and `steady_lines`. `trace` is an ordinary `-d exec` log from the same baseline runtime and workload. The manifest identifies the log by SHA-256 and uses a one-based inclusive line range covering the training benchmark. Each selected link must have an observed installation to the one exact profiled successor. The integer `max_transfers` defaults to 1. Use independent timing to qualify a wider cohort.

```json
{"retire-ordinary-exits":{"training_manifest":"train.json","max_transfers":16}}
```

```sh
scopt optimize WARM_SNAPSHOT --pipeline ordinary-exit --profile PROFILE \
  --params params.json --store STORE --json
scopt validate ARTIFACT --snapshot WARM_SNAPSHOT
scopt validate ARTIFACT --dynamic --config CONFIG --baseline WARM_SNAPSHOT \
  --qemu QEMU --inputs EVAL_BANK --out VALIDATION_DIR
```

Use the public `scopt select-warm` command with fresh cases to confirm a gain over ordinary warm before deployment. Performance depends on the reuse distribution and the runtime cost of modifying native code pages.
