
Guest work (ms, median)

| category | 1 stock QEMU | 2 FastDyn | 3 +FirmBOLT | 4 +warm cache | 5 +scopt cache | cache deployed in 5 |
|---|---:|---:|---:|---:|---:|---|
| `compute` | 1175 | 1435 | 1413 | 1372 | 1184 | `sfb+b3` |
| `fwbench` | 1100 | 1470 | 1120 | 1077 | 1038 | `b3` |
| `kernbench` | 1141 | 1758 | 1470 | 1426 | 1420 | `ordinary warm` |
| `libbench` | 1005 | 1076 | 747 | 723 | 585 | `sfb+b3` |
| `algobench` | 982 | 1064 | 1178 | 1159 | 894 | `sfb+b3` |

Speedup of each step, guest (median of within-block ratios)

| category | 2→3 | 3→4 | 4→5 | 2→5 | 1→5 |
|---|---:|---:|---:|---:|---:|
| `compute` | 1.013× | 1.030× | 1.159× | 1.208× | 0.989× |
| `fwbench` | 1.313× | 1.040× | 1.035× | 1.412× | 1.059× |
| `kernbench` | 1.194× | 1.032× | 1.007× | 1.231× | 0.801× |
| `libbench` | 1.437× | 1.031× | 1.237× | 1.848× | 1.716× |
| `algobench` | 0.900× | 1.016× | 1.298× | 1.188× | 1.102× |
| **geometric mean** | **1.155×** | **1.030×** | **1.142×** | **1.358×** | **1.097×** |

Whole process (ms, median)

| category | 1 stock QEMU | 2 FastDyn | 3 +FirmBOLT | 4 +warm cache | 5 +scopt cache | cache deployed in 5 |
|---|---:|---:|---:|---:|---:|---|
| `compute` | 1210 | 1674 | 1652 | 1612 | 1426 | `sfb+b3` |
| `fwbench` | 1116 | 1694 | 1346 | 1303 | 1269 | `b3` |
| `kernbench` | 1172 | 1987 | 1700 | 1662 | 1655 | `ordinary warm` |
| `libbench` | 1042 | 1302 | 973 | 951 | 811 | `sfb+b3` |
| `algobench` | 1013 | 1291 | 1405 | 1385 | 1121 | `sfb+b3` |

Speedup of each step, process (median of within-block ratios)

| category | 2→3 | 3→4 | 4→5 | 2→5 | 1→5 |
|---|---:|---:|---:|---:|---:|
| `compute` | 1.012× | 1.025× | 1.132× | 1.170× | 0.846× |
| `fwbench` | 1.260× | 1.031× | 1.026× | 1.335× | 0.880× |
| `kernbench` | 1.169× | 1.025× | 1.004× | 1.195× | 0.704× |
| `libbench` | 1.336× | 1.024× | 1.174× | 1.611× | 1.288× |
| `algobench` | 0.917× | 1.013× | 1.236× | 1.148× | 0.903× |
| **geometric mean** | **1.128×** | **1.024×** | **1.111×** | **1.281×** | **0.906×** |
