"""Configuration discovery for user-facing FastDyn virtuals and plugins."""
from __future__ import annotations

from dataclasses import dataclass

from .platform_browser import DocumentationEntry, Menu, browse_menu
from . import virtual_preprocessing


@dataclass(frozen=True)
class FeatureEntry:
    """One user-configurable feature and the TOML needed to enable it."""
    kind: str
    name: str
    description: str
    toml: str
    documentation: str


_MODIFIERS = (
    FeatureEntry("Modifier", "register assignment", "Set an architecture register at a trigger PC.",
                 '[[CPU.cpu0.modifiers]]\nat = "0x08001234"\npatch = "r0 <- 1"',
                 "docs/VirtualsAndModifiers.md"),
    FeatureEntry("Modifier", "memory assignment", "Write an immediate value to an absolute guest address.",
                 '[[CPU.cpu0.modifiers]]\nat = "0x08001234"\npatch = "0x20001000 <- 0x42"',
                 "docs/VirtualsAndModifiers.md"),
    FeatureEntry("Modifier", "register-indirect assignment", "Write through an address held in a register.",
                 '[[CPU.cpu0.modifiers]]\nat = "0x08001234"\npatch = "[r0] <- 0x42"',
                 "docs/VirtualsAndModifiers.md"),
    FeatureEntry("Modifier", "ARM control-flow redirect", "Redirect 32-bit ARM execution by setting r15.",
                 '[[CPU.cpu0.modifiers]]\nat = "0x08001234"\npatch = "r15 <- 0x08004567"',
                 "docs/VirtualsAndModifiers.md"),
    FeatureEntry("Modifier", "x86-64 control-flow redirect", "Redirect x86-64 execution by setting RIP.",
                 '[[CPU.cpu0.modifiers]]\nat = "0x18008"\npatch = "rip <- 0x18004"',
                 "docs/VirtualsAndModifiers.md"),
    FeatureEntry("Modifier", "RISC-V control-flow redirect", "Redirect RISC-V execution by setting riscv_pc.",
                 '[[CPU.cpu0.modifiers]]\nat = "0x8020041c"\npatch = "riscv_pc <- 0x80200418"',
                 "docs/VirtualsAndModifiers.md"),
)


_DEVICE_MODELS = (
    FeatureEntry("Device model", "classic", "Use FastDyn's standard software peripheral model.",
                 '[Device.Models.classic]\n\n[Device.unmapped_peripherals]\nranges = [["0x40000000", "0x5fffffff"]]\ndescription = "Peripherals handled by the classic model."\n\n[[Device.unmapped_peripherals.handlers]]\nmodel = "classic"\nenabled = true',
                 "docs/ClassicDeviceModel.md"),
    FeatureEntry("Device model", "passthrough", "Forward selected peripheral accesses to a hardware backend.",
                 '[Device.Models.passthrough]\nbackend = "stlink"\n\n[Device.hardware_peripherals]\nranges = [["0x40000000", "0x4000ffff"]]\n\n[[Device.hardware_peripherals.handlers]]\nmodel = "passthrough"\nenabled = true',
                 "docs/PassthroughDeviceModel.md"),
    FeatureEntry("Device model", "elder", "Use the ElderScroll stateful peripheral model.",
                 '[Device.Models.elder]\n\n[Device.modeled_peripheral]\nranges = [["0x40000000", "0x400000ff"]]\n\n[[Device.modeled_peripheral.handlers]]\nmodel = "elder"\nenabled = true',
                 "docs/ElderScrollDeviceModel.md"),
    FeatureEntry("Device model", "twintrace", "Use a trace-backed hardware model.",
                 '[Device.Models.twintrace]\nbackend = "stlink"\n\n[Device.trace_peripheral]\nranges = [["0x40000000", "0x400000ff"]]\n\n[[Device.trace_peripheral.handlers]]\nmodel = "twintrace"\nenabled = true',
                 "docs/Configuration.md"),
    FeatureEntry("Device model", "unhandled", "Explicitly mark an address range as unhandled.",
                 '[Device.Models.unhandled]\n\n[Device.ignored_peripheral]\nranges = [["0x40000000", "0x400000ff"]]\n\n[[Device.ignored_peripheral.handlers]]\nmodel = "unhandled"\nenabled = true',
                 "docs/Configuration.md"),
)


_MACHINE_SETTINGS = (
    FeatureEntry("Machine setting", "headless QEMU", "Disable graphical/serial frontends; qemu_path is optional (defaults to the QEMU FastDyn was built against).",
                 '[Machine]\n# qemu_path = "path/to/qemu-system-arm"  # optional\ndisplay = "none"\nserial = "none"\nmonitor_port = 0\nqmp_socket = "/tmp/fastdyn.qmp"\nlog_options = "none"',
                 "docs/Configuration.md"),
    FeatureEntry("Machine setting", "instruction-counted time", "Use deterministic instruction-counted virtual time.",
                 '[Machine]\nicount = { shift = 5, sleep = false, align = false }\ntimer_irq_period_ns = 1000000',
                 "docs/Configuration.md"),
    FeatureEntry("Machine setting", "semihosting", "Enable host-file semihosting for supported firmware.",
                 '[Machine]\nsemihosting = true\nsemihosting_config = "enable=on,target=native"',
                 "docs/Configuration.md"),
)


_MEMORY_SETTINGS = (
    FeatureEntry("Memory setting", "primary file-backed RAM", "Required primary RAM bank backed by a host file.",
                 '[Memory.main]\nid = "ram0"\nbase_address = "0x20000000"\nmemory_size = "1M"\nmemory_type = "SRAM"\nbackend = "file"\nmemory_file = "/tmp/fastdyn.ram"\nshare = true\nprealloc = false',
                 "docs/Configuration.md"),
    FeatureEntry("Memory setting", "additional RAM bank", "Add a second RAM bank to the selected machine.",
                 '[[Memory.ram1]]\nid = "ram1"\nindex = 1\nbase_address = "0x30000000"\nmemory_size = "512K"\nmemory_type = "SRAM"\nbackend = "file"\nmemory_file = "/tmp/fastdyn-ram1.ram"\nshare = true\nprealloc = false',
                 "docs/Configuration.md"),
    FeatureEntry("Memory setting", "anonymous RAM", "Use RAM without a persistent backing file.",
                 '[Memory.main]\nid = "ram0"\nbase_address = "0x20000000"\nmemory_size = "1M"\nmemory_type = "SRAM"\nbackend = "ram"\nshare = false\nprealloc = false',
                 "docs/Configuration.md"),
)


_FIRMWARE_SETTINGS = (
    FeatureEntry("Firmware/CPU setting", "ELF firmware", "Set the firmware ELF and FastDyn runtime library.",
                 '[[CPU.cpu0]]\nbinary = "build/firmware.elf"\nplugin_library = "build/libfastdyn.so"\ninit_nsvtor = "0x08000000"',
                 "docs/Configuration.md"),
    FeatureEntry("Firmware/CPU setting", "start under GDB", "Add these settings to the existing [Machine] table. The GDB server uses port 1234; monitor_port is a separate QEMU monitor setting.",
                 '# Add inside the existing [Machine] table:\nenable_gdb = true\nstop_on_start = true',
                 "docs/Configuration.md"),
    FeatureEntry("Firmware/CPU setting", "x86-64 disk image", "Use a raw disk image instead of an ELF firmware image.",
                 '[[CPU.cpu0]]\narch = "x86_64"\nmachine = "base_generic"\ncpu = "qemu64"\ndrive_file = "disk.img"\ndrive_format = "raw"\nplugin_library = "build/libfastdyn.so"',
                 "docs/Configuration.md"),
)


_RUN_GUIDE = (
    FeatureEntry("Execution command", "run a configuration", "Launch QEMU using one FastDyn TOML configuration.",
                 "fastdyn run -c configs/target.toml -o fastdyn_work",
                 "docs/RunningFastDyn.md"),
    FeatureEntry("Execution command", "preserve a previous work directory", "Keep prior artifacts rather than resetting the selected work directory.",
                 "fastdyn run -c configs/target.toml -o fastdyn_work --persist-work-dir",
                 "docs/RunningFastDyn.md"),
    FeatureEntry("Execution command", "use an explicit SVD catalog", "Resolve a board platform against a specific CMSIS-SVD file or catalog.",
                 "fastdyn run -c configs/target.toml -s third_party/common/cmsis-svd-data",
                 "docs/RunningFastDyn.md"),
    FeatureEntry("Execution command", "control logging", "Show fewer or more log messages, for everything or one area, and save them to a file.",
                 "fastdyn -q run -c configs/target.toml                      # warnings and errors only\n"
                 "fastdyn -v run -c configs/target.toml                      # include debug messages\n"
                 "fastdyn --log-level fastdyn.targets=DEBUG run -c configs/target.toml\n"
                 "fastdyn --log-level fastdyn.native=ERROR run -c configs/target.toml  # QEMU plugin: errors only\n"
                 "fastdyn --log-file fastdyn_work/fastdyn.log run -c configs/target.toml",
                 "docs/RunningFastDyn.md"),
    FeatureEntry("Execution command", "build device models", "Compile device model sources (one .so per .c file) for the elder handler; only changed models are rebuilt.",
                 "fastdyn build-model models/usart2.c -o build/models              # one file\n"
                 "fastdyn build-model models/ -o build/models -r -j 8              # a whole tree, in parallel\n"
                 "fastdyn build-model --name spi4 models/spi4.c models/spi4_dma.c -o build/models\n"
                 "fastdyn build-model models/ -o build/models --keep-going --json  # report every failure",
                 "docs/RunningFastDyn.md"),
)


def _registered_entries(
    kind: str,
    definitions: dict[str, virtual_preprocessing.VirtualDefinition]
    | dict[str, virtual_preprocessing.RunDefinition],
) -> tuple[FeatureEntry, ...]:
    """Render public help declared by registered SDK definitions.

    Internal callbacks deliberately omit ``help`` and are not exposed here.
    This browser never supplies feature-specific fallback metadata.
    """
    entries = []
    for name, definition in sorted(definitions.items(), key=lambda item: item[0].casefold()):
        metadata = definition.help
        if metadata is None:
            continue
        entries.append(FeatureEntry(
            kind, name, metadata.description, metadata.toml, metadata.documentation,
        ))
    return tuple(entries)


def virtual_entries() -> tuple[FeatureEntry, ...]:
    """Return public virtuals declared through the preprocessing SDK."""
    return _registered_entries("Virtual instruction", virtual_preprocessing.VIRTUAL_DEFINITIONS)


def plugin_entries() -> tuple[FeatureEntry, ...]:
    """Return public run plugins declared through the preprocessing SDK."""
    return _registered_entries("Run-wide plugin", virtual_preprocessing.RUN_PREPROCESSORS)


def all_entries() -> tuple[FeatureEntry, ...]:
    return virtual_entries() + plugin_entries()


def modifier_entries() -> tuple[FeatureEntry, ...]:
    return _MODIFIERS


def device_model_entries() -> tuple[FeatureEntry, ...]:
    return _DEVICE_MODELS


def machine_entries() -> tuple[FeatureEntry, ...]:
    return _MACHINE_SETTINGS


def memory_entries() -> tuple[FeatureEntry, ...]:
    return _MEMORY_SETTINGS


def firmware_entries() -> tuple[FeatureEntry, ...]:
    return _FIRMWARE_SETTINGS


def run_entries() -> tuple[FeatureEntry, ...]:
    return _RUN_GUIDE


def _entry_menu(title: str, entries: tuple[FeatureEntry, ...]) -> Menu:
    choices = tuple((f"{entry.name}  —  {entry.description}", entry) for entry in entries)
    return Menu(title, choices, lambda entry: entry)


def _documentation_menu(title: str, entries: tuple[DocumentationEntry, ...]) -> Menu:
    choices = tuple((f"{entry.path}  —  {entry.description}", entry) for entry in entries)
    return Menu(title, choices, lambda entry: entry)


def _feature_documentation(entries: tuple[FeatureEntry, ...]) -> tuple[DocumentationEntry, ...]:
    """Build contextual documentation choices from SDK-provided feature help."""
    docs: dict[str, DocumentationEntry] = {}
    for entry in entries:
        docs.setdefault(entry.documentation, DocumentationEntry(entry.documentation, entry.description))
    return tuple(docs[path] for path in sorted(docs, key=str.casefold))


def _settings_menu(title: str, entries: tuple[FeatureEntry, ...], docs: tuple[DocumentationEntry, ...]) -> Menu:
    choices = _entry_menu(title, entries).choices + (
        ("Documentation", _documentation_menu(f"{title}  ›  documentation", docs)),
    )
    return Menu(title, choices, lambda item: item)


def build_feature_browser() -> Menu:
    """Build the top-level virtual/plugin picker."""
    virtuals = virtual_entries()
    plugins = plugin_entries()
    virtual_menu = _entry_menu("FastDyn virtuals  ›  instruction", virtuals)
    plugin_menu = _entry_menu("FastDyn plugins  ›  run-wide plugin", plugins)
    choices = (
        (f"Virtual instructions  ({len(virtuals)})", virtual_menu),
        (f"Run-wide plugins  ({len(plugin_menu.choices)})", plugin_menu),
        ("Documentation", _documentation_menu(
            "FastDyn virtuals and plugins  ›  documentation",
            _feature_documentation(virtuals + plugins),
        )),
    )
    return Menu("FastDyn virtuals and plugins", choices, lambda item: item)


def browse_features() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_feature_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None


def build_modifier_browser() -> Menu:
    choices = _entry_menu("FastDyn modifiers", _MODIFIERS).choices + (
        ("Documentation", _documentation_menu("FastDyn modifiers  ›  documentation", (
            DocumentationEntry("docs/VirtualsAndModifiers.md", "Modifier grammar and examples"),
            DocumentationEntry("docs/Configuration.md", "CPU configuration reference"),
        ))),
    )
    return Menu("FastDyn modifiers", choices, lambda item: item)


def browse_modifiers() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_modifier_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None


def build_device_model_browser() -> Menu:
    choices = _entry_menu("FastDyn device models", _DEVICE_MODELS).choices + (
        ("Documentation", _documentation_menu("FastDyn device models  ›  documentation", (
            DocumentationEntry("docs/FastDynDeviceModel.md", "Device-model overview"),
            DocumentationEntry("docs/ClassicDeviceModel.md", "Classic model"),
            DocumentationEntry("docs/PassthroughDeviceModel.md", "Hardware passthrough"),
            DocumentationEntry("docs/ElderScrollDeviceModel.md", "ElderScroll model"),
        ))),
    )
    return Menu("FastDyn device models", choices, lambda item: item)


def browse_device_models() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_device_model_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None


def build_machine_browser() -> Menu:
    return _settings_menu("FastDyn machine settings", _MACHINE_SETTINGS, (
        DocumentationEntry("docs/Configuration.md", "Machine and QEMU settings"),
        DocumentationEntry("docs/BuildingAConfig.md", "Build a first runnable configuration"),
    ))


def browse_machine() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_machine_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None


def build_memory_browser() -> Menu:
    return _settings_menu("FastDyn memory", _MEMORY_SETTINGS, (
        DocumentationEntry("docs/Configuration.md", "Memory-bank configuration"),
        DocumentationEntry("docs/BuildingAConfig.md", "Set memory for a first runnable configuration"),
    ))


def browse_memory() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_memory_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None


def build_firmware_browser() -> Menu:
    return _settings_menu("FastDyn firmware and CPU", _FIRMWARE_SETTINGS, (
        DocumentationEntry("docs/Configuration.md", "CPU and firmware configuration"),
        DocumentationEntry("docs/BuildingAConfig.md", "Set a firmware binary and entry/vector address"),
    ))


def browse_firmware() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_firmware_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None


def build_run_browser() -> Menu:
    return _settings_menu("Run FastDyn", _RUN_GUIDE, (
        DocumentationEntry("docs/RunningFastDyn.md", "Run workflow, flags, and generated artifacts"),
        DocumentationEntry("docs/BuildingAConfig.md", "Build a configuration before running it"),
    ))


def browse_run() -> FeatureEntry | DocumentationEntry | None:
    selected = browse_menu(build_run_browser())
    return selected if isinstance(selected, (FeatureEntry, DocumentationEntry)) else None
