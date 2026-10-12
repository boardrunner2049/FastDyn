# FastDyn Coding Conventions

This document defines how code, configuration, tests, documentation, and
commits are written in FastDyn. It applies to every contribution: core,
virtual plugins, device models, and tools.

It extends, and does not replace:

* [WritingVirtuals.md](WritingVirtuals.md): how to write a virtual or run
  plugin, and the **project-wide ownership convention**.
* [VirtualPreprocessing.md](VirtualPreprocessing.md): the public Python
  preprocessing contract.
* [`virtuals/README.md`](../virtuals/README.md): the public C runtime SDK.

Where this document and those disagree, those documents win for their
specific contract. Please report the conflict so it can be fixed.

Rules use **must** (required for merge), **should** (expected, and a
reviewer may ask you to justify an exception) and **may** (allowed).

---

## 1. Architecture rules

These rules protect FastDyn's boundaries. Breaking them is the most common
reason a change is rejected.

### 1.1 What belongs in FastDyn

FastDyn is the execution engine. It provides:

* the QEMU plugin (`core/`, `virtuals/virtuals.c`, `virtuals/runtime_sdk.c`)
* the device-model infrastructure and the backends it owns (`device_models/`:
  dispatch, classic, passthrough, twintrace, elder, halucinator)
* generic virtual/run plugins (`virtuals/<feature>/`)
* the generic Python frontend and API (`src/fastdyn/`): configuration
  parsing, `fastdyn run`, the machine/builder API, the preprocessing SDK,
  and the help browsers
* small generic tools (`tools/`)

Workflows built **on top of** FastDyn live in their own repositories and
call FastDyn through its public API. Examples are model synthesis
(BoardRunner), agentic rehosting (PhantomSmith) and the VIO model library
(LibVIO). They must not be added to `src/fastdyn/`, `core/` or
`device_models/`.

### 1.2 Ownership and placement

```text
one feature only           -> virtuals/<feature>/{host,runtime}/
shared plugin helpers      -> virtuals/utils/   (only when two or more plugins need it)
generic FastDyn support    -> src/fastdyn/, core/, include/  (core-maintainer-owned)
a downstream tool          -> its own repository, using fastdyn's public API
```

* You **must not** put feature-specific behavior, TOML interpretation,
  runtime arguments or feature registries in `src/fastdyn/` or `core/`.
* FastDyn code **must not** name a downstream tool, plugin or model library.
  That means no `if plugin_name == ...`, no `boardrunner/...` default paths
  and no imports of downstream packages. Extension happens only through
  registration: `register_virtual()`, `register_run_preprocessor()`,
  `VIRTUAL_PLUGIN()`, and entry points.
* If a feature needs a new generic capability (an SDK call, a lifecycle
  hook, a loader behavior), you **must** request it from the core
  maintainers as a separate, generic change. It must not be smuggled into a
  feature PR.

### 1.3 Configuration flows through TOML, not command lines

* User settings **must** live in TOML. A plugin reads only its own table
  (`[CPU.cpuN.plugins.<name>]`). A device model reads only the configuration
  FastDyn forwards to it.
* A plugin **must not** add QEMU `--plugin` arguments, CLI flags, or
  environment variables to pass its data. Derived data becomes an artifact
  (`ctx.plugin_artifact_path()` in Python, `virtual_artifact_path()` in C).
* FastDyn **must** forward tables it does not own unchanged. It must not
  partially interpret them.

### 1.4 No layout or machine assumptions

* You **must not** hardcode absolute paths (`/scratch/...`, `/home/...`,
  `/workspace/...`) in code, configs, scripts or docs.
* You **must not** locate files through the checkout layout
  (`Path(__file__).parents[N]`, `../qemu`, `cd ../../..`). Use the
  configured path, a path relative to the configuration file, or the
  package's path helper.
* Relative paths in a TOML file **should** be resolved relative to that
  file. Paths relative to the current directory are legacy behavior.

### 1.5 One implementation per behavior

* Running a configuration has one implementation, the public run API. CLI
  commands and tools **must** call it rather than re-implementing the parse,
  prepare, launch and cleanup sequence.
* Before adding a helper, search for an existing one. Duplicated logic
  (two `compile_device_routing` implementations, two object models, repeated
  work-directory setup) is a review blocker.

---

## 2. Python

### 2.1 Baseline

* Target **Python 3.10+**. Modern syntax such as `X | None` and
  `dict[str, int]` is allowed.
* Every new module **must** start with a one-line module docstring,
  followed by `from __future__ import annotations`.
* Line length **should** be ≤ 100 characters. Existing longer lines are not
  a reason to add more.
* Use 4-space indentation. Never use tabs.

### 2.2 Imports

* Order imports in three groups separated by blank lines: standard library,
  third-party, then `fastdyn` (or the plugin's relative imports).
* Import at module top level. A function-local import is allowed **only**
  for an optional, heavy dependency, and it must carry a comment saying why.
* Importing a module **must not** have side effects beyond definitions and
  explicit self-registration. No I/O, no subprocesses, no network access, and
  no loading other plugins. Registration calls in
  `host/preprocessor.py` are the intended exception.
* Core modules **must not** import optional or feature-specific packages at
  import time. `fastdyn run` must work when those packages are not
  installed.

### 2.3 Naming and structure

* Use `snake_case` for functions, variables and modules, `PascalCase` for
  classes, and `UPPER_CASE` for module constants.
* Prefix private helpers with `_`. Anything without a leading underscore in
  a public module is public API. Keep that surface small and documented.
* Prefer `@dataclass` or `@dataclass(frozen=True)` for structured data over
  dicts with implicit keys.
* Public functions and methods **must** have type hints and a docstring
  that states what they return or guarantee, not how they work.

### 2.4 Errors, exits and logging

* Library code **must not** call `sys.exit()`, `os._exit()`, or
  `click.echo()` / `print()` for diagnostics. Raise an exception. Only the
  CLI layer (`main.py` command functions) turns exceptions into exit codes
  and user messages.
* Raise the most specific documented exception. In preprocessors that is
  `VirtualPreparationError`, with a message that names the TOML key at fault,
  for example `[CPU.cpu0.plugins.function_counter].include must be an array`.
* Chain exceptions: `raise ... from exc`.
* Log with a module logger, `log = logging.getLogger(__name__)` (plugins use
  `ctx.logger`), with lazy formatting (`log.debug("x=%s", x)`). Library code
  **never** configures logging (no handlers, no `basicConfig`); only an entry
  point calls `fastdyn.logs.configure()`. Log messages go to stderr; command
  results go to stdout. `fastdyn_log.getFastdynLogger()` is deprecated.
* Never swallow an exception silently.
* Code **must not** delete user data by default. No `shutil.rmtree()` of a
  default work directory unless the user asked for it.

### 2.5 Files, paths and processes

* Use `pathlib.Path`. Open text files with an explicit `encoding="utf-8"`.
* Code **should** write artifacts only below the run work directory, through
  the provided allocators.
* Code **must not** invoke `fastdyn` as a subprocess from Python. Call the
  API. When a subprocess is necessary for a genuinely external tool, pass
  arguments as a list (never `shell=True` with interpolated strings) and
  check the return code.

### 2.6 CLI commands

* A `click` command is a thin adapter: it parses options, calls one API
  function and reports the result. Command bodies **should** stay under
  about 40 lines. Orchestration belongs in a module that can be tested
  without the CLI.
* New options **must** have help text and sensible defaults. Destructive
  options must be opt-in.

---

## 3. C

### 3.1 Baseline

* The code is C as built by the project's meson configuration, which uses
  the GNU C dialect of the compiler default. GCC-compatible attributes are
  allowed where the project already relies on them (for example the
  `section("virtual_plugins")` registration).
* Use **4-space indentation and no tabs** for new and modified code. Do not
  reformat untouched lines of an existing file in a feature commit. Use a
  separate "style only" commit for that.
* Line length **should** be ≤ 100 characters.
* Put the opening brace on the same line for functions and control
  statements, as in `virtuals/runtime_sdk.c`. Always use braces, even for
  single-statement bodies.

```c
static int valid_relative_path(const char *relative) {
    return relative && relative[0] && relative[0] != '/'
        && strstr(relative, "..") == NULL;
}
```

### 3.2 Naming

* Name functions and variables in `snake_case` with a **module prefix**:
  `core_*`, `dev_*`, `virtual_*` (runtime SDK), `<plugin>_*` inside a plugin.
* Macros and constants use `UPPER_CASE` with the same prefix
  (`VIRTUAL_LOG_INFO`, `VIRTUAL_RUNTIME_MAX_PLUGINS`).
* Types use `PascalCase` (`VirtualContext`, `DeviceModel`). Function-pointer
  typedefs end in `Func` or `_t`, matching the surrounding header.
* Every function and file-scope variable not declared in a header **must**
  be `static`.

### 3.3 Headers

* Public headers live in `include/`. New public headers go under
  `include/fastdyn/` and use guards derived from their path, for example
  `FASTDYN_ARCH_ARM_V7M_H` for `include/fastdyn/arch/arm_v7m.h`.
* A header includes what it uses and nothing else. Do not include a large
  internal header only for one type.
* Order includes as: the file's own header, C standard library, system and
  third-party (`glib`, `qemu/qemu-plugin.h`), then FastDyn headers. Separate
  the groups with blank lines.
* Document public functions with Doxygen comments (`/** @brief ... */`). The
  repository builds documentation from them (`make docs`).

### 3.4 Plugin and device-model code

* A runtime plugin **must** use only `include/fastdyn_runtime.h` and the
  matching `include/fastdyn/arch/*.h` header. It **must not** call
  `virtual_register()`, `core_get_run_artifact_path()`, `qemu_get_register()`,
  `qemu_set_register()` or other internals directly. It must not parse
  plugin arguments, and must not add entries to `virtuals/virtuals.c`.
* Use the architecture ABI macros (`VIRTUAL_FIRST_ARG`,
  `VIRTUAL_RETURN_VALUE`, ...) or named register constants. Never use bare
  register numbers.
* Diagnostics go through `virtual_log()` (runtime plugins), the
  `FD_LOG_*` macros from `include/fastdyn/log.h` (core code; define
  `FD_LOG_SUBSYSTEM` before the include), or the existing `dev_debug`
  macros (device models), not bare `printf()`. These print
  `fastdyn.<subsystem>|LEVEL|  message` to stderr at the level the
  frontend passes in. Nothing may be logged on a hot path
  (per-instruction, per-TB, per-MMIO) except with `FD_LOG_DEBUG` /
  `FD_LOG_TRACE`, which are compiled out unless `DEBUG_PRINT` is set.
* Persistent output **must** go through `virtual_artifact_path()` for
  plugins, or a configured path for device models. Never use a hardcoded
  location.
* Treat callback argument strings as untrusted. Validate and parse them
  defensively even when a Python preprocessor already validated them.

### 3.5 Safety

* Check every return value that can fail: allocation, I/O, `dlopen` /
  `dlsym`, `snprintf` truncation. Handle the failure or report it with
  context. Do not ignore it.
* Use bounded string functions (`snprintf`, not `sprintf` / `strcpy`), and
  check for truncation as `runtime_sdk.c` does.
* Mark intentionally unused parameters with `(void)name;`.
* Shared state touched from vCPU and main-loop threads **must** be
  protected (atomics, a mutex, `g_once`) and commented with the threads that
  access it.
* Guard optional features with meson options, `config.h` or
  auto-detection. Never leave commented-out or `#if 0` code in a merged
  change. Delete it; git keeps the history.

---

## 4. Build system

* Each plugin owns a `virtuals/<feature>/meson.build`, included from
  `virtuals/meson.build`. A plugin **must not** add a top-level meson option
  or Makefile flag just to enable itself.
* Optional third-party dependencies are **auto-detected** and skipped
  with a clear `message()` when absent (see `virtuals/world/meson.build`).
  The core build must never fail because an optional feature's dependency is
  missing.
* External libraries are pinned as git submodules under `tools/` or
  `third_party/`. They are optional unless the core needs them. A submodule
  **must** point to a dependency of FastDyn, never to a tool that depends on
  FastDyn.
* Python dependencies go in `requirements.txt`, commented by purpose (core,
  optional feature, developer tooling). A core module must not need an
  optional dependency.

---

## 5. Configuration (TOML) conventions

* Keys use `snake_case`. New plugin tables use `[CPU.cpuN.plugins.<name>]`
  with an `enabled` boolean.
* Every user-configurable virtual or run plugin **must** register
  `ConfigurationHelp` (description, a working TOML snippet, a documentation
  path), so `fastdyn help` lists it without a frontend change.
* Example configs in `configs/` **must** run from a fresh checkout with
  only documented prerequisites. `configs/minimal.toml` must always work.
* Configuration errors **must** name the table and key and say what was
  expected.

---

## 6. Tests

* Every behavior change or bug fix **must** add or update a test.
* **Unit tests** (`tests/unit/`, pytest) **must not** launch QEMU or require
  hardware, network access, API keys or submodules beyond those already
  required. Use small CPU- or machine-shaped test doubles and
  `tmp_path`. Call `prepare_virtual_rules()` /
  `prepare_run_preprocessors()` directly for plugin logic.
* **Integration or smoke tests** (`tests/integration/`) may launch QEMU.
  Each needs a README entry that states its prerequisites.
* **Binary fixtures** must be small and redistributable, and must sit
  beside their source and a build script so they can be regenerated. Do not
  vendor whole RTOS or SDK trees (see
  [WritingVirtuals.md](WritingVirtuals.md#testing-rtos-introspection-without-rtos-submodules)).
* Test native plugin logic with a host-side C harness where practical.
* Before opening a PR, run:

```bash
./fastdyn-env/bin/python -m pytest -q tests/unit
./fastdyn-env/bin/fastdyn run -c configs/minimal.toml
```

---

## 7. Documentation

* A new plugin, device model or user-visible feature **must** add
  `docs/<FeatureName>.md` (CamelCase file name, like the existing docs) and
  link it from `README.md` and its `ConfigurationHelp`.
* Update documentation in the **same PR** as the behavior it describes.
* Code comments explain *why*: invariants, constraints, non-obvious
  choices. They do not narrate what the code does. Remove "TODO: remove
  this" comments by doing the removal, or open an issue and reference it.

---

## 8. Git and pull requests

### 8.1 Branches

* Branch from `main` with a descriptive name (`separation_of_concerns`,
  `variable-watch-raw-binaries`). Do not commit directly to `main`.
* Keep one topic per branch. Unrelated fixes go in their own PR.

### 8.2 Commits

* The subject line is imperative, ≤ 72 characters and has no trailing
  period. It may carry an area prefix:

```text
Add co-simulation slave mode with exact budget stops
core: drop the qemu_plugin_set_budget_exact_stop call
vio/spi: fix NULL bus-name crash in slave parsing
```

* For anything non-trivial, the body **should** explain *why* the change is
  needed and what was measured or verified (see `efda810` for a good
  example). Wrap it at about 72 characters.
* Each commit **should** build and pass the unit tests. Keep refactors,
  moves and behavior changes in separate commits, so each diff can be
  reviewed.
* Never use commit subjects like "fix", "update", "changes", "wip" or
  "hmm".

### 8.3 What must never be committed

* Build outputs and caches (`build/`, `*.so` except intentional test
  fixtures, `__pycache__/`, `*.egg-info/`), virtual environments, or run
  outputs (`fastdyn_work*/`, `run-artifacts/`, logs, traces, LLM
  histories).
* Machine-specific files (`build_config.env` with local paths, editor swap
  files, `.env`).
* Secrets such as API keys and tokens, including inside logs or prompt
  transcripts.
* Large binaries (> ~5 MB) without prior agreement. Use release assets or
  LFS instead.
* Output generated by downstream tools (generated device models, target
  configs, agent transcripts). These belong in the tool's repository.

### 8.4 Pull request checklist

Copy this into the PR description:

```markdown
- [ ] Code is placed according to the ownership rules (§1.2); no downstream names or paths in core
- [ ] No new CLI flags / QEMU plugin args for feature data; settings live in TOML (§1.3)
- [ ] No absolute or checkout-relative paths (§1.4); no duplicated logic (§1.5)
- [ ] Python: type hints + docstrings on public API, no sys.exit/print in library code (§2)
- [ ] C: 4-space style, module prefixes, static internals, return values checked (§3)
- [ ] Optional dependencies auto-detected; core build unaffected (§4)
- [ ] ConfigurationHelp registered for user-facing features (§5)
- [ ] Tests added/updated; `pytest tests/unit` and `configs/minimal.toml` pass (§6)
- [ ] Docs updated in this PR (§7)
- [ ] Commits are focused with descriptive messages; nothing from §8.3 committed
```

Changes to public contracts (the runtime SDK, the preprocessing SDK, the
device-model ABI, the run API, TOML schema semantics) require a core
maintainer's review.

---

## 9. Tooling

No formatter or linter is enforced in CI yet. Until one is, follow this
document by hand. The planned configuration, to be added in a separate
change agreed with the core maintainers, is:

| Language | Tool | Intended settings |
|---|---|---|
| Python | `ruff check` + `ruff format` | line length 100, target `py310`, rules `E,F,W,I,B,UP` |
| C | `clang-format` | 4-space indent, no tabs, attached braces, column limit 100, include blocks preserved |
| Both | `pre-commit` | runs the above on changed files only, so legacy files are not reformatted wholesale |

When the tools are introduced, legacy files are reformatted in dedicated
"style only" commits, never mixed with behavior changes.
