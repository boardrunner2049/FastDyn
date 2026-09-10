'''
This package can be used independently as well to include as a tool.
See test/fastdyn_package/example.py
'''
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union, Sequence
import os, sys, pathlib
import subprocess
import logging

from .utils import helper
from . import timing

from .utils import parse_config as parse_helper

from .machine import VirtualInstruction

from . import fastdyn_log as fastdyn_log_conf
log = logging.getLogger(__name__)
fastdyn_log = fastdyn_log_conf.getFastdynLogger()

#supported targets
from .targets import qemu_target

class Fastdyn:
    def __init__(self):
        self.machines = {}

    def create_machine(self, machine_name, platform):
        machine = Machine(machine_name, platform)
        self.machines[machine_name] = machine

        return machine

    def run(self, target, machine_name, out_path=None):
        machine = self.machines[machine_name]
        if target.lower() == "qemu":
            # Firmware-wide modules prepare
            # declarative virtual rules and artifacts here.  The QEMU
            # target later sends every generated and user rule through the
            # same per-virtual preparation pipeline.
            from .virtual_preprocessing import prepare_run_preprocessors

            prepare_run_preprocessors(machine, out_path or "fastdyn_work")

        #before running resolve the device models for each machine
        with timing.phase(f"{machine_name}.compile_device_routing"):
            compile_device_routing(machine.devices, machine.parsed_device, models=machine.models) #return value will be written to machine.parsed_device

        if target.lower() == "qemu":
            try:
                python_endpoints = _python_endpoints_for_machine(machine)
                #write device config to the json file
                with timing.phase(f"{machine_name}.qemu_setup"):
                    qemu_cmd, gdb_cmd, launch_gdb, binary = qemu_target.setup_qemu(
                        machine,
                        out_path
                    )

                with timing.phase(f"{machine_name}.qemu_execution"):
                    qemu_target.start_execution(
                        qemu_cmd,
                        launch_gdb,
                        gdb_cmd,
                        binary,
                        python_endpoints=python_endpoints,
                        exit_timeout_ms=machine.qemu_target_opts.exit_timeout_ms,
                    )
            finally:
                for cleanup in reversed(getattr(machine, "preprocessing_cleanup", [])):
                    cleanup()


    def shutdown(self, machine_name=None):
        machines = [self.machines[machine_name]] if machine_name is not None else self.machines.values()
        for machine in machines:
            port = getattr(machine.qemu_target_opts, "monitor_port", None)
            if port is not None:
                qemu_target.kill_qemu_process(port=port)


def _python_endpoints_for_machine(machine):
    endpoints = []
    modeling_dir = Path(getattr(machine, "modeling_dir", "boardrunner/boardrunner_sdk/model")).expanduser()
    if not modeling_dir.is_absolute():
        modeling_dir = Path.cwd() / modeling_dir

    for dev_name, dev in machine.devices.items():
        for conn in getattr(dev, "connections", []) or []:
            if not isinstance(conn, dict):
                continue
            if str(conn.get("type", "")).lower().strip() != "endpoint":
                continue
            if conn.get("enabled", True) is False:
                continue

            name = str(conn.get("name", "")).strip()
            if not name.endswith(".py"):
                continue

            endpoint_path = Path(name).expanduser()
            if not endpoint_path.is_absolute():
                endpoint_path = modeling_dir / endpoint_path
            if endpoint_path.exists():
                endpoints.append((str(endpoint_path), dev_name))
            else:
                log.warning("Python endpoint configured but not found: %s", endpoint_path)

    return endpoints

class QemuTargetOpts:
    def __init__(self):
        #initialize with default values
        self.qemu_path: str = "qemu-system-arm"
        self.finline: Optional[str] = None
        self.coverage: bool = False
        self.fuzzing: bool = False
        self.fuzzing_schema: Optional[str] = None
        self.edge_coverage: bool = False
        self.enable_gdb: bool = False
        self.gdb_port: int = 1234
        self.stop_on_start: bool = False
        self.launch_gdb: bool = False
        self.semihosting: bool = True
        self.semihosting_config: str = "enable=on,target=native"
        self.monitor_port: Optional[int] = 5555
        self.qmp_socket: Optional[str] = "/tmp/qmp.sock"
        # Halt exactly on a co-simulation budget deadline rather than at the
        # first translation-block boundary after it. See docs/CoSimulationSlave.md.
        self.exact_budget_stop: bool = False
        self.exit_timeout_ms: int = 5000
        self.icount: Optional[str] = None
        self.timer_irq_period_ns: Optional[int] = None
        self.print_command: bool = False
        self.reset_memory_files: bool = False
        self.probe_run: bool = False
        self.probe_faults: Optional[str] = None
        self.probe_milestones: Optional[str] = None
        self.probe_ignores: Optional[str] = None
        self.probe_out_dir: Optional[str] = None
        self.print_command = False
        self.savestate_extra_ranges: list[tuple[int, int]] = []

class Machine:
    def __init__(self, machine_name, platform_name):
        self.name: Optional[str] = machine_name
        self.cpus = []
        self.memories = {}
        self.devices = {}
        self.models = {}
        self.fmu_name: Optional[str] = None
        self.fmu_path: Optional[str] = None
        self.fmu_instantiation_token: Optional[str] = None
        self.fmu_resource_path: Optional[str] = None
        self.fmu_parameters: Dict[str, float] = {}
        self.fmu_value_references: Dict[str, int] = {}
        self.platform = platform_name
        self.qemu_target_opts = QemuTargetOpts()

        #optional params -- useful for svd
        self.irq_map = {}
        self.svd_file: Optional[str] = None
        self.svd_key: Optional[str] = None
        self.svd_device = None
        self.static_analysis_cache_dir: Optional[str] = None
        self.static_analysis_macros: dict[str, Any] = {}
        self.firmware_source_roots: list[str] = []
        self.modeling_dir: str = "boardrunner/boardrunner_sdk/model"
        self.milestones: list[str] = []
        self.ignore_functions: list[str] = []

        self.parsed_device = {}            #internal to the machine for qemu understanding
        self.generated_virtual_rules: dict[int, list] = {}
        self.preprocessing_artifacts: list[Path] = []
        self.preprocessing_cleanup: list = []
        self.preprocessing_prepared: bool = False
        # Optional native-build or host-provided capabilities consumed by the
        # public virtual preprocessing SDK (for example, "fuzzing" or "fmu").
        self.virtual_capabilities: set[str] = set()

    def add_cpu(self, arch, machine, cpu, binary, init_nsvtor, twintrace, hardware_trace, exstng_config_path):
        cpu = CPU(arch, machine, cpu, binary, init_nsvtor, twintrace, hardware_trace, exstng_config_path, self) #pass parent for easy referencing to objs like irq_map
        self.cpus.append(cpu)
        return cpu

    def add_memory(self, memory_name, memory_id, memory_start, memory_size, memory_type, backend, memory_file, share=True):
        if memory_name in self.memories:
            raise KeyError(f"Unable to create a memory with name {memory_name}. Create a memory with a different name")

        #initial check to make sure main memory is added before optional rams
        if memory_name != "main" and len(self.memories) == 0:
            raise KeyError(f"Unable to create memory {memory_name}. First Create main memory")

        memory = Memory(memory_name, memory_id)

        if not memory.validate_and_add_memory(memory_start, memory_size, memory_type, backend, memory_file, share):
            raise ValueError(f"Unable to Create Memory: {memory_name}")

        self.memories[memory_name] = memory
        return memory

    def add_device(self, name):
        if name in self.devices:
            raise ValueError(f"Device with name {name} already attached")
        device = Device(name, self) #pass parent for easy referencing to objs like irq_map
        self.devices[name] = device

        return device

    def add_model(self, name, backend=None):
        if name in self.models:
            raise ValueError(f"Model with name: {name} already initialized")
        self.models[name] = {"backend": backend}

        return True

    def add_rehosting_info(self, rehosting_info=None):
        rehosting_info = rehosting_info or {}
        rehosting_dirs = rehosting_info.get("directories", {}) or {}

        firmware_source_roots = rehosting_dirs.get(
            "firmware_source_roots",
            rehosting_dirs.get(
                "source_roots",
                rehosting_dirs.get("source_code_dir"),
            ),
        )
        if firmware_source_roots in (None, ""):
            firmware_source_roots = []
        elif not isinstance(firmware_source_roots, list):
            firmware_source_roots = [firmware_source_roots]

        self.static_analysis_cache_dir = rehosting_dirs.get(
            "static_analysis_cache_dir",
            "fastdyn_static",
        )
        self.firmware_source_roots = [str(path) for path in firmware_source_roots]

        static_analysis_info = rehosting_info.get("static_analysis", {}) or {}
        self.static_analysis_macros = static_analysis_info.get("macros", {}) or {}
        self.modeling_dir = rehosting_dirs.get(
            "modeling_dir",
            self.modeling_dir,
        )

        return True

    #TODO: Update this
    def list_devices(self):
        pass

    def add_cmsis_svd(self, cmsis_svd):
        platform = (self.platform or "").strip()
        if not platform or platform.lower() in ("intel", "x86", "x86_64", "base_generic", "pc", "q35", "riscv", "riscv32", "riscv64", "virt", "spike"):
            fastdyn_log.info(f"Skipping CMSIS SVD resolution for non-ARM platform '{platform}'.")
            return

        fastdyn_log.info("Parsing the passed path for the CMSIS SVD")

        try:
            svd_file, svd_key = parse_helper.resolve_svd(
                platform_or_board=platform or "unused",
                svd=cmsis_svd,          # user explicitly passed it here
                default_dir=None,       # no default in this path
                auto_discover=False,    # don’t do repo search when user already gave a path
            )
        except parse_helper.SvdResolutionError as e:
            if platform.lower().startswith("generic-"):
                fastdyn_log.info(
                    f"Skipping CMSIS SVD resolution for generic platform '{platform}'."
                )
            else:
                fastdyn_log.warning(
                    f"Could not resolve CMSIS SVD for platform '{platform}': {e}"
                )
            return

        fastdyn_log.info(f"Using SVD: {svd_file} (key='{svd_key}')")
        self.svd_file = svd_file
        self.svd_key = svd_key
        svd_device = parse_helper.get_svd_device(svd_file)
        self.svd_device = svd_device

        fastdyn_log.info("Creating IRQ Map using the CMSIS SVD")
        self.irq_map = parse_helper.create_svd_irq_map(svd_device)
        return True

@dataclass
class InstructionModifier:
    """Runtime instruction-level patch."""
    at: int
    patch: str

class CPU:
    def __init__(self, arch, machine, cpu, binary, init_nsvtor, twintrace, hardware_trace, exstng_config_path, machine_obj):
        """One CPU instance belonging to a machine."""
        self.arch = arch
        self.machine = machine
        self.cpu = cpu
        self.binary = binary
        self.init_nsvtor: int = init_nsvtor
        self.twintrace = twintrace       #supported options are record, replay or None
        self.hardware_trace = hardware_trace    # hardware log needed in case of replay
        self.plugin_config: dict[str, dict] = {}
        self.machine_obj = machine_obj
        self.exstng_config_path = exstng_config_path

        self.plugin_library: Optional[str] = "build/libfastdyn.so"
        self.monitor_elf: Optional[str] = None

        self.log_file: Optional[str] = "qemu.log"
        self.log_options: Optional[str] = "in_asm,op"

        self.virtuals = []
        self.modifiers = []
        self.logger_content: Optional[str] = """\
                                            # --- Plugin Logger Configuration ---
                                            # level = DEBUG
                                            # output = stderr
                                            """
        self.symbol_dict = {}



    def add_virtual_instruction(self, vi: Union["VirtualInstruction", str, Sequence[Union["VirtualInstruction", str]]]) -> bool:
        items = vi if isinstance(vi, (list, tuple)) else [vi]

        out: list[str] = []
        for item in items:
            ok, parsed = parse_helper.parse_vi(item)
            if not ok or parsed is None:
                fastdyn_log.error("Unable to parse Virtual Instruction")
                return False

            # Keep rules declarative until run preparation. This preserves the
            # original arguments for a virtual-specific preprocessor (for
            # example, symbolic Cortex-M IRQ names) and makes user-generated
            # and run-generated rules share one pipeline.
            out.append(parse_helper.vi_to_string(parsed))

        self.virtuals.extend(out)
        return True

    def add_modifier(
        self,
        mod: Union["InstructionModifier", str, Sequence[Union["InstructionModifier", str]]]
    ) -> bool:
        items = mod if isinstance(mod, (list, tuple)) else [mod]

        out: list[str] = []
        for item in items:
            ok, parsed = parse_helper.parse_mod(item)
            if not ok or parsed is None:
                fastdyn_log.error("Unable to parse Instruction Modifier")
                return False

            try:
                resolved = parse_helper.resolve_mod(parsed, symbol_map=self.symbol_dict)
            except Exception as e:
                fastdyn_log.error(f"Unable to resolve Instruction Modifier: {e}")
                return False

            out.append(parse_helper.mod_to_string(resolved))

        self.modifiers.extend(out)
        return True

    def update_cpu_param(self, param, val):
        if not hasattr(self, param):
            fastdyn_log.error(f"Unknown CPUConfig field: {param}")
            return False
        try:
            setattr(self, param, val)
            return True
        except (AttributeError, TypeError, ValueError) as e:
            fastdyn_log.error(f"Unable to set '{param}' to {val!r}: {e}")
            return False

@dataclass
class DeviceHandler:
    model: str = ""                           # "qemu" | "elder" | "passthrough" | ...
    enabled: bool = True
    type: Optional[str] = None           # e.g., "stm32f2xx-usart"
    args: Optional[Any] = None           # could be str/list/dict depending on your parser
    scroll: Optional[str] = None         # path to .so (your example uses this for elder)

@dataclass
class Slave:
    device: str                         # The device ID/name
    model: list[str]                    # MUST be a list (e.g., ["elder"])
    params: Dict[str, Any] = field(default_factory=dict)
    device_scroll: Optional[str] = None # The path/identifier for the slave

class Device:
    def __init__(self, name, machine_obj):
        self.device_name = name
        self.machine_obj = machine_obj
        self.supported_ranges = []
        self.handlers = []
        self.irq_range = []
        self.description: str = ""
        self.connections: list = []
        #here just add ranges and validate them before running qemu (transformation step)

    def add_ranges(self, start, end=None, size=None):
        """
        Add a supported MMIO range for this device.

        Accepts:
        - start/end as int or str (hex like "0x40000000" or decimal like "1073741824")
        - size as int (bytes) or str with suffix: "B", "K"/"KB", "M"/"MB", "G"/"GB" (binary units, i.e., 1KB=1024B)

        Stores ranges internally as inclusive [start, end] integers.
        """
        import re

        def _parse_addr(x):
            if isinstance(x, int):
                return x
            if isinstance(x, str):
                return int(x.strip().lower(), 0)  # base=0 supports 0x.. and decimals
            raise TypeError(f"Address must be int or str, got {type(x)}")

        def _parse_size(x):
            if isinstance(x, int):
                if x < 0:
                    raise ValueError("size must be >= 0")
                return x
            if not isinstance(x, str):
                raise TypeError(f"size must be int or str, got {type(x)}")

            s = x.strip().lower()
            m = re.fullmatch(r"(\d+)\s*(b|kb|k|mb|m|gb|g)?", s)
            if not m:
                raise ValueError(f"Invalid size string: {x!r} (examples: '512K', '4MB', '1024')")

            val = int(m.group(1))
            unit = m.group(2) or "b"
            scale = {
                "b": 1,
                "k": 1024, "kb": 1024,
                "m": 1024**2, "mb": 1024**2,
                "g": 1024**3, "gb": 1024**3,
            }[unit]
            return val * scale

        if start is None:
            raise TypeError("start must not be None")
        if (end is None) == (size is None):
            raise TypeError("Provide exactly one of: end or size")

        start_i = _parse_addr(start)

        if end is not None:
            end_i = _parse_addr(end)
        else:
            size_i = _parse_size(size)
            if size_i <= 0:
                raise ValueError("size must be > 0")
            end_i = start_i + size_i - 1  # inclusive end

        if end_i < start_i:
            raise ValueError(f"Invalid range: end ({hex(end_i)}) < start ({hex(start_i)})")

        # store as inclusive integer range
        self.supported_ranges.append([start_i, end_i])
        return True

    def add_handler(self, name, enabled=True, args=None, scroll=None, type=None):
        handler_obj = DeviceHandler(
            model=str(name),       #required
            enabled=bool(enabled),  #required
            type=None if type is None else str(type),
            args=None if args is None else str(args),
            scroll=None if scroll is None else str(scroll),
        )
        self.handlers.append(handler_obj)
        return True

    def add_irq(self, irq):
        """
        Add IRQ(s) for this device.

        Supported inputs:
        - int: 53
        - str: "53", "0x35"  (numeric)
        - str: "USART1_IRQn" (symbol; resolved via machine_obj.irq_map)
        - [start, end]: inclusive range, where start/end can be int/str/symbol

        Behavior:
        - self.irq_range is a flat list of ints
        - add_irq(1) -> [1]
        - add_irq([2, 4]) -> extends with [2,3,4]
        - avoids duplicates (keeps list clean), keeps sorted ascending
        """
        def _resolve_one(x) -> int:
            # int directly
            if isinstance(x, int):
                return x

            # strings: numeric first, then symbol lookup
            if isinstance(x, str):
                s = x.strip()
                # numeric? (supports "53" and "0x35")
                try:
                    return int(s, 0)
                except ValueError:
                    pass

                # symbol: require irq_map only in this case
                irq_map = getattr(self.machine_obj, "irq_map", None)
                if not irq_map:
                    raise RuntimeError(
                        "IRQ symbol provided but machine_obj.irq_map is empty/unset. "
                        "Call machine.add_cmsis_svd(...) before using symbolic IRQ names."
                    )

                # support both {name->num} and {num->name}
                if s in irq_map and isinstance(irq_map[s], int):
                    return int(irq_map[s])

                for k, v in irq_map.items():
                    if v == s and isinstance(k, int):
                        return int(k)

                raise KeyError(f"IRQ symbol {s!r} not found in machine_obj.irq_map")

            raise TypeError(f"IRQ must be int, str, or a 2-item [start,end] list; got {type(x)}")

        # ensure storage exists
        if not hasattr(self, "irq_range") or self.irq_range is None:
            self.irq_range = []

        # internal dedup set (optional but keeps list clean and fast)
        irq_set = set(self.irq_range)

        def _add_value(v: int):
            if v < 0:
                raise ValueError("IRQ numbers must be >= 0")
            if v not in irq_set:
                self.irq_range.append(v)
                irq_set.add(v)

        # parse input
        if isinstance(irq, (int, str)):
            _add_value(_resolve_one(irq))

        elif isinstance(irq, (list, tuple)):
            if len(irq) != 2:
                raise TypeError("IRQ range must be a 2-item list/tuple like [start, end]")
            start = _resolve_one(irq[0])
            end   = _resolve_one(irq[1])

            if start < 0 or end < 0:
                raise ValueError("IRQ numbers must be >= 0")
            if end < start:
                raise ValueError(f"Invalid IRQ range: end ({end}) < start ({start})")

            for v in range(start, end + 1):
                _add_value(v)

        else:
            raise TypeError(f"IRQ must be int/str or a 2-item [start,end] list; got {type(irq)}")

        # keep deterministic order
        self.irq_range.sort()
        return True

    def add_connection(self, conn_data: dict) -> None:
        if conn_data and isinstance(conn_data, dict):
            self.connections.append(conn_data)

    def add_slave(self, slave_data: dict):
            """
            Expects a dictionary containing at least 'device' and 'model'.
            All other keys (except 'device_scroll') are moved into 'params'.
            """
            if not hasattr(self, 'slaves') or self.slaves is None:
                self.slaves = []

            # 1. Extract metadata
            device_id = slave_data.get('device')
            models = slave_data.get('model', [])
            scroll = slave_data.get('device_scroll')

            # 2. Extract everything else into params
            # This takes every key in the TOML entry EXCEPT the metadata ones
            metadata_keys = {'device', 'model', 'device_scroll'}
            params = {k: v for k, v in slave_data.items() if k not in metadata_keys}

            # 3. Create and store the object
            new_slave = Slave(
                device=str(device_id),
                model=list(models) if isinstance(models, list) else [models],
                params=params,  # Now contains 'address', 'cs_id', etc.
                device_scroll=scroll
            )
            self.slaves.append(new_slave)
            return True

class MemoryType(Enum):
    SRAM = "SRAM"
    MMIO = "MMIO"
    FLASH = "FLASH"
    DRAM = "DRAM"
    RAM = "RAM"
    SYSTEM = "SYSTEM"

class BackendType(Enum):
    FILE = "FILE"
    RAM = "RAM"
    MEMFD = "MEMFD"

class Memory:
    def __init__(self, memory_name, memory_id):
        self.memory_name: str = memory_name
        self.memory_id: str = memory_id
        self.memory_start: str = ""
        self.memory_size: str = ""
        self.memory_backend: BackendType
        self.memory_file: str = ""
        self.memory_type: MemoryType
        self.memory_share: bool = True

    def validate_and_add_memory(self, start, size, mem_type, mem_backend, memory_file, share):
        self.memory_start = start
        self.memory_size = size
        self.memory_share = share
        if isinstance(mem_type, str):
            try:
                self.memory_type = MemoryType[mem_type.upper()]
            except KeyError:
                valid = ", ".join([m.name for m in MemoryType])
                raise ValueError(f"Unknown memory type {mem_type!r}. Valid: {valid}")
        elif not isinstance(mem_type, MemoryType):
            raise TypeError(f"mem_type must be MemoryType or str, got {type(mem_type).__name__}")

        if isinstance(mem_backend, str):
            try:
                self.memory_backend = BackendType[mem_backend.upper()]
            except KeyError:
                valid = ", ".join([m.name for m in BackendType])
                raise ValueError(f"Unknown backend type {mem_backend!r}. Valid: {valid}")
        elif not isinstance(mem_backend, BackendType):
            raise TypeError(f"mem_type must be MemoryType or str, got {type(mem_backend).__name__}")

        # if not os.path.exists(memory_file):
        #     raise ValueError(f"memory file: {memory_file} does not exist")

        self.memory_file = memory_file

        return True

# =============================================================================
# Devices/Peripherals Information
# This api transforms the DeviceSection to Fastdyn understandable device format
# =============================================================================

import os
from typing import Any, Dict, Optional

def compile_device_routing(
    devices: Dict[str, Any],
    parsed_device: Dict[str, Any],
    *,
    models: Optional[Dict[str, Any]] = None,
    include_models: Optional[set[str]] = None,
    check_scroll_paths: bool = True,
) -> bool:
    """
    Transform {dev_name: Device} into model-centric routing JSON and write it into parsed_device.

    devices: machine.devices
    parsed_device: machine.parsed_device (dict that will be cleared+updated)
    models: machine.models (optional; used only to set routing[model]["backend"])
    """
    routing: Dict[str, Any] = {}
    builtin_qemu: Dict[str, Any] = {}

    def _dedup_preserve_order(seq):
        seen = set()
        out = []
        for x in seq:
            # handle unhashables
            key = x if isinstance(x, (str, int, float, tuple)) else repr(x)
            if key in seen:
                continue
            seen.add(key)
            out.append(x)
        return out

    def _model_backend(model: str):
        if not models:
            return None
        md = models.get(model)
        if isinstance(md, dict):
            return md.get("backend")
        return None

    # -----------------------------
    # Pass 1: group devices by enabled handler model
    # -----------------------------
    for dev_name, dev in devices.items():
        ranges = list(getattr(dev, "supported_ranges", []) or [])
        irq = getattr(dev, "irq_range", None)

        for h in getattr(dev, "handlers", []) or []:
            if getattr(h, "enabled", True) is False:
                continue

            model = getattr(h, "model", None)
            if not model:
                continue

            # QEMU builtins are separated
            if model == "qemu":
                h_type = getattr(h, "type", None)
                if not h_type:
                    raise ValueError(f"[Device.{dev_name}] qemu handler enabled but missing 'type'")
                builtin_qemu.setdefault(h_type, {})
                h_args = getattr(h, "args", None)
                if h_args is not None:
                    builtin_qemu[h_type]["args"] = h_args
                continue

            if include_models is not None and model not in include_models:
                continue

            bucket = routing.setdefault(model, {"overall": []})

            bucket["overall"].extend(ranges)

            entry = bucket.setdefault(dev_name, {})
            entry["range"] = list(ranges)

            if irq:
                entry["irq"] = irq

            # elder needs scroll
            if model == "elder":
                scroll = getattr(h, "scroll", None)
                if not scroll:
                    raise ValueError(f"[Device.{dev_name}] elder handler enabled but missing 'scroll'")
                entry["scroll"] = scroll

    # add backend + dedup overall ranges
    for model, bucket in routing.items():
        bucket["overall"] = _dedup_preserve_order(bucket["overall"])
        bucket["backend"] = _model_backend(model)

    # -----------------------------
    # Pass 2: slaves and bus-attached connections.
    # Endpoint connections are host-side helpers and are not forwarded to QEMU.
    # -----------------------------
    for dev_name, dev in devices.items():
        slaves = getattr(dev, "slaves", None)
        for slave in slaves or []:
            for model in getattr(slave, "model", []) or []:
                if include_models is not None and model not in include_models:
                    continue

                if model not in routing or dev_name not in routing[model]:
                    continue

                routing[model][dev_name].setdefault("slaves", {})
                slaves_out = routing[model][dev_name]["slaves"]

                params = getattr(slave, "params", {}) or {}
                s_cfg: Dict[str, Any] = dict(params)

                device_scroll = getattr(slave, "device_scroll", None)
                if device_scroll:
                    if check_scroll_paths and os.path.exists(device_scroll):
                        s_cfg["scroll_path"] = device_scroll
                        s_cfg["is_scroll_path"] = True
                    else:
                        s_cfg["is_scroll_path"] = False
                else:
                    s_cfg["is_scroll_path"] = False

                slaves_out[getattr(slave, "device")] = s_cfg

        for conn in getattr(dev, "connections", []) or []:
            if not isinstance(conn, dict):
                continue
            if conn.get("enabled", True) is False:
                continue
            if str(conn.get("type", "")).lower().strip() != "slave":
                continue

            conn_models = conn.get("model")
            if conn_models is None:
                conn_models = [
                    model for model, bucket in routing.items()
                    if dev_name in bucket
                ]
            elif isinstance(conn_models, str):
                conn_models = [conn_models]

            for model in conn_models:
                if include_models is not None and model not in include_models:
                    continue
                if model not in routing or dev_name not in routing[model]:
                    continue

                routing[model][dev_name].setdefault("slaves", {})
                slaves_out = routing[model][dev_name]["slaves"]

                meta_keys = {"type", "model", "enabled", "device_or_topic"}
                s_cfg: Dict[str, Any] = {
                    k: v for k, v in conn.items() if k not in meta_keys
                }

                device_scroll = conn.get("device_scroll")
                if device_scroll:
                    if check_scroll_paths and os.path.exists(device_scroll):
                        s_cfg["scroll_path"] = device_scroll
                        s_cfg["is_scroll_path"] = True
                    else:
                        s_cfg["is_scroll_path"] = False
                else:
                    s_cfg["is_scroll_path"] = False

                slave_key = (
                    conn.get("name")
                    or conn.get("device_or_topic")
                    or f"{dev_name}_conn_{len(slaves_out)}"
                )
                slaves_out[slave_key] = s_cfg

    # write to output dict (your current pattern)
    parsed_device.clear()
    parsed_device.update(routing)

    return True
