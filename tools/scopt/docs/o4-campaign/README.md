# O4 campaign records

This directory holds the raw records of the scopt O4 campaign (2026-11-25 and 2026-11-26),
copied byte for byte from `/data/scfix-o4` on the machine where the campaign ran. Every copy
was checked with `cmp` against its original, and its sha256 is listed below.

- The story of the campaign, with its numbers and their sources, is
  [`../o4-campaign.md`](../o4-campaign.md).
- The recursion investigation is summarized in [`../o4-recursive.md`](../o4-recursive.md).
  Its critique, [`../o4-recursive-critique.md`](../o4-recursive-critique.md), was written for
  the documentation after the campaign. It is not a record, so it is not in this directory.
- The adopted configuration (O4-r3) and its decision rule are in [`../O4.md`](../O4.md).

Do not edit these files. They are the evidence; corrections belong in the documents above.
Only this README and `.gitattributes` were written here. `.gitattributes` turns off git's
whitespace checks for the copies, because `pipelines/o4-noregions.toml` keeps its original's
trailing blank line and `git diff --check` would otherwise flag it.

## Reading the records

**Paths inside the records refer to the original machine.** `/data/scfix-o4` was the
campaign directory, `/data/fastdyn-work/fd-int` the scopt/FastDyn worktree (this repository,
branch o4), `/data/fastdyn-work/qemu-fu` the QEMU tree and `/data/FirmBOLT/firmbolt` the
FirmBOLT tree. Many cited files (timing directories, plans, receipts, hygiene records) are not
in this archive and exist only on that machine. For files that are here, the original path
maps to this directory as follows. Line-number citations such as `driver/decide.py:350-362`
still hold, because the bytes are unchanged.

| Original path (under `/data/scfix-o4/`) | Here |
|---|---|
| `<X>-SUMMARY.md` | `<X>-SUMMARY.md` |
| `driver/PREREG*.md`, `driver/AMENDMENT-F-most-cases.md`, `driver/DECIDE*.sha256` | `prereg/` |
| `p6/B1-rule.md` | `prereg/B1-rule.md` |
| `driver/<tool>.py` | `tools/<tool>.py.txt` |
| `<w>/VERIFY.md` (w = packet, uart, cli) | `verify/<w>-VERIFY.md` |
| `design/O4-design.md` | `design/O4-design.md` |
| `design/pipelines/*.toml` | `pipelines/` |
| `rec/*.md` | `recursive/` |
| `*.json` at the top level | `data/` |

**The tools are stored as `.py.txt`** so that pytest and the package build never import
them. They are records, not runnable code here: `decide_r3.py` and `best_most.py` import
`decide.py`, and `compare_rtos.py` imports modules from paths on the original machine.

**Registered digests.** `prereg/DECIDE.sha256` and `prereg/DECIDE_R3.sha256` name the files
`decide.py` and `decide_r3.py`. Their archived copies, `tools/decide.py.txt` and
`tools/decide_r3.py.txt`, have exactly those digests (see the table below). The sha256 of each
decide, decide_r3 and best_most output in `data/` also matches the digest (or digest prefix)
that its campaign summary cites. `decisions.json` is not cited by digest in any summary.

## Index

### Campaign summaries (in the order the campaigns ran)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`A-SUMMARY.md`](A-SUMMARY.md) | `/data/scfix-o4/A-SUMMARY.md` | `64be6d78434782dbc10dac30c5cb5740dbcf0a53d788695b35a1b10e56723f90` | Campaign A, the screen against O4-r1 (design §3.4). VALID and BEST hold. |
| [`H-SUMMARY.md`](H-SUMMARY.md) | `/data/scfix-o4/H-SUMMARY.md` | `ae723828ccafd2af0641808b3f315592ece4934ce6c8615b197087c12b39704d` | Campaign H, heavy and bimodal arms (design §3.6). Attempt 1 failed on uart ("allocate supercache code buffer: File exists") and was rerun; in attempt 2, o4_pwhopr (A5) is faster than O4-r1, so BEST fails. |
| [`A2-SUMMARY.md`](A2-SUMMARY.md) | `/data/scfix-o4/A2-SUMMARY.md` | `cfc3e33d16dbab1f2bbc633199b24107895a6cf9ac25a2d661984d60991ed369` | Campaign A2, the head-to-heads, run as one combined plan per workload (design §3.5, PREREG.md change 3) and decided together with H. |
| [`B-SUMMARY.md`](B-SUMMARY.md) | `/data/scfix-o4/B-SUMMARY.md` | `4f76796ef559f627ce8d634f83f0906c72b9bdcb4c109a11da0bf9faf5437e27` | Campaign B: B1 (O4-r2 fails on packet), the O4-r2b retry build, B1-retry (pass), B2 (pass) and H2 (o4_pwhopr faster than O4-r2b). O4 stayed at O4-r1 and the step ended BLOCKED. |
| [`R3-SUMMARY.md`](R3-SUMMARY.md) | `/data/scfix-o4/R3-SUMMARY.md` | `5e3b45dcde5d6a6bc3d31320cdee09598f139c8de81c346110585474f56fbe4f` | Round R3: o4r3a-o4r3d against O4-r1 under PREREG-R3.md. All four pass ADD-confirm; o4r3c has the highest pooled d; the registered tie-break picks o4r3d. |
| [`F-SUMMARY.md`](F-SUMMARY.md) | `/data/scfix-o4/F-SUMMARY.md` | `3952d4c550f4700085b406977ac4e955da5bcdd302a53b864215ddf429569137` | Campaign F, the best-check of every arm against o4r3d. The strict BEST and the amended BEST-most both hold; it also compares o4r3c with o4r3d and with O4-r1. |

### Pre-registrations and rules (`prereg/`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`prereg/PREREG.md`](prereg/PREREG.md) | `/data/scfix-o4/driver/PREREG.md` | `42d19699aecf00c214cdfb4aff68fe7e97e74bc46fd535200ede5af0c0b9c232` | Registration 2 of decide.py, made before any O4 timing: the Student-t reference in place of the normal (with the simulation that motivated it), same-bytes aliases, and combined A2 plans. |
| [`prereg/DECIDE.sha256`](prereg/DECIDE.sha256) | `/data/scfix-o4/driver/DECIDE.sha256` | `9a09b0dabecad8634d7de1bd17bbab4713abbfa1d38cd203f850790b390325db` | The registered sha256 of decide.py (registration 2). It names the file `decide.py`; here that file is `tools/decide.py.txt`. |
| [`prereg/B1-rule.md`](prereg/B1-rule.md) | `/data/scfix-o4/p6/B1-rule.md` | `4c53346fc2326066d40845f9e42728a69a4b81bb33fad5017d789f2a4cc0a7f8` | The B1 rule for O4-r2 (ADD-confirm, because O4-r2 carries the A5 speed claim), settled before any campaign-B timing. |
| [`prereg/PREREG-R3.md`](prereg/PREREG-R3.md) | `/data/scfix-o4/driver/PREREG-R3.md` | `7d63ada3cb2a6d39c42bb536ce3c0368fef0da1b6ae442c69884e6cc37e3a9d4` | The pre-registered rule of round R3 (decide_r3.py), with the digests of the files it relies on. Also used for F. |
| [`prereg/DECIDE_R3.sha256`](prereg/DECIDE_R3.sha256) | `/data/scfix-o4/driver/DECIDE_R3.sha256` | `a0fb7ae744ad9ebeeedc2b4cad75b050629d5b818294e6bc77928ef4578d1810` | The registered sha256 of decide_r3.py. It names the file `decide_r3.py`; here that file is `tools/decide_r3.py.txt`. |
| [`prereg/AMENDMENT-F-most-cases.md`](prereg/AMENDMENT-F-most-cases.md) | `/data/scfix-o4/driver/AMENDMENT-F-most-cases.md` | `01b64b25120e4cbd3cd9df72cf713a83c56abca53f45dfc88812932479a71d5f` | The "wins in most cases" amendment (BEST-most), written at the user's instruction while F was still timing and before any F estimate was read. |

### Builds and gates per workload (`verify/`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`verify/packet-VERIFY.md`](verify/packet-VERIFY.md) | `/data/scfix-o4/packet/VERIFY.md` | `a827133b7766e091810899913bf0ffaec1e9f0ac051cc1db75f5452be31af603` | packet: identity checks, builds (receipts), gates and same-bytes pairs for every arm (P0-P2), plus the later P4b (o4_tasks), P5 (o4r2) and R3 (o4r3b-d) sections. Nothing timed. |
| [`verify/uart-VERIFY.md`](verify/uart-VERIFY.md) | `/data/scfix-o4/uart/VERIFY.md` | `3f4828d408af064f7f76fdffc243dc743c86880c26601e2231cfea3caabedbd0` | uart: the same record (P0 rebuild check, P1 O4-r1 identity, P2 builds, gates, findings, P4b, P5, R3). Nothing timed. |
| [`verify/cli-VERIFY.md`](verify/cli-VERIFY.md) | `/data/scfix-o4/cli/VERIFY.md` | `e2af5b3ef3407c23ed2a20daafdf196abd23b421c62e7fd7db8a4cfa83338aa8` | cli: the same record (rebuild and identity checks, builds, same bytes, gates, problems, P4b, P5, R3). Nothing timed. |

### Design (`design/`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`design/O4-design.md`](design/O4-design.md) | `/data/scfix-o4/design/O4-design.md` | `f30011e8237e4f4325111496bd57a6fd9b266d25161080dbe83b6030f728cddf` | The O4 design, revision 2 after review: §0 summary, §1 definition and candidates (§1.8), §2 code, §3 experiment plan, §4 risks, §5 how each review finding was resolved, Appendix A probe2. |

### Recursion investigation (`recursive/`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`recursive/map-regions.md`](recursive/map-regions.md) | `/data/scfix-o4/rec/map-regions.md` | `df1ebc51a22acd531cca387c7d96ae147f903e605fe526bccd63914d6b942ffe` | Map of firmware region formation and the optimizations inside regions (read-only audit). |
| [`recursive/map-b3.md`](recursive/map-b3.md) | `/data/scfix-o4/rec/map-b3.md` | `2c263a24083fb4e56d6f10479f41a5cc8ce4b2152d4840183fe42182bcaaa35b` | Map of the B3 passes, the B3 manager and the QEMU runtime: what they reach inside regions. |
| [`recursive/map-hostinline.md`](recursive/map-hostinline.md) | `/data/scfix-o4/rec/map-hostinline.md` | `261c51c829afc7c107ba700e7ea9943c8941873e969c7a532c93beb9228d577a` | How recursive host-inline is, and what a recursive version would need. |
| [`recursive/map-others.md`](recursive/map-others.md) | `/data/scfix-o4/rec/map-others.md` | `9ae84cdc57964d78be5d97a8ef8498caed174c390947c949a814da9193f10c3b` | The other scopt passes, the runtime features and the pass manager, read for recursion. |
| [`recursive/map-firmbolt.md`](recursive/map-firmbolt.md) | `/data/scfix-o4/rec/map-firmbolt.md` | `217df5e0ce9ef188f2eb07bcdb8656fd9b4ad9c5f75af7daceb3adcc616f056e` | FirmBOLT -O4's recursion model, mapped onto scopt O4. |
| [`recursive/DESIGN.md`](recursive/DESIGN.md) | `/data/scfix-o4/rec/DESIGN.md` | `e389b7c7eea6b997d1fd3e0599510175085c28ab2d240c4b09e7ccbf4d001985` | O4-rec: the design for applying every optimization recursively, written from the five maps. Design only: nothing was built, run or timed. In the campaign its critique step was stopped before it ran. A critique was written afterwards (2026-11-26) for the documentation: [`../o4-recursive-critique.md`](../o4-recursive-critique.md), outside this archive. |
| [`recursive/no-qemu-routes.md`](recursive/no-qemu-routes.md) | `/data/scfix-o4/rec/no-qemu-routes.md` | `eb2f2c0a1658e8c4004891fe71782c60c3b2fa03ca9a74d5abdda14101e82d3b` | Final version (08:17:57, 44,484 B). Every rule the QEMU loader (aacca05) applies to a region member, and scopt-only routes that put B3, SPEC and host-inline inside region members with QEMU unchanged. Its §5 is an adversarial check of its own routes. Verdict: no QEMU change is needed. |

### Decision outputs (`data/`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`data/screen.json`](data/screen.json) | `/data/scfix-o4/screen.json` | `3cac638cad722d34f6b3fa817ece1e78322ff8afc0b3d5b6050ab3102ad3755a` | decide.py output for campaign A. |
| [`data/H.json`](data/H.json) | `/data/scfix-o4/H.json` | `87c6a38f428a02c75703664d8c770792703bd33c1025fa5da7d02734cedb062f` | decide.py output for campaign H (attempt 2). |
| [`data/A2.json`](data/A2.json) | `/data/scfix-o4/A2.json` | `cf630e285e7b0c21790b13298bdba48fb8d6ad15d085e6cac3218ff62173e24b` | decide.py output for A2 and H decided together. |
| [`data/decisions.json`](data/decisions.json) | `/data/scfix-o4/decisions.json` | `a8ca1a409acc12f443963928b75a0790aa01428876b0dfb0a56f53c63c13960a` | Step P5's per-item decisions and the O4-r2 composition, written by `p5/make_decisions.py` from a registered decide.py run over campaigns A, A2 and H (`p5/P5.decide.json`). |
| [`data/B1.json`](data/B1.json) | `/data/scfix-o4/B1.json` | `8f748cd7d86307717b59ef0d93535521c1eeb7ae185f753e1f7758faa2b4fa19` | decide.py output for B1 (O4-r2 against O4-r1). |
| [`data/B1-retry.json`](data/B1-retry.json) | `/data/scfix-o4/B1-retry.json` | `2d96a217f55cb60a964133f37a7c4eb08cb09bea2de6d8793c091da7853a84ef` | decide.py output for the B1 retry (O4-r2b against O4-r1). |
| [`data/B2.json`](data/B2.json) | `/data/scfix-o4/B2.json` | `f884eeb03985536013c900c37cc5a6340f541913f36c5e4f55e8cacbbe36b011` | decide.py output for B2 (best-check against O4-r2b). |
| [`data/H2.json`](data/H2.json) | `/data/scfix-o4/H2.json` | `32ae3283fe94e0ee78e6aeca8f05a50d3322fc527b874cffc240f0dfa2ad980b` | decide.py output for H2 (campaign H's arms rerun against O4-r2b). |
| [`data/B.json`](data/B.json) | `/data/scfix-o4/B.json` | `42947dbeb15f721b0e91ca456270eeb0cb3d3b60789cce2ff2f08fac747c1f42` | decide.py output for B1-retry and B2 together (`adopt_o4r2`); cited by B-SUMMARY.md. |
| [`data/R3.json`](data/R3.json) | `/data/scfix-o4/R3.json` | `f5449daca3236bf992785ba8b88577504b079decdab893bf3b508847cdc974db` | decide_r3.py output for round R3. |
| [`data/F.json`](data/F.json) | `/data/scfix-o4/F.json` | `38980dce09a4c04ec6666095a496ae75da7d37f39c7a1df80a52e9531a3fc91f` | decide_r3.py output for campaign F. |
| [`data/F-most.json`](data/F-most.json) | `/data/scfix-o4/F-most.json` | `ec477d48d5bfc2a2e2cacc5c9e255026bd2579dc5fb0706917ef91fff32f6262` | best_most.py output: the amendment's BEST-most applied to F.json, next to the strict BEST. |

### Pipelines of every arm (`pipelines/`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`pipelines/O4.toml`](pipelines/O4.toml) | `/data/scfix-o4/design/pipelines/O4.toml` | `106c4ba4a6552b92c6d1baece367c4dd55d03fa43e17a6a7a92ce3f4617ca13f` | The O4 alias as it stood during the campaign (then revision 1). The packaged `../../pipelines/O4.toml` now points at O4-r3. |
| [`pipelines/O4-r1.toml`](pipelines/O4-r1.toml) | `/data/scfix-o4/design/pipelines/O4-r1.toml` | `7a6fd0fb18c8370ebb1b38169486b23d88a402efbc8510c840f866603f100540` | Revision 1, the reference of campaigns A, H, A2, B1, B1-retry and R3. Same bytes as the packaged `../../pipelines/O4-r1.toml`. |
| [`pipelines/o4-chained.toml`](pipelines/o4-chained.toml) | `/data/scfix-o4/design/pipelines/o4-chained.toml` | `16a0d6abc7bd437b9531920c929186b63f51459920d3ff4435de05f6396ea5ae` | Arm A1: firmware-inline residual_goto_tb = "chained". |
| [`pipelines/o4-rank.toml`](pipelines/o4-rank.toml) | `/data/scfix-o4/design/pipelines/o4-rank.toml` | `1ed7d3e6af0c23c3b0931e98162f079eb889a4684a53e5f190daca30b7fb40c8` | Arm A3: chain-static rank_by_net_work = true, c_target_check = 0.0. |
| [`pipelines/o4-hotgraph.toml`](pipelines/o4-hotgraph.toml) | `/data/scfix-o4/design/pipelines/o4-hotgraph.toml` | `d832485c27654c48ccd374c84802d8c45a437e41a33a83617ccccb950f304578` | Arm A4: host-inline hot-loop full-graph lowering, 32768 cap. |
| [`pipelines/o4-pwhopr.toml`](pipelines/o4-pwhopr.toml) | `/data/scfix-o4/design/pipelines/o4-pwhopr.toml` | `f9c44137564ab3818712b0cc8738f04429ef9476fbdc3a6e936786e7b30dc48c` | Arm A5: priced exhaustive firmware-region selection (campaign H challenger). |
| [`pipelines/o4-hostall.toml`](pipelines/o4-hostall.toml) | `/data/scfix-o4/design/pipelines/o4-hostall.toml` | `105957e488a32b858e8e1a0c456b946bf161c1b354f30722ef834ce3a89d226a` | Arm A6: bounded unrestricted host-inline (w7bd's, 32768 B). |
| [`pipelines/o4-min2.toml`](pipelines/o4-min2.toml) | `/data/scfix-o4/design/pipelines/o4-min2.toml` | `66473fe1cf3815e815d1ca1fe6c8df216f77cde47cb855450f2d245327cd9eba` | Arm A7: hot-loop min_call_count = 2. |
| [`pipelines/o4-legacy.toml`](pipelines/o4-legacy.toml) | `/data/scfix-o4/design/pipelines/o4-legacy.toml` | `22561f4a366837698502df393d3af33d6518efd99dfbd1fb4768fa1b28ead4b3` | Arm A8: the legacy planner. |
| [`pipelines/o4-tasks.toml`](pipelines/o4-tasks.toml) | `/data/scfix-o4/design/pipelines/o4-tasks.toml` | `4a413896d71554940093dcd617183300251ef791e7f4b5455b350a2239570c39` | Arm A2a: hot-loop-tasks union selection (deferred in the design; built in P4b). |
| [`pipelines/o4-isb.toml`](pipelines/o4-isb.toml) | `/data/scfix-o4/design/pipelines/o4-isb.toml` | `1480af8afebfa04b909d57db7c5f6a20bf6c16b6a2a2393ac70cb4c44e9378c9` | Arm I1: firmware-inline isb_boundary = true. |
| [`pipelines/o4-nohi.toml`](pipelines/o4-nohi.toml) | `/data/scfix-o4/design/pipelines/o4-nohi.toml` | `ce25dfc7a055f7d0cdea6bc789af659c4b35632e90ed67bbfb87e660ddbe15d8` | Arm X1: host-inline removed. |
| [`pipelines/o4-nocp.toml`](pipelines/o4-nocp.toml) | `/data/scfix-o4/design/pipelines/o4-nocp.toml` | `3a38889420f1dd1e08c257da2e3f476a05854e3de7078d3ec2b81a3e4d59352a` | Arm X2: constant-propagation removed. |
| [`pipelines/o4-noregions.toml`](pipelines/o4-noregions.toml) | `/data/scfix-o4/design/pipelines/o4-noregions.toml` | `e9f9422044f357ab620286f1fab792bdb79b712f547be87c36dcccb0666d3792` | Arm X3: firmware-inline (regions) removed. |
| [`pipelines/o4-nospec.toml`](pipelines/o4-nospec.toml) | `/data/scfix-o4/design/pipelines/o4-nospec.toml` | `1324ef4cc4b402688ecb6022fe98c1f05e5015e22d91abc7813efb377b8473e2` | Arm X4: the B3 SPEC bundle removed (specialize-returns, constant-propagation, branch-folding, dead-host-ops). |
| [`pipelines/o4x-fl3b3.toml`](pipelines/o4x-fl3b3.toml) | `/data/scfix-o4/design/pipelines/o4x-fl3b3.toml` | `ec9353c7347fde9b1ef2c4ce9509eda6b57760a3fe235556f6fb225a5e769e1a` | Challenger C1: FirmLooper v3 regions, unpriced, composed with B3; host-inline removed. |
| [`pipelines/o4x-fl3b3p.toml`](pipelines/o4x-fl3b3p.toml) | `/data/scfix-o4/design/pipelines/o4x-fl3b3p.toml` | `47f9d95825734518cda4bb8dbff09c6fec12aa06edfd5a99f5efbf3bd195fa70` | Challenger C1p: FirmLooper v3 loops priced by the sealed steady profile, composed with B3. |
| [`pipelines/O4-r2.toml`](pipelines/O4-r2.toml) | `/data/scfix-o4/design/pipelines/O4-r2.toml` | `1ee63d43790eba149b412a04b6a9cb8367a32acc108d54c002bb6d745feec40b` | Revision 2 (A5 + I1 + X1 + X4), composed in P5. Failed B1 on packet; not adopted. |
| [`pipelines/o4r2.toml`](pipelines/o4r2.toml) | `/data/scfix-o4/design/pipelines/o4r2.toml` | `4ea2b72005203d5170476b5ea8fe0e7e023ba465f03c582e561a302a3b3d4f4d` | Build copy of O4-r2.toml (arm o4r2): same passes and capabilities, different name. |
| [`pipelines/O4-r2b.toml`](pipelines/O4-r2b.toml) | `/data/scfix-o4/design/pipelines/O4-r2b.toml` | `6805ba193bc33d097b1c1ce10d757a3d8c042c80d2c889f1cbbf81461c3030ef` | Revision 2b (O4-r2 without A5), the B1 retry. Passed B1-retry and B2, failed H2; not adopted. |
| [`pipelines/o4r2b.toml`](pipelines/o4r2b.toml) | `/data/scfix-o4/design/pipelines/o4r2b.toml` | `a82f3eadb71d0c2279dadcd992c6460a1e8cd87855a25c3ff050c2f08388c50a` | Build copy of O4-r2b.toml (arm o4r2b). |
| [`pipelines/o4r3a.toml`](pipelines/o4r3a.toml) | `/data/scfix-o4/design/pipelines/o4r3a.toml` | `b76c5d06906e05fedf3a0507c7f5ce44cb99de89d4b92c4df8b56bd553d0f4bb` | R3 variant o4r3a: O4-r1 + A5. |
| [`pipelines/o4r3b.toml`](pipelines/o4r3b.toml) | `/data/scfix-o4/design/pipelines/o4r3b.toml` | `1b27f53932ee06e6289bd9d601908528b91614590a9954726a44017fc5c56ef0` | R3 variant o4r3b: O4-r1 + A5 + X1 + X4. |
| [`pipelines/o4r3c.toml`](pipelines/o4r3c.toml) | `/data/scfix-o4/design/pipelines/o4r3c.toml` | `1739ead0f8798513292226f9305e987aa5f3312f3447119334710324a20cb32b` | R3 variant o4r3c: O4-r1 + A5 + A3. Same `[[pass]]` entries and capabilities as the packaged `../../pipelines/O4-r3.toml` (name and description differ). |
| [`pipelines/o4r3d.toml`](pipelines/o4r3d.toml) | `/data/scfix-o4/design/pipelines/o4r3d.toml` | `8dacdb8017cadbb1dfc9c7009a2704d38616f32d10f1f4031134323534a26e9b` | R3 variant o4r3d: O4-r1 + A5 + A3 + X1 + X4, the registered R3 winner and F's reference. |

### Driver tools (`tools/`, stored as `.py.txt`)

| File | Original path | sha256 | What it is |
|---|---|---|---|
| [`tools/decide.py.txt`](tools/decide.py.txt) | `/data/scfix-o4/driver/decide.py` | `0b7247476a39abade599000de4e2555c7edb1167b57d0989f4bac54cbaf1a438` | The registered decision rule (registration 2) for campaigns A, H, A2, B1, B1-retry, B2 and H2. |
| [`tools/decide_r3.py.txt`](tools/decide_r3.py.txt) | `/data/scfix-o4/driver/decide_r3.py` | `e6554ab034b85d021df575b2eb401c1cf288e26614e37a66c9477c517409969c` | The registered R3 rule, used for R3 and F. It imports decide.py's functions unchanged and checks decide.py's sha256. |
| [`tools/best_most.py.txt`](tools/best_most.py.txt) | `/data/scfix-o4/driver/best_most.py` | `6ff655e283a31d87d457e65896d6e5c5b09c76761bc4c88f61a3cba12c0c12c6` | The amendment's BEST-most check. It reads F.json and computes no estimate of its own (two Holm step-downs with decide.py's `holm`). |
| [`tools/make_plan.py.txt`](tools/make_plan.py.txt) | `/data/scfix-o4/driver/make_plan.py` | `048da1778f3b26e3457c221ffdfb854c2513f7ea012adc716dcfea225f4c0e05` | Writes the driver plans for campaigns A, A2, H, B1 and B2; refuses to write a plan when decide.py differs from DECIDE.sha256. |
| [`tools/compare_rtos.py.txt`](tools/compare_rtos.py.txt) | `/data/scfix-o4/driver/compare_rtos.py` | `f7046c86be09c37d7b6b12665cfc0d4b427696aedd1ff956dea319de277d7736` | The private timing driver: round 2's compare_rtos.py plus --settle-seconds, a recorded burn-in block and the runtime-identity QEMU check at staging (design §3.9). |

## Not archived

No record was over the 2 MB limit, so nothing was left out for size. The rest of
`/data/scfix-o4` stays on the original machine only. That covers the per-workload build and
timing directories (`packet/`, `uart/`, `cli/` and the `-w` work trees), `plans/`, `gates/`,
`hygiene/`, `p4b/`, `p5/`, the rest of `p6/`, `r3/`, `evidence/`, the decide console outputs
(`*.stdout.txt`), the driver's other scripts, tests and simulation (`driver/`), and the
design's working files (`design/arms.json`, `commands.txt`, `inventory.md`, `feasibility.md`,
`O4-design.v1.md`, `O4-design.json`, `probe/`, `probe2/`).
