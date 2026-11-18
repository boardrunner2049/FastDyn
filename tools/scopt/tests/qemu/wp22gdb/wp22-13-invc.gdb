# EXPECT pause_iter=300
# EXPECT pause_in_spin=1
# EXPECT cbz=0xb108
# EXPECT cbnz=0xb908
# EXPECT-RES 0x00520000=300
# EXPECT-RES 0x00520001=700
# WP22 T-B3-26 "invalidate C" (input inputs/diag-13d.bin, --sigint-after):
# the F-B3-13 kernel spins at iteration K = 300 after the link R -> C' was
# used; the SIGINT is a remote interrupt = RUN_STATE_PAUSED stop, which does
# NOT flush (G11), so the link is still armed when the debug write turns
# vc_b3_13's cbz into cbnz (code-page invalidation of C while armed: stock
# tb_jmp_unlink(C) must reset R's SPEC slot to its reset path).  Release the
# spin, detach.  Expected guest results: z = 300 (iterations 0..299), nz = 700.
continue
printf "B3DIAG pause_in_spin=%d\n", ($pc >= (unsigned int) &b3_13_spin && $pc < (unsigned int) &b3_13_spin + 6)
printf "B3DIAG pause_iter=%d\n", $r4
set var $hw = *(unsigned short *) &vc_b3_13
set var *(unsigned short *) &vc_b3_13 = $hw | 0x0800
printf "B3DIAG cbz=0x%04x\n", $hw
printf "B3DIAG cbnz=0x%04x\n", *(unsigned short *) &vc_b3_13
set var *(unsigned int *) &g_go13 = 1
detach
