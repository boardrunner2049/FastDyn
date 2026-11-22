# PX4 Trebuchet milestone results

The valid guest-side result is negative. FirmBOLT reordered and relocated only
the mission-hot `memset`; fresh bridge metadata was generated and all 12 scored
missions passed. Six randomized paired blocks gave a median speedup of **0.973x**
(candidate slower), with a bootstrap 95% interval of **0.958-1.017x**. Baseline
and candidate medians were 30.384 s and 31.125 s. There is no break-even horizon,
so this scoped policy should remain disabled.

The full FirmBOLT policy is not a timing result. A bounded fix removed a real
64-round CBZ/CBNZ convergence defect, after which publication found four distinct
PC-relative address violations. The proposed hot-text region also exceeded the
physical flash aperture. The exclusion-list variant was stopped as invalid.

The host setup produced valid separate captures/imports for the original and
rewritten guests (47,005 and 48,137 records), but warm profiling appended 7,280
and 7,537 late translations. Neither cache is sealed. Both requested `b2-layout`
artifacts were also identities: receipts reported `missing_profile` or
`missing_hotness`, no unit moved, and input/output SnapshotIds were identical.
V1-V9 passed for those identity outputs, but no host optimization executed. A
targeted original-guest retry explicitly requested `phase=boot`; it remained an
identity because the `hotcold-partition` and `cluster-hot` admission descriptors
hardcode `steady` before their implementations can read the documented phase
parameter. The matching combined retry was stopped once this shared defect was
confirmed. Host-only and combined performance are therefore unavailable, not
negative.

Recommended disposition:

- Disable the scoped `memset` layout policy.
- Retain the bounded CBZ/CBNZ fix and its 80-site regression.
- Keep the PC-relative publication gates and reject the full artifact.
- Extend capture through late warm translations, prove a zero-append replay,
  fix configurable-phase admission, and require nonempty affected units before
  any host timing.

Machine-readable evidence is in `guest-experiment-manifest.json`,
`host-experiment-manifest.json`, `guest-failure-record.json`, and
`opportunity-ledger.json`. Raw data remains under `/data/trebuchet-clean/work`.
