from ..introspector_base import RTOSIntrospector
from fastdyn.binary.schema_gen import *
from fastdyn import fastdyn_log as fastdyn_log_conf
import struct
import logging

log = logging.getLogger(__name__)
fastdyn_log = fastdyn_log_conf.getFastdynLogger()

class ChibiOSIntrospector(RTOSIntrospector, rtos_name="ChibiOS"):

    def setup_hooks(self):
        """Wire up ChibiOS-specific execution hooks and emit schema."""

        self.register_prologue_hook("__port_switch")
        self.register_prologue_hook("__thd_object_init")
        for hook in (
            "chSemObjectInit", "chMtxObjectInit", "chVTObjectInit",
            "chSemWaitTimeout", "chSemSignal", "chMtxLock", "chMtxUnlock",
            "chVTSet", "chVTReset",
            "chMBObjectInit", "chMBPostTimeout", "chMBFetchTimeout",
        ):
            self.register_prologue_hook(hook)

        # Generate the schema FastDyn needs. These struct names must match the
        # DWARF type names produced by the ChibiOS build:
        #
        #   - ch_thread       : kernel thread descriptor (thread_t)
        #   - ch_system       : global system state (ch_system_t)
        #   - ch_os_instance  : per-core OS instance (os_instance_t)
        #   - ch_ready_list   : ready list wrapper (ready_list_t)
        #   - ch_priority_queue : priority queue node/header
        generator = (SchemaGenerator(self.binary, analysis=self.analysis)
                     if self.analysis is not None else SchemaGenerator(self.binary))
        target_structs = [
            "ch_thread",
            "ch_system",
            "ch_os_instance",
            "ch_ready_list",
            "ch_priority_queue",
        ]

        # Get symbol address from elf
        symbols_to_export = {}
        for sym_name in ["ch_system", "ch_debug"]:
            sym = self.symbols.get(sym_name)
            address = None
            if sym is None and self.analysis is not None:
                matches = self.analysis.symbols.find(sym_name, kind="object")
                if len({item.address for item in matches}) == 1:
                    address = matches[0].address
            else:
                address = sym.address

            if address is None:
                log.warning("[ChibiOSIntrospector] Missing symbol '%s' in binary", sym_name)
                continue
            symbols_to_export[sym_name] = address

        # Write the schema for fastdyn
        schema_content = generator.generate_schema(target_structs, symbols_to_export)
        return schema_content
