# Compiled-launch benchmark

`fastdyn compile-launch` removes the Python frontend from repeated QEMU
launches.  On the canonical Cortex-M3 firmware-inline fixture, a randomized
10-pair comparison reduced median process wall time from **302.859 ms** through
`fastdyn run` to **32.884 ms** through direct QEMU execution: a **9.249x**
paired median reduction (bootstrap 95% interval **8.872-9.477x**).  All 20
runs passed, loaded the trusted cache without appending records, and produced
the same normalized guest output.

The measured firmware interval did not change: 287.087 us through the Python
frontend and 279.561 us directly, with a paired ratio whose interval included
one.  The speedup is launch/setup removal, not a change to generated native
code.

A second 12-pair campaign ran the actual generated `run.sh` for trusted-cache
and uncached bundles.  Trusted execution reduced the firmware interval from
**1.357 ms** to **0.327 ms**, a **4.235x** paired median speedup (bootstrap 95%
interval **3.706-5.025x**).  Fresh-process wall time was 37.793 ms uncached and
37.149 ms trusted; the paired ratio was **1.000x** (**0.964-1.071x**).  After
removing Python, QEMU process startup still dominates this sub-millisecond
workload.  Persistent QEMU execution is therefore the next required step for
turning this cache gain into per-input throughput.

## Scope and controls

- Host: x86-64 Ubuntu, Linux 6.17.0-1033-oem.
- QEMU revision: `bd331f89c35ad9aed50dea85e20e4d1ef36978fd`.
- QEMU SHA-256: `8d130b5b32fa116f099ca428a5dcc76b8ef9592c5f557e28934aa03e97844eac`.
- FastDyn plugin SHA-256: `15ed0f8d8fbbbcb3a5d1167c31e3dbe107147a4b2018a51eab32b8f8aca191e6`.
- Firmware SHA-256: `53ca19e3ae4927d4a81d95420c57567484ca2a3375f0757ac0fd8ec7db4f0a04`.
- Trusted cache SHA-256: `9fe13f4653456976ba2d1e90efa60b2abd3bfae126775d62e0c440e090dd68df`.
- Each process received fresh sparse RAM.  Trusted runs used `setarch x86_64
  -R`; the cache remained unchanged and appended zero records.
- The first campaign used the exact direct command later emitted by the
  compiler.  The second campaign used the generated scripts themselves.

The historical six-firmware stock-QEMU comparison remains in
[`../selected-warm-benchmark/README.md`](../selected-warm-benchmark/README.md).
Its stock and firmware fixture binaries are not retained in this checkout, so
these new measurements do not splice a current direct-launch number into that
older matched campaign.  Rebuilding or restoring those fixtures is required
for a new direct-versus-stock suite claim.

Raw observations are in [`frontend-samples.csv`](frontend-samples.csv) and
[`cache-samples.csv`](cache-samples.csv).  Process wall covers only the launch
command through exit; bundle compilation/deployment is setup outside that
boundary.  Confidence intervals bootstrap paired blocks 20,000 times with the
recorded pairing.
