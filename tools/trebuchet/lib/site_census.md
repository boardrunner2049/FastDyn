# Physical call and encoding census

Run once for each admitted application, scenario, phase and image. The scanner operates on existing files. The snapshot importer rechecks the stored cache and captures, the guest ELF must match the bundle digest, and the optional profile must match the generation and reach the named phase. Application and scenario are experiment labels supplied by the caller. Keep their input manifests and run IDs alongside the output.

```sh
scopt-env/bin/python tools/trebuchet/lib/site_census.py \
  --snapshot "$SNAPSHOT" --elf "$IMAGE" --application "$PRODUCT" \
  --scenario "$CASE" --phase steady --profile "$PROFILE" --out census.json
```

For an exported ordinary v5 snapshot whose original host paths are unavailable, add `--archived-ordinary`. This offline mode reimports cache and SCM bytes, checks the original snapshot, captures, bundle and runtime identities, then compares the guest origin, native and search byte hashes, partitions, instruction/event map, entries, exits and recovery against every stored region. The report counts differing host annotations. Relocation and native-body eligibility use only evidence available on the analysis host. The ordinary archive mode is limited to physical diagnostics. Artifact validation uses full store reimport.

For archived QEMU function costs, supply `--archived-qemu` with the matching executable. The scanner checks the runtime SHA-256, captured file identity and mapping, the helper name, flags and exact entry address, and the unique function range. Static byte and disassembly counts in `cost_proxy.archived_function` describe those immutable ELF bytes. Successful lookup binding for offline costs does not produce an inline certificate.

QEMU’s P3 profiler fires within `helper_lookup_tb_ptr`. The census reports the number of exact unflagged, in-domain lookup-hit and lookup-miss transitions. Their sum is a lower bound on actual helper invocations independent of the enclosing TB proxy.

The report separates physical calls from capture-only references. Native bindings carry the resolved body identity or a refusal reason. The setup proxy counts instructions between the captured CALL opmark and the call. The result proxy recognizes a MOV from the integer return register immediately after the call. Function bytes and linear instruction counts describe the bound symbol's static footprint, including possible cold blocks. Certified fragments report the growth calculated by their recorded splice or island representation. Unmaterialized calls have unknown splice growth. These proxies do not measure helper latency or retired instructions.

Profile v1 observes unchained dispatch. An exact TB entry count bounds each ordinary site where the complete host CFG rules out repeat visits within a dispatch. Sites potentially in a cycle, unclassified units, grown native or guard units, and private copies receive unknown weight without site evidence. Bounds describe the profiled execution. The reported replay counts describe the separate replay. Keep each subtotal within its own evidence class.

For direct site measurements, pass a sparse histogram produced by a separately instrumented **untimed** execution of these exact bytes using `--site-counts hits.json`. This tool consumes observations and does not install probes or run the application. For an initial static inventory of physical offsets and bytes, omit both count inputs. A replay without a compatible dispatch profile may omit `--profile` and use a null profile ID in its identity. Specify the complete identity object documented below. Use UnitId for ordinary code and the reimported variant key for a private copy. Every listed (body, offset, bytes) must identify a decoded census site. Missing sites have no observed count. A recorded zero must come from an active counter. The replay producer is responsible for counter completeness, phase scoping, module attribution and an accurate experiment identity.

```json
{
  "schema": "trebuchet.untimed-site-counts/1",
  "identity": {
    "application": "product", "scenario": "scenario", "phase": "steady",
    "snapshot_id": "sha256:...", "image_hash": "sha256:...", "profile_id": null
  },
  "sites": [{"body": "unit-or-variant-id", "offset": 41, "bytes": "6690", "executions": 7}]
}
```

NOP rows cover disassembled body, native island and guard spans, excluding pool data. Invalid metadata or ambiguous instruction boundaries appear under `uninspected_bodies`. Private bodies must match the SCM variant witness and the relocated general body with its patches. NOP totals include static bytes, observed instruction and byte executions, upper bounds and unweighted site counts, partitioned by ordinary or specialized body. A dynamic byte here counts the length of a visited instruction. It does not measure I-cache traffic or identify which transformation originally emitted the NOP.

Pass an optimizer `--params` JSON file containing `{"constant-propagation":{"record_encoding_sites":true},"branch-folding":{"record_encoding_sites":true}}` on a diagnostic run and save its `--json` output. Supply that file as `--optimizer-result` alongside a snapshot imported from its emitted artifact. The scanner verifies the artifact manifest and output snapshot ID, then joins the predecessor, slot and general unit to a verified private body. Only unchanged instruction bytes can receive an execution weight. Unmaterialized or subsequently changed sites retain their refusal record. Each stage reports whether site recording was enabled. The two B3 passes currently emitting the named encoding refusal operate on specialized copies. Ordinary-body NOPs are scanned independently. No site-observation result can establish the benefit of compacting or splicing a candidate without a matched performance experiment.

For a phase spanning dispatch zero through the end of every included replay, the scanner also joins exact, unflagged chain0/chain1 transitions to the unique physical GOTO_TB exit for that slot. A contiguous NOP suffix with no alternate path into its jump must execute on every such recorded transfer. The `exit_evidence` interval includes this observed minimum and bounds the maximum by excluding recorded traversals of the opposite slot. These bounds overlap the general TB-entry upper bounds. Flagged, transient, profile-only, and missing endpoint events contribute no positive count to the minimum. Destination-phase attribution prevents this inference for an interior phase. Physical NOP padding immediately before a patched GOTO_TB often aligns its rel32 field for atomic updates. Removing it requires maintaining that alignment.
