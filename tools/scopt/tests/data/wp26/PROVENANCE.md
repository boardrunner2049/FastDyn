# WP26 test data provenance

Copies of recorded files (read-only use in tests/unit/test_wp26_harness.py). Sources:

| file | source | sha256 |
|---|---|---|
| logs/7-icount-nocache-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/7/nocache-1/guest.norm | 5937d2bb92795fc3… |
| logs/7-icount-warm-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/7/warm-1/guest.norm | 5937d2bb92795fc3… |
| logs/7-noicount-nocache-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/noicount/7/nocache-1/guest.norm | 80446e437ffe09cb… |
| logs/7-noicount-warm-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/noicount/7/warm-1/guest.norm | 03606a51c1ecdaf0… |
| logs/3b-icount-warm-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/3b/warm-1/guest.norm | 4b8ad09ea8803cd7… |
| logs/b4-3-icount-warm-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/b4-3/warm-1/guest.norm | c5b828fb3314c472… |
| logs/b4-9-icount-warm-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/b4-9/warm-1/guest.norm | 473cb729a91b2ceb… |
| logs/b4-9-noicount-warm-1.norm | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/noicount/b4-9/warm-1/guest.norm | 473cb729a91b2ceb… |
| stale/b3-1.toml | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/1/warm-1/fastdyn.toml | 534a0c7abfe644d2… |
| stale/b3-1.sc.fastdyn.json | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/1/warm-1/cache/b3-1.sc.fastdyn.json | 2e23f425c4b51b22… |
| stale/b3-16a.toml | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/16a/warm-1/fastdyn.toml | a1733b02a88b476c… |
| stale/b3-16a.sc.fastdyn.json | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/16a/warm-1/cache/b3-16a.sc.fastdyn.json | 5db460d53b5d0b21… |
| stale/b3-16e.toml | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/16e/warm-1/fastdyn.toml | cc801eda90e42ff6… |
| stale/b3-16e.sc.fastdyn.json | /data/extra_copy/fastdyn/runs/wp24/qual-rework/b3/icount/16e/warm-1/cache/b3-16e.sc.fastdyn.json | 1ecc1266ef76bd92… |
| stale/b3.elf | /data/extra_copy/fastdyn/runs/wp24-fix/fixture-before-fix/b3.elf (the fixtures/b3/b3.elf the stale/ manifests were recorded with: size 36424, mtime_ns 1794911183321234382) | 39a5f283895d1e67… |
| int/7-warm.int.log | /data/extra_copy/fastdyn/runs/wp24/edet-int/7-warm/work/qemu.log (-d int) | c17fbfdc74a9b6ec… |
| int/7-nc1.int.log | /data/extra_copy/fastdyn/runs/wp24/edet-int/7-nc1/work/qemu.log (-d int) | c17fbfdc74a9b6ec… |
| stats/guarded.json | /data/extra_copy/fastdyn/runs/wp22/unit/v6-unit/runs/guarded/v6-stats.json (qint-v6 4671d5ceda) | 48a34ae3ba0c1305… |
| stats/v5-control.json | /data/extra_copy/fastdyn/runs/wp22/unit/v6-unit/runs/v5-control/v6-stats.json | d0d3bf5d78c87a63… |

The stale/ manifests name the fixture ELF and libfastdyn.so by path, size and mtime. The tests that rebuild identity bundles from them point the firmware entry at a temporary copy of stale/b3.elf with the recorded mtime (WP24 rebuilds fixtures/b3/b3.elf, which made these tests skip); they still skip (BundleStale) only if src/fastdyn/build/libfastdyn.so changed, which is never modified.
