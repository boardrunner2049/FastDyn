# Host Analysis Migration

The audit started from `main` at
`d1cf287a3b217f18ff08e607d189820089a44c70`; fetching `origin/main` showed no
later commits. The table records both migrations and deliberate non-migrations.
“Current consumers” names existing code, not hypothetical future users.

| Existing implementation | Current consumers | Decision | Canonical owner | Verification |
|---|---|---|---|---|
| Repeated `ELFFile` load-segment reads in DWARF provider, ObjectSan, and Courbet `FirmwareELF` | DWARF return discovery, ObjectSan literal loads, Courbet instruction validation | **Extract shared primitive** | `fastdyn.host.ImageAccess` | Host image tests; Courbet tests |
| `binary_wrange.py` writable-segment selection | Fuzzer snapshot CLI only | **Keep standalone utility** | `fastdyn.binary.binary_wrange` | Unchanged; inventory review |
| ELF symbol loops in Function Counter and Function Tracer | Both run preprocessors | **Extract shared primitive** | `fastdyn.host.SymbolIndex` | Virtual preprocessing tests |
| ELF symbol loops in World pin resolution | World run preprocessor; duplicates counter/core lookup | **Reuse existing service** | `fastdyn.host.SymbolIndex` | World plugin tests |
| ELF symbol/function loops in ObjectSan and VariableWatch | ObjectSan selection/call ownership; VariableWatch fallback/owner labels | **Reuse existing service** | `fastdyn.host.SymbolIndex` | Virtual preprocessing tests |
| Introspection DWARF-plus-symtab merge and prologue fallback | Introspection; duplicates canonical symbol index | **Retain thin compatibility wrapper** | `fastdyn.host` plus legacy `SymbolInfo` map | Introspection support and preprocessing tests |
| `binary.symmap.ElfSymtabProvider` | Legacy `SymbolResolver` clients/tests | **Retain thin compatibility wrapper** | `fastdyn.host.SymbolIndex` | Symmap tests excluding missing fixture |
| `binary.symmap.DwarfProvider` ELF reads and epilogue scan | Introspection and symmap tests | **Retain thin compatibility wrapper** | DWARF provider plus shared session/return analysis | Introspection tests; missing `RTOSDemo.axf` noted below |
| `utils/symbol_lookup.py` subtract-one symbol loop | Standalone YAML/REPL tool; duplicates symbol service | **Reuse existing service** | `fastdyn.host.SymbolIndex` | Import/compile check; CLI interaction not automated |
| Courbet `execution_configuration/elf.py` symbol sets and address reads | Courbet rule compiler/validators | **Retain thin compatibility wrapper** | `fastdyn.host`; Courbet owns ARM32 checks and SHA-256 | Courbet execution-configuration tests |
| `tools/elf2config` symbol names/vector discovery | Configuration hints and vector recovery | **Keep standalone utility** | `elf2config` | `test_elf_to_config.py`; policy is configuration generation |
| `src/fastdyn/utils/helper.py::elf_file_parser` | Legacy VTOR configuration path | **Keep standalone utility** | Existing helper | No second identical current consumer after vector recovery policy is separated |
| Repeated zero-sized function inference in ObjectSan, VariableWatch, introspection | Ownership labels and bounded return scans | **Extract shared primitive** | `SymbolIndex.function_ranges` | Section-boundary host tests |
| Consumer function selection, hook installation, and manifests | Counter, Tracer, ObjectSan, VariableWatch, World | **Keep plugin-specific** | Respective plugin | Affected plugin tests |
| Tracer `_type_info`, `_member_offset`, `_dwarf_subprograms` | Tracer; member/type mechanisms overlap VariableWatch/ObjectSan/schema | **Extract shared primitive** | `fastdyn.host.DwarfQueries`; Tracer keeps printable projection | Tracer preprocessing coverage; DWARF host tests |
| VariableWatch `_unwrap`, `_type_size`, `_fixed_address`, `_member_offset`, field traversal | VariableWatch and same operations in ObjectSan/Tracer | **Remove superseded duplicate** | `fastdyn.host.DwarfQueries` | VariableWatch integration tests; multi-byte ULEB128 test |
| ObjectSan `_type_size`, `_dwarf_address`, `_type_names`, global traversal | ObjectSan and VariableWatch share fixed globals/type sizes | **Remove superseded duplicate** | `fastdyn.host.DwarfQueries` | Static-object/type preprocessing tests |
| `SchemaGenerator._member_offset` DWARF expression parsing | Introspection schemas; established best implementation duplicated by VariableWatch | **Reuse existing service** | `fastdyn.host.constant_member_offset` | Existing schema tests and ULEB128 regression |
| `SchemaGenerator` schema flattening and native field-type mapping | Introspection native schema only | **Keep plugin-specific** | Introspection/schema generator | Existing introspection tests |
| Courbet GDB C++ layout lookup | Hook verifier and ABI validator require real C++/GDB behavior | **Keep plugin-specific** | Courbet | Courbet ABI tests when GDB available |
| Tracer ABI register/stack placement, argument/field limits, rendering | Function Tracer only | **Keep plugin-specific** | Function Tracer | Virtual preprocessing tests |
| ObjectSan object/type/allocation selection and allocator models | ObjectSan only | **Keep plugin-specific** | ObjectSan | ObjectSan preprocessing tests |
| Capstone setup in DWARF provider and introspection return scan | Both need identical Cortex-M return forms | **Extract shared primitive** | `fastdyn.host.Decoder.return_sites` | Return-form and introspection tests |
| Capstone setup in ObjectAccessAnalysis and ObjectSan scans | VariableWatch/ObjectSan candidate set; ObjectSan propagation uses same target decode | **Reuse existing service** | `fastdyn.host.Decoder`; ObjectSan retains native operand policy | Candidate-access and ObjectSan tests |
| `LoopFinder` backward-edge detection | Loop utility/tests | **Keep standalone utility** | `fastdyn.binary.loopfinder` | Existing loop test fixture missing; no API merge |
| `next_pcs` branch/fallthrough analysis | Standalone binary utility | **Keep standalone utility** | `fastdyn.binary.nextpc` | Unchanged; empty indirect targets remain “unknown” |
| `mmiofinder` constant propagation/MMIO filtering | Standalone MMIO utility | **Keep standalone utility** | `fastdyn.binary.mmiofinder` | Existing tests |
| ObjectSan propagation/instrumentation plan | ObjectSan runtime preparation | **Keep plugin-specific** | ObjectSan | ObjectSan tests; candidate semantics not narrowed |
| Virtual and modifier numeric/symbol-offset resolvers | `resolve_vi`, `resolve_mod`; same primitive as Courbet | **Extract shared primitive** | `fastdyn.host.resolve_address` with syntax adapters | QEMU/config resolution tests |
| World pin trigger resolution | World pins; duplicates core symbol lookup but historically accepts exact symbol/numeric only | **Reuse existing service** | Host symbol index; World syntax adapter | World tests; accepted syntax unchanged |
| Courbet rule/expression resolution | Courbet virtual/modifier compiler | **Reuse existing service** | `resolve_address`; Courbet adapter | Courbet symbol-offset tests |
| Callback-specific PX4 arguments, ArduPilot vehicle tags, register/memory expressions | Courbet firmware profiles and core syntax adapters | **Keep plugin-specific** | Existing profiles/adapters | Courbet tests |
| Strong RTOS signatures in `virtuals/utils/rtos_models.py` | Introspection and ObjectSan allocator inference | **Retain thin compatibility wrapper** | `fastdyn.host.rtos` | RTOS and ObjectSan tests |
| Tentative `_RTOS_SYMBOLS` scoring in `elf2config` | Starter-configuration hints | **Reuse existing service** | Shared marker data; `elf2config` keeps scoring policy | `test_elf_to_config.py` |
| RTOS hook selection and runtime schemas | Introspection implementations | **Keep plugin-specific** | Introspection | RTOS support tests |
| SVD catalog/resolution helpers | Platform browser/config parser and `elf2config` | **Reuse existing service** | Existing `parse_config.resolve_svd` | SVD/platform tests; no new parser |
| Vector-table, writable-range, and RAM-size recovery | `elf2config`, snapshot utility | **Keep standalone utility** | Their existing tools | Policies/defaults are not common image facts yet |
| Courbet hook requirement extraction and presentation | Hook verifier | **Keep plugin-specific** | Courbet GDB tool | GDB-only workflow not run in unit environment |
| Courbet target/PX4 ABI expectations and bridge conventions | PX4 profile, execution validator | **Keep plugin-specific** | Courbet profiles | Courbet matching/mismatch tests |
| ChibiOS timer IRQ discovery and live VTOR reads | Courbet GDB timer tool | **Keep plugin-specific** | Courbet GDB tool | Not cached as immutable image data; not run without GDB target |
| `fmu_build`, `fmu_runtime`, and FMI3 packager XML parsing | Build-time value references, runtime package, source packaging | **Extract shared primitive** | `fastdyn.fmu_metadata` (domain-local, not host API) | FMU build/runtime tests |
| FMU header autogeneration through FMPy | FMI2 physics harness generator | **Keep plugin-specific** | Physics FMU tool | Requires richer causality/type behavior; no behavior-equivalent reuse |
| Aircraft-required FMU value references | Flight-controller integration | **Keep plugin-specific** | `fmu_build` integration constants | FMU missing-variable tests |
| Packaged FMU extraction and binary selection | Runtime preparation only | **Keep plugin-specific** | `fmu_runtime` | Runtime tests confirm XML/binary stay in same package |
| Gazebo SDF latitude/longitude parsing in `get_earth_mag.py` and calibration runner | Earth-field tool and frame calibration use different inputs | **Keep plugin-specific** | Courbet Gazebo tools | No identical parser contract established |
| Magnetic field, Procrustes/frame calibration, sample collection, transport | Physics tooling only | **Keep plugin-specific** | Courbet Gazebo tools | Units/frames untouched; optional dependencies absent from core imports |
| Coverage block/objdump parsers and expansion in `coverage_analysis.py` and `coverage_report.py` | Both reporting tools | **Extract shared primitive** | `utils/coverage_helpers.py` | Coverage malformed/missing/duplicate/bounds tests |
| Coverage-growth parser in Courbet `graph_gen.py` and `graph_multi_gen.py` | Single- and multi-run plots | **Extract shared primitive** | `utils.coverage_helpers.py` | Growth normalization tests |
| Coverage plotting, LCOV/HTML generation, and CSS patching | Reporting scripts | **Keep standalone utility** | Existing tools | Plotting/report formats unchanged |
| TwinTrace parser | TwinTrace host utility/runtime | **Keep standalone utility** | `fastdyn.utils.twintrace` | No duplicated operation found |
| Ethernet trace decoder/live observers | `print_eth_trace.py`, World/Introspection observers | **Keep standalone utility** | Respective tools/plugins | Formats and live state intentionally not unified |
| `raw2elf` reconstruction and SVD inference | External submodule integration | **Reuse existing service** | `tools/raw2elf` public interface | Submodule unchanged |
| World validation/binding generation | World plugin and external `world` tool | **Reuse existing service** | `tools/world` | Submodule unchanged; plugin continues current integration |
| Rumoca compilation/package boundary | FMU build pipeline | **Reuse existing service** | Rumoca submodule/CLI | Submodule unchanged |
| FIRE composition/toolchain boundary | Physics composition/build | **Reuse existing service** | Existing FIRE/Rumoca integration | No wrapper or submodule change |

## Verification

Run from the repository root with the existing environment:

```text
PYTHONPATH=src:. fastdyn/bin/python -m pytest -q \
  tests/unit/test_host_analysis.py tests/unit/test_coverage_helpers.py \
  tests/unit/test_virtual_preprocessing.py tests/unit/test_world_plugin.py \
  tests/unit/test_introspection_support.py tests/unit/test_fmu_build.py \
  tests/unit/test_fmu_runtime.py tests/unit/test_elf_to_config.py \
  tests/unit/test_qemu_resolution.py tests/unit/test_config_defaults.py \
  src/fastdyn/binary/tests/test_mmiofinder.py \
  src/fastdyn/binary/tests/test_symmap.py \
  virtuals/physics/flight_controllers/courbet/tests/test_execution_configuration.py
```

Final affected-boundary result: `131 passed, 2 skipped, 2 subtests passed`.
The skips are existing optional cases reported by pytest.

An earlier binary-helper run also found `69 passed, 2 skipped, 2 subtests
passed`; `src/fastdyn/binary/tests/test_loopfinder.py` could not run because the
repository does not contain its expected `tests/RTOSDemo.axf` fixture. This is
marked unverified rather than treated as a passing check.

The broader unit run, excluding the unavailable MAVLink dependency, reported
`282 passed, 21 skipped, 2 failed`. Both failures read the already-modified
`tests/binaries/freertos_stm32f429i_discovery/freertos_stm32f429i_discovery.elf`
worktree fixture, which was dirty before this refactor and currently lacks the
DWARF expected by those tests. Collecting the complete unit suite without the
exclusion is blocked by the environment's missing `pymavlink` package.

The focused tests demonstrate session identity reuse across two preparation
contexts and verify that ELF parsing remains lazy. Existing same-PC virtual
conflict tests remain unchanged and exercise the native dispatcher's existing
one-callback restriction separately from analysis reuse.

Representative artifacts were generated once from an archived clean `HEAD`
tree and once from this worktree. Concatenated artifact SHA-256 and byte counts
matched exactly:

| Workflow | Before and after SHA-256 | Bytes |
|---|---|---:|
| Function Counter | `ed6ca6d2585fb829402b5bd437dd647c166e9c7fe255636aed8d25f5b7f95d73` | 3,083 |
| Function Tracer | `ad4b776ca54be4c890406a3f87243966951a039469d7d5c0d9f16a99a0e77168` | 202 |
| ObjectSan static object | `582caffdb1f22e291f96873ca38a12cf3cdba95e5124fd10748f06c2d4d2ab61` | 77,407 |
| VariableWatch structure field | `23a4ca461097d0ee58eb3c250d5296a65376e1464b1190d37315a59879384306` | 482 |
| Courbet ArduPilot Plane execution configuration | `7bc7c1af4c25921275f50717fa2b445ac0cb7a3efc6e5f0cf86de325eecef3e1` | 845,513 |

## Intentional output differences

- `utils/symbol_lookup.py` now clears the Thumb bit only for function symbols.
  Its previous unconditional `address - 1` corrupted data addresses and address
  zero. This aligns the utility with ELF symbol kind and preserves odd data.
- Ambiguous World symbol triggers now fail explicitly instead of silently using
  the first symbol-table entry. Numeric-only World configurations still do not
  open the ELF.
- Coverage helpers skip malformed rows in the permissive report and retain the
  strict analysis script's failure behavior. Block expansion is bounded rather
  than raising an accidental `IndexError`.

Generated plugin artifact schemas, Courbet rule files, CLI entry points,
configuration keys, native runtime behavior, and callback-specific arguments
are unchanged.

## Concise PR description

Add a lazy, run-scoped `fastdyn.host` firmware-analysis session with safe image
reads, target-aware symbols/function ranges, conservative DWARF queries,
Cortex-M decoding/return discovery, address resolution, and shared RTOS marker
data. Migrate current plugin/Courbet consumers, retain thin legacy adapters,
factor domain-local FMU and coverage parsers, and document every audited
analysis that intentionally remains specialized.
