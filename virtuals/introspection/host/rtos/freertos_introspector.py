from ..introspector_base import RTOSIntrospector
from fastdyn.binary.schema_gen import *
from fastdyn import fastdyn_log as fastdyn_log_conf
import struct
import logging
import pathlib

log = logging.getLogger(__name__)
fastdyn_log = fastdyn_log_conf.getFastdynLogger()

class FreeRTOSIntrospector(RTOSIntrospector, rtos_name="FreeRTOS"):
    
    def setup_hooks(self):
        """Wires up the FreeRTOS-specific execution hooks."""
        # Register hooks
        self.register_prologue_hook('vTaskSwitchContext')
        self.register_prologue_hook('prvAddNewTaskToReadyList')
        self.register_epilogue_hook('xQueueGenericCreate')
        self.register_epilogue_hook('xTimerCreate')
        self.register_prologue_hook('xQueueSemaphoreTake')
        self.register_prologue_hook('xQueueGenericSend')
        self.register_prologue_hook('xTimerGenericCommand')

        # TODO: remove hardcoding requirements, i think the registeration function can return which symbols/structs need to be exported.
        # Pass the structs you know you'll need for this RTOS
        generator = (SchemaGenerator(self.binary, analysis=self.analysis)
                     if self.analysis is not None else SchemaGenerator(self.binary))
        target_structs = [
            "tskTaskControlBlock",
            "xLIST",
            "xLIST_ITEM",
            "xMINI_LIST_ITEM",
            "QueueDefinition",
            "TimerDefinition",
        ]
        symbols_to_export = {
            "pxCurrentTCB": self.symbols.get('pxCurrentTCB').address,
            "pxReadyTasksLists": self.symbols.get('pxReadyTasksLists').address,
        }

        # Write the schema for fastdyn
        schema_content = generator.generate_schema(target_structs, symbols_to_export)

        return schema_content
