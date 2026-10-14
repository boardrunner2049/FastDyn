import sys
import tomli
import logging
import os
from pathlib import Path

from fastdyn.fastdyn import *
from . import fmu_build, fmu_runtime
from . import paths

log = logging.getLogger(__name__)


def _env_int(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _env_bool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    text = value.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", "none"):
        return False
    raise ValueError(f"{name} must be a boolean, got {value!r}")


def _positive_int(value, setting):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{setting} must be a positive integer, got {value!r}")
    return value


def _range_integer(value, setting, *, positive=False):
    if isinstance(value, bool):
        raise TypeError(f"{setting} must be an integer or integer string, got {value!r}")

    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0)
        except ValueError as exc:
            raise ValueError(f"{setting} must be an integer, got {value!r}") from exc
    else:
        raise TypeError(f"{setting} must be an integer or integer string, got {value!r}")

    if result < 0 or (positive and result == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{setting} must be {qualifier}, got {value!r}")
    return result


def _savestate_extra_ranges(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("[Machine].savestate_extra_ranges must be an array of tables")

    ranges = []
    for index, entry in enumerate(value):
        setting = f"[Machine].savestate_extra_ranges[{index}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{setting} must be an inline table")
        if "start" not in entry or "size" not in entry:
            raise ValueError(f"{setting} requires start and size")

        start = _range_integer(entry["start"], f"{setting}.start")
        size = _range_integer(entry["size"], f"{setting}.size", positive=True)
        if start + size - 1 > 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f"{setting} exceeds the address space")
        ranges.append((start, size))

    return ranges


def _memory_file_for(machine_name, memory_name, memory_info):
    memory_file = memory_info["memory_file"]
    memory_dir = os.environ.get("FASTDYN_QEMU_MEMORY_DIR")
    if not memory_dir:
        return memory_file
    if str(memory_info.get("backend", "")).lower() != "file":
        return memory_file

    memory_id = str(memory_info.get("id", memory_name))
    filename = f"{machine_name}_{memory_name}_{memory_id}.bin"
    return str(Path(memory_dir).expanduser() / filename)


def _resolve_existing_config_path(config_path, value):
    if value in (None, "", False):
        return False

    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return str(path)

    config_path = Path(config_path).expanduser().resolve()
    bases = [
        config_path.parent,
        Path.cwd(),
    ]

    try:
        bases.append(fmu_build.find_repo_root(config_path))
    except Exception:
        pass

    for base in bases:
        candidate = (base / path).resolve()
        if candidate.exists():
            return str(candidate)

    return str((bases[-1] / path).resolve())


def _config_relative(config_path, value):
    """A file named in the configuration (firmware, trace, model library):
    a relative path is looked up next to the configuration file first; if it
    is not there, it stays relative to the current directory, as before."""
    if not isinstance(value, str) or not value:
        return value
    path = Path(value).expanduser()
    if path.is_absolute():
        return value
    candidate = Path(config_path).expanduser().resolve().parent / path
    return str(candidate) if candidate.exists() else value


def _slaves_config_relative(config_path, slaves):
    """Slave entries with their device_scroll resolved like other model libraries."""
    resolved = []
    for slave in slaves:
        if isinstance(slave, dict) and "device_scroll" in slave:
            slave = {**slave, "device_scroll": _config_relative(config_path, slave["device_scroll"])}
        resolved.append(slave)
    return resolved


def _endpoint_config_relative(config_path, conn):
    """A host endpoint connection with a Python helper (``name`` ending in
    ``.py``) resolved like other files named in the configuration."""
    if (isinstance(conn, dict)
            and str(conn.get("type", "")).lower().strip() == "endpoint"
            and str(conn.get("name", "")).strip().endswith(".py")):
        conn = {**conn, "name": _config_relative(config_path, str(conn["name"]).strip())}
    return conn


def _format_icount_option(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "auto" if value else "none"
    if not isinstance(value, dict):
        raise TypeError("[Machine].icount must be a string, boolean, or inline table")

    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TypeError("[Machine].icount.enabled must be true or false")
    if not enabled:
        return "none"

    parts = []
    shift = value.get("shift")
    if shift is not None:
        if isinstance(shift, int) and not isinstance(shift, bool):
            if shift < 0:
                raise ValueError("[Machine].icount.shift must be non-negative")
            parts.append(f"shift={shift}")
        elif isinstance(shift, str) and shift:
            parts.append(f"shift={shift}")
        else:
            raise TypeError("[Machine].icount.shift must be an integer or string")

    for key in ("sleep", "align"):
        setting = value.get(key)
        if setting is None:
            continue
        if not isinstance(setting, bool):
            raise TypeError(f"[Machine].icount.{key} must be true or false")
        parts.append(f"{key}={'on' if setting else 'off'}")

    extra = value.get("extra")
    if extra is not None:
        if isinstance(extra, str):
            parts.append(extra)
        elif isinstance(extra, list) and all(isinstance(item, str) for item in extra):
            parts.extend(extra)
        else:
            raise TypeError("[Machine].icount.extra must be a string or list of strings")

    return ",".join(parts) if parts else "auto"


def load_toml_config(config_path):
    try:
        with open(config_path, "rb") as f:
            return tomli.load(f)
    except FileNotFoundError:
        log.error(f"The file '{config_path}' was not found.")
        raise
    except tomli.TOMLDecodeError as e:
        log.error(f"Error: Failed to parse TOML file '{config_path}': {e}")
        raise


#parse a single toml per machine, use as many instances as you want in future to parse multiple machines
def parser(out_dir, machine_name, toml_config, svd_path, fmu_name=None, load_fmu=True):
    #parse the toml configuration
    log.info(f"Parsing Config file: {toml_config}")

    fastdyn_handle = Fastdyn()

    #parse the toml and the fastdyn_handle
    toml_parser = TomlParser(toml_config, fastdyn_handle)

    #right now, we support per machine config
    machine0 = fastdyn_handle.create_machine(machine_name=machine_name,
                                    platform=toml_parser.machine_info.get("platform")
                                    )

    if load_fmu:
        try:
            fmu = fmu_build.resolve(Path(toml_config), fmu_name)
            runtime = fmu_runtime.prepare_runtime(fmu, Path(out_dir or "fastdyn_work"))
            machine0.fmu_name = fmu.name
            machine0.fmu_path = str(runtime.library_path)
            machine0.fmu_token = runtime.instantiation_token
            machine0.fmu_resources = runtime.resource_path
            machine0.fmu_parameters = fmu.parameters or {}
            machine0.fmu_value_references = runtime.value_references
        except fmu_build.NoFmuConfig:
            pass

    # additional machine params if set by the user related to qemu target
    q = machine0.qemu_target_opts

    q.qemu_path          = toml_parser.machine_info.get("qemu_path", "")  # optional; see qemu_target.resolve_qemu_binary

    q.enable_gdb         = toml_parser.machine_info.get("enable_gdb", False)
    q.stop_on_start      = toml_parser.machine_info.get("stop_on_start", False)
    q.launch_gdb         = toml_parser.machine_info.get("launch_gdb", False)

    q.semihosting        = toml_parser.machine_info.get("semihosting", False)
    q.semihosting_config = toml_parser.machine_info.get("semihosting_config", "enable=on,target=native")

    q.monitor_port       = _env_int(
        "FASTDYN_MONITOR_PORT",
        toml_parser.machine_info.get("monitor_port", 5555),
    )
    q.qmp_socket         = os.environ.get(
        "FASTDYN_QMP_SOCKET",
        toml_parser.machine_info.get("qmp_socket", "/tmp/qmp.sock"),
    )
    q.exit_timeout_ms    = _positive_int(
        toml_parser.machine_info.get("exit_timeout_ms", 5000),
        "[Machine].exit_timeout_ms",
    )
    q.gdb_port           = _env_int(
        "FASTDYN_GDB_PORT",
        toml_parser.machine_info.get("gdb_port", 1234),
    )
    q.exact_budget_stop  = toml_parser.machine_info.get("exact_budget_stop", False)
    q.icount             = _format_icount_option(toml_parser.machine_info.get("icount", None))
    q.timer_irq_period_ns = toml_parser.machine_info.get("timer_irq_period_ns", None)

    q.display            = toml_parser.machine_info.get("display", "none")
    q.monitor            = toml_parser.machine_info.get("monitor", None)
    q.serial             = toml_parser.machine_info.get("serial", None)
    q.coverage           = _env_bool(
        "FASTDYN_COVERAGE",
        toml_parser.machine_info.get("coverage", False),
    )
    q.fuzzing            = _env_bool(
        "FASTDYN_FUZZING",
        toml_parser.machine_info.get("fuzzing", False),
    )
    fuzzing_schema = toml_parser.machine_info.get("fuzzing_schema")
    if fuzzing_schema is not None and not isinstance(fuzzing_schema, str):
        raise TypeError("[Machine].fuzzing_schema must be a path string")
    q.fuzzing_schema = _resolve_existing_config_path(toml_config, fuzzing_schema)
    q.edge_coverage      = _env_bool(
        "FASTDYN_EDGE_COVERAGE",
        toml_parser.machine_info.get("edge_coverage", False),
    )
    q.finline            = toml_parser.machine_info.get("finline", None)
    q.print_command       = toml_parser.machine_info.get("print_command", False)
    q.reset_memory_files  = toml_parser.machine_info.get("reset_memory_files", False)
    q.savestate_extra_ranges = _savestate_extra_ranges(
        toml_parser.machine_info.get("savestate_extra_ranges")
    )

    #add cmsis svd if Platform name provided by the user
    if toml_parser.machine_info.get("platform") is not None:
        machine0.add_cmsis_svd(cmsis_svd=svd_path)



    #add cpus information per machine
    cpus = []
    for idx, cpu in enumerate(toml_parser.cpus_info):
        curr_cpu = toml_parser.cpus_info[cpu][0]

        binary_path = _config_relative(toml_config, curr_cpu.get('binary')) or curr_cpu.get('drive_file') or ''
        cpu_obj = machine0.add_cpu(
                arch=curr_cpu.get("arch", "arm"),
                machine=curr_cpu.get("machine", "cortexm"),
                cpu=curr_cpu.get("cpu", "cortex-m4"),
                binary=binary_path,
                init_nsvtor= curr_cpu.get("init_nsvtor", None),
                twintrace = curr_cpu.get("twintrace", None),
                hardware_trace = _config_relative(toml_config, curr_cpu.get("hardware_trace", None)),
                exstng_config_path = _resolve_existing_config_path(
                    toml_config,
                    curr_cpu.get("existing_config_path", False),
                ),
                )

        # additional params if set by the user
        cpu_obj.drive_file     =   curr_cpu.get('drive_file', None)
        cpu_obj.drive_format   =   curr_cpu.get('drive_format', 'raw')
        cpu_obj.bios           =   curr_cpu.get('bios', None) or toml_parser.machine_info.get("bios", None)
        cpu_obj.plugin_library  =   curr_cpu.get('plugin_library', paths.default_plugin_library())
        cpu_obj.monitor_elf     =   curr_cpu.get('monitor_elf', '../ws/monitor.elf')
        cpu_obj.log_file        =   toml_parser.machine_info.get("log_file", curr_cpu.get("log_file", cpu_obj.log_file))
        cpu_obj.log_options     =   toml_parser.machine_info.get("log_options", curr_cpu.get("log_options", cpu_obj.log_options))
        cpu_obj.logger_content  =   curr_cpu.get("logger_content", cpu_obj.logger_content)

        # Run-wide modules are configured under a generic per-CPU namespace.
        # FastDyn preserves the table without knowing plugin names or settings.
        raw_plugins = curr_cpu.get("plugins", {})
        if not isinstance(raw_plugins, dict):
            raise TypeError("[CPU.cpu0.plugins] must be a table")
        plugin_config: dict[str, dict] = {}
        plugin_sources: dict[str, Path] = {}
        for plugin_name, plugin_settings in raw_plugins.items():
            if not isinstance(plugin_settings, dict):
                raise TypeError(
                    f"[CPU.cpu0.plugins.{plugin_name}] must be a table"
                )
            settings = dict(plugin_settings)
            # `source` names a plugin folder outside FastDyn; FastDyn uses it
            # to load the plugin and does not pass it on as a setting.
            source = settings.pop("source", None)
            if source is not None:
                if not isinstance(source, str) or not source:
                    raise TypeError(f"[CPU.cpu0.plugins.{plugin_name}].source must be a folder path")
                plugin_sources[str(plugin_name)] = (
                    Path(toml_config).expanduser().resolve().parent / source).resolve()
            plugin_config[str(plugin_name)] = settings
        cpu_obj.plugin_config = plugin_config
        cpu_obj.plugin_sources = plugin_sources

        #symbol resolution per cpu
		#if curr_cpu.get("map_file") is not None:
		#	cpu_obj.add_map_file(curr_cpu.get("map_file"))



        #add virtual instructions per cpu
        if curr_cpu.get("virtuals"):
            for value in curr_cpu.get("virtuals"):
                curr_vi = VirtualInstruction(
                    at=value['at'],
                    instruction=value['instruction'],
                    args=value['args']
                )
                cpu_obj.add_virtual_instruction(curr_vi)

        #add modifiers per cpu
        if curr_cpu.get("modifiers"):
            for value in curr_cpu.get("modifiers"):
                curr_modifier = InstructionModifier(
                    at=value['at'],
                    patch=value['patch']
                )
                cpu_obj.add_modifier(curr_modifier)

        cpus.append(cpu_obj)

    #add machine memory
    #make sure to first add main memory to avoid errors
    try:
        curr_mem = toml_parser.memory_info.pop('main')
    except:
        log.error("Unable to retreive Main Memory")
        raise KeyError("Main Memory required with the name 'main' in the toml configuration")

    machine0.add_memory(memory_name='main',
                        memory_id = curr_mem['id'],
                        memory_start = curr_mem.get('base_address', None),
                        memory_size=curr_mem['memory_size'],
                        memory_type=curr_mem['memory_type'],
                        backend      = curr_mem['backend'],          # file | ram | memfd
                        memory_file=_memory_file_for(machine_name, "main", curr_mem),
                        share = curr_mem['share'],
                        )

    #add additional memories added by the user in the toml config
    for memory in toml_parser.memory_info:
        curr_mem = toml_parser.memory_info.get(memory)[0]
        machine0.add_memory(memory_name=memory,
                            memory_id = curr_mem['id'],
                            memory_start = curr_mem.get('base_address', None),
                            memory_size=curr_mem['memory_size'],
                            memory_type=curr_mem['memory_type'],
                            backend      = curr_mem['backend'],          # file | ram | memfd
                            memory_file=_memory_file_for(machine_name, memory, curr_mem),
                            share = curr_mem['share'],
                            )

    #add devices information

    #get the available models for the user
    for model in toml_parser.devices_info.get('Models'):
        #parse device specific info
        model_info = toml_parser.devices_info.get('Models')[model]
        backend = model_info.get('backend')
        #add model and its info to the devices
        machine0.add_model(
            name=model,
            backend=backend
        )
    toml_parser.devices_info.pop('Models')

    #add devices added by the user
    for device in toml_parser.devices_info:
        device_info = toml_parser.devices_info[device]

        #create a device
        device_handler = machine0.add_device(device)
        device_handler.description = device_info.get('description', '')
        device_handler.config = {key: value for key, value in device_info.items()
                                 if key not in FASTDYN_DEVICE_KEYS}
        if isinstance(device_handler.config.get('slaves'), list):
            device_handler.config['slaves'] = _slaves_config_relative(toml_config, device_handler.config['slaves'])

        #add the handlers info
        for handler in device_info.get('handlers'):
            device_handler.add_handler(
                name=handler['model'],
                enabled=handler['enabled'],
                args=handler.get('args', None),
                scroll=_config_relative(toml_config, handler.get('scroll', None)),
                type=handler.get('type', None)
            )

        # Reject configs that enable more than one hardware-talking
        # backend on the same device range. passthrough and twintrace
        # both forward MMIO to the probe, so enabling both means every
        # firmware write becomes two probe writes to the same address
        # (corrupts clear-on-write registers, FIFO data registers,
        # etc.). Pick exactly one.
        HW_BACKENDS = {"passthrough", "twintrace"}
        hw_enabled = [h.model for h in device_handler.handlers
                      if h.enabled and h.model in HW_BACKENDS]
        if len(hw_enabled) > 1:
            log.error(
                f"[Device.{device}] enables multiple hardware-talking "
                f"backends on the same range: {hw_enabled}. "
                f"passthrough and twintrace both issue MMIO to the "
                f"physical probe; enabling both duplicates every write. "
                f"Set 'enabled = false' on all but one."
            )
            sys.exit(1)

        #add device ranges
        for r_entry in device_info.get('ranges', []) or []:
            if isinstance(r_entry, str):
                if "-" in r_entry:
                    parts = r_entry.split("-", 1)
                    r_start, r_end = parts[0].strip(), parts[1].strip()
                else:
                    r_start, r_end = r_entry, r_entry
            elif isinstance(r_entry, (list, tuple)) and len(r_entry) >= 2:
                r_start, r_end = r_entry[0], r_entry[1]
            else:
                continue
            device_handler.add_ranges(
                start=r_start,
                end=r_end
            )

        #add connections (host endpoints)
        for conn_entry in device_info.get('connections', []):
            device_handler.add_connection(_endpoint_config_relative(toml_config, conn_entry))

        #add irqs if added by the user
        if device_info.get('irq') is not None:
            irqs = device_info.get('irq')
            if isinstance(irqs, str):
                # The old "0-20:21-25" syntax; iterating it would add characters.
                raise ValueError(f"[Device.{device}].irq = {irqs!r} is the old range syntax; "
                                 "write ranges as a list, e.g. irq = [[0, 100]] or "
                                 "irq = [[0, 20], [21, 25], 41]")
            for irq in irqs:
                device_handler.add_irq(irq)

    return fastdyn_handle

class TomlParser:
    def __init__(self, config_path, fastdyn_handle):
        self.config_path    = config_path
        self.parsed_config  = self.toml_parser(config_path)
        self.fastdyn_handle = fastdyn_handle
        self.machine_info   = self.parsed_config.get("Machine")
        self.cpus_info      = self.parsed_config.get("CPU")
        self.devices_info   = self.parsed_config.get("Device")
        self.memory_info    = self.parsed_config.get("Memory")

    def toml_parser(self, config_path):
        return load_toml_config(config_path)
