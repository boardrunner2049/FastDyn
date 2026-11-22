# Guest-only cacheless training control

On the distro stock QEMU ARM emulator, the trained FirmBOLT relocation had a median paired operational speedup of 1.012× across 24 balanced pairs. The paired bootstrap 95% interval spans 0.989–1.023×. The data do not establish a repeatable advantage for the training edit. The marginal original and relocated medians were 16.892 and 16.928 ms. Paired ratios and ratios of marginal medians estimate different quantities.

The whole QEMU process had a median paired speedup of 0.998× (0.982–1.013×), and its CPU-time speedup was 0.992× (0.978–1.011×). Every measured launch and both warmups returned the expected 166-byte document and exited normally.

These controls used one pinned CPU, disabled process ASLR, the same stock QEMU binary and the same training document. Raw data and both ELF hashes accompany this summary. Actions run 44982302595 was removed after preserving and checking the archive in /workspace/trebuchet-work/runner/44982302595. The experiment used guest code placement alone. Selection and held-out evaluation use the separate FastDyn tracks.
