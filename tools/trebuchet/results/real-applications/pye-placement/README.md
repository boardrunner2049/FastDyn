# Pye guest code placement

Two release images ran the same editor training transaction on stock QEMU. Both moved the same 66 bytes of `strncmp` into a 68-byte load segment. The reference placed this segment at 0x80000 and the alternative at 0x54000. Both segments executed in separate guest-profile checks (72,796 entries each). The near address is the next free 4 KiB page following the original image.

The fixed-address runner completed one warmup and 24 balanced pairs. The median edit took 17.291 ms at 0x80000 and 16.987 ms at 0x54000. The median paired ratio was 1.021× with a 95% bootstrap interval of 0.993–1.064. Placement alone did not establish a gain. This comparison used fresh launches without a translation cache.

The hosted summary step failed after writing all observations because it used a hard-coded deployment label. The corrected analysis checked every planned observation against its individual result, full returned document, UART digest and image hash. The ZIP and action log were archived locally before the temporary branch and run were deleted. [Plan](plan.json) · [Raw observations](observations.jsonl.gz) · [Verified result](result.json)
