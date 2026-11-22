# PX4 scoped `memset` layout experiment

This experiment compares the rebuilt PX4 v1.17 FMUv6C image with a FirmBOLT
image in which only the mission-hot `memset` function is reordered and moved.
Each independent observation is a fresh FastDyn process completing the same
automatic mission. The six paired blocks are randomized with a fixed seed.

Run from the FastDyn repository root:

```bash
fastdyn-env/bin/python tools/trebuchet/experiments/px4_memset_layout/run_paired.py \
  --baseline /data/trebuchet-clean/work/px4-reloc/original.toml \
  --candidate /data/trebuchet-clean/work/px4-reloc/memset.toml \
  --output /data/trebuchet-clean/work/px4-reloc/paired-missions.jsonl
```

The runner stops on a failed mission and preserves every completed record.
Diagnostics and firmware generation are performed before timing; final runs do
not enable the translation-block profiler.

For scopt capture, derive an otherwise identical non-flat SuperCache config:

```bash
fastdyn-env/bin/python tools/trebuchet/experiments/px4_memset_layout/make_cache_config.py \
  --input /data/trebuchet-clean/work/px4-reloc/original.toml \
  --output /data/trebuchet-clean/work/px4-reloc/original-cache.toml \
  --unpacked-fmu /absolute/path/to/frozen-unpacked-fmu
```

Build the FMU once, extract it to the named fixed tree, and then use
`--unpacked-fmu`. A fixed unpacked tree is required for cache provenance:
Rumoca rebuilding changes the instantiation token and native library identity,
while extracting an otherwise identical package for every run changes the
native library path and timestamp. Either change correctly causes scopt's
bundle or FastDyn fingerprint check to reject the artifact. `--freeze-fmu`
only disables rebuilding and is useful outside the deploy workflow; packaged
scopt runs should use `--unpacked-fmu`.
