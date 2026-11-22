
Guest work (ms, median)

| category | 1 stock QEMU | 2 FastDyn | 3 +FirmBOLT | 4 +warm cache | 5 +scopt cache | cache deployed in 5 |
|---|---:|---:|---:|---:|---:|---|
| `compute` | 1182 | 1451 | 1395 | 1354 | 1183 | `sfb+b3` |
| `fwbench` | 1094 | 1470 | 1121 | 1066 | 1039 | `b3` |
| `kernbench` | 1131 | 1738 | 1451 | 1402 | 1402 | `ordinary warm` |
| `libbench` | 992 | 1064 | 742 | 720 | 581 | `sfb+b3` |
| `algobench` | 972 | 1052 | 926 | 911 | 844 | `sfb+b3` |

Speedup of each step, guest (median of within-block ratios)

| category | 2→3 | 3→4 | 4→5 | 2→5 | 1→5 |
|---|---:|---:|---:|---:|---:|
| `compute` | 1.043× | 1.025× | 1.142× | 1.223× | 0.992× |
| `fwbench` | 1.303× | 1.050× | 1.022× | 1.410× | 1.055× |
| `kernbench` | 1.199× | 1.035× | 1.000× | 1.243× | 0.807× |
| `libbench` | 1.440× | 1.029× | 1.238× | 1.827× | 1.709× |
| `algobench` | 1.139× | 1.019× | 1.072× | 1.244× | 1.147× |
| **geometric mean** | **1.217×** | **1.032×** | **1.092×** | **1.373×** | **1.106×** |

Whole process (ms, median)

| category | 1 stock QEMU | 2 FastDyn | 3 +FirmBOLT | 4 +warm cache | 5 +scopt cache | cache deployed in 5 |
|---|---:|---:|---:|---:|---:|---|
| `compute` | 1207 | 1690 | 1635 | 1594 | 1419 | `sfb+b3` |
| `fwbench` | 1112 | 1696 | 1346 | 1296 | 1267 | `b3` |
| `kernbench` | 1159 | 1963 | 1678 | 1627 | 1631 | `ordinary warm` |
| `libbench` | 1037 | 1287 | 965 | 946 | 806 | `sfb+b3` |
| `algobench` | 1004 | 1277 | 1150 | 1134 | 1068 | `sfb+b3` |

Speedup of each step, process (median of within-block ratios)

| category | 2→3 | 3→4 | 4→5 | 2→5 | 1→5 |
|---|---:|---:|---:|---:|---:|
| `compute` | 1.036× | 1.021× | 1.118× | 1.185× | 0.850× |
| `fwbench` | 1.254× | 1.041× | 1.024× | 1.334× | 0.878× |
| `kernbench` | 1.171× | 1.029× | 1.000× | 1.209× | 0.709× |
| `libbench` | 1.336× | 1.022× | 1.169× | 1.598× | 1.276× |
| `algobench` | 1.110× | 1.014× | 1.059× | 1.194× | 0.941× |
| **geometric mean** | **1.177×** | **1.025×** | **1.072×** | **1.295×** | **0.913×** |
