# EXPECT pause_iter=300
# EXPECT pause_in_spin=1
# EXPECT same=1
# EXPECT-RES 0x00520000=1000
# EXPECT-RES 0x00520001=0
# WP22 T-B3-26 "invalidate R" (input inputs/diag-13d.bin, --sigint-after):
# PAUSED stop (no flush, G11) at iteration K = 300 with the link armed; a
# debug write of the SAME halfword into the callee f_b3_13 (R) invalidates R
# without changing behaviour (stock do_tb_phys_invalidate(R) removes R|n from
# C's jump list).  Release the spin, detach.  Expected: z = 1000, nz = 0.
continue
printf "B3DIAG pause_in_spin=%d\n", ($pc >= (unsigned int) &b3_13_spin && $pc < (unsigned int) &b3_13_spin + 6)
printf "B3DIAG pause_iter=%d\n", $r4
set var $a = ((unsigned int) &f_b3_13) & ~1
set var $hw = *(unsigned short *) $a
set var *(unsigned short *) $a = $hw
printf "B3DIAG same=%d\n", (*(unsigned short *) $a == $hw)
set var *(unsigned int *) &g_go13 = 1
detach
