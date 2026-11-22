# Physical call and encoding census

Run once for each admitted application, scenario, phase and image. The scanner operates on existing files. The snapshot importer rechecks the stored cache and captures, the guest ELF must match the bundle digest, and the optional profile must match the generation and reach the named phase. Application and scenario are experiment labels supplied by the caller. Keep their input manifests and run IDs alongside the output.

```sh
scopt-env/bin/python tools/trebuchet/lib/site_census.py \
  --snapshot "$SNAPSHOT" --elf "$IMAGE" --application "$PRODUCT" \
  --scenario "$CASE" --phase steady --profile "$PROFILE" --out census.json
```

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
