# Protocol: does SFB + B3 beat SFB alone? (written before any timing of the combination)

Question: on the Trebuchet FirmBOLT image, is one cache carrying both the icount-entry
store-forwarding fix (sfb) and return/static chaining (b3) faster on guest work than sfb alone?

Arms (same image, configuration, QEMU, private tmpfs RAM): ordinary warm; sfb; b3; sfb+b3
(b3 applied to the re-imported sfb artifact, warm baseline = the ordinary snapshot).

Selection (decides what Trebuchet deploys): `scopt tune-warm` with the three candidates,
12 pairs each on heldout-1, confirmation of the winner with 12 pairs on heldout-2 and heldout-3.

Final evaluation (reported whatever it shows): final-1..3, one unrecorded warm-up block per
input, then 8 blocks per input; a block is one launch of each of the four arms in random
order (seed 20261121). Metric: FastDyn [BENCH] guest window; step speedups are medians of
within-block ratios with a 95 % bootstrap interval over blocks.

Checks on every launch: sixteen reported registers equal the reference; run proof PASS;
zero translation blocks appended. Any failed launch invalidates the campaign (no selective
reruns). An infrastructure failure means repeating the whole campaign once.

Stopping rule: exactly one selection run and one final campaign. No retuning after final.
Decision: sfb+b3 becomes a Trebuchet candidate if it builds and validates on both images;
it is deployed only where tune-warm selects it.
