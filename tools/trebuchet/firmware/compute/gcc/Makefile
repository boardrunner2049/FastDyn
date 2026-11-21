OUTPUT_DIR := ./output
IMAGE := $(OUTPUT_DIR)/RTOSDemo.out

# The directory that contains the /source and /demo sub directories.
FREERTOS_ROOT = ./../../../..

CC = arm-none-eabi-gcc
LD = arm-none-eabi-gcc
SIZE = arm-none-eabi-size
MAKE = make

ifeq ($(PICOLIBC), 1)
    CFLAGS += --specs=picolibc.specs -DPICOLIBC_INTEGER_PRINTF_SCANF
endif
CFLAGS += -mthumb -mcpu=cortex-m3 -ffreestanding
CFLAGS += -g3 -Os -ffunction-sections -fdata-sections
# fastdyn compute fixture: keep the DWARF free of the checkout location
CFLAGS += -ffile-prefix-map=$(abspath $(FREERTOS_ROOT)/../..)=.
#CFLAGS += -flto
CFLAGS += -Wall -Wextra -Wshadow -Wno-unused-value
CFLAGS += -MMD -MP -MF"$(@:%.o=%.d)" -MT $@
CFLAGS += $(INCLUDE_DIRS)

LDFLAGS = -T ./mps2_m3.ld
LDFLAGS += -Xlinker -Map=$(OUTPUT_DIR)/RTOSDemo.map
LDFLAGS += -Xlinker --gc-sections
#LDFLAGS += -Xlinker --print-gc-sections
ifneq ($(PICOLIBC), 1)
    LDFLAGS += -nostartfiles -specs=nano.specs -specs=nosys.specs # -specs=rdimon.specs
endif

#
# Kernel build.
#
KERNEL_DIR = $(FREERTOS_ROOT)/Source
KERNEL_PORT_DIR += $(KERNEL_DIR)/portable/GCC/ARM_CM3
INCLUDE_DIRS += -I$(KERNEL_DIR)/include \
				-I$(KERNEL_PORT_DIR)
VPATH += $(KERNEL_DIR) $(KERNEL_PORT_DIR) $(KERNEL_DIR)/portable/MemMang
SOURCE_FILES += $(KERNEL_DIR)/tasks.c
SOURCE_FILES += $(KERNEL_DIR)/list.c
SOURCE_FILES += $(KERNEL_DIR)/queue.c
SOURCE_FILES += $(KERNEL_DIR)/timers.c
SOURCE_FILES += $(KERNEL_DIR)/event_groups.c
SOURCE_FILES += $(KERNEL_DIR)/stream_buffer.c
SOURCE_FILES += $(KERNEL_DIR)/portable/MemMang/heap_4.c
SOURCE_FILES += $(KERNEL_DIR)/portable/GCC/ARM_CM3/port.c

#
# fastdyn compute fixture: the "full" demo's Common/Minimal files are not
# built (main_full is not used); DEMO_ROOT is kept for the TraceRecorder path.
#
DEMO_ROOT = $(FREERTOS_ROOT)/Demo

#
# Application entry point.  main_blinky is self contained.  main_full builds
# the above common demo (and test) files too.
#
DEMO_PROJECT = $(DEMO_ROOT)/CORTEX_MPS2_QEMU_IAR_GCC
VPATH += $(DEMO_PROJECT)
INCLUDE_DIRS += -I$(DEMO_PROJECT) -I$(DEMO_PROJECT)/CMSIS
SOURCE_FILES += (DEMO_PROJECT)/main.c
SOURCE_FILES += (DEMO_PROJECT)/main_compute.c
SOURCE_FILES += ./startup_gcc.c
# Lightweight print formatting to use in place of the heavier GCC equivalent.
SOURCE_FILES += ./printf-stdarg.c

# Percepio TraceRecorder (FreeRTOS-Plus-Trace)
TRACERECORDER_DIR = $(DEMO_ROOT)/../../FreeRTOS-Plus/Source/FreeRTOS-Plus-Trace
TRACERECORDER_CFG_DIR = $(DEMO_PROJECT)/TraceRecorderConfig
VPATH += $(TRACERECORDER_DIR)
VPATH += $(TRACERECORDER_DIR)/kernelports/FreeRTOS
VPATH += $(TRACERECORDER_DIR)/streamports/RingBuffer
INCLUDE_DIRS += -I$(TRACERECORDER_CFG_DIR)
INCLUDE_DIRS += -I$(TRACERECORDER_DIR)/include
INCLUDE_DIRS += -I$(TRACERECORDER_DIR)/kernelports/FreeRTOS/include
INCLUDE_DIRS += -I$(TRACERECORDER_DIR)/streamports/RingBuffer/include
SOURCE_FILES +=	(TRACERECORDER_DIR)/kernelports/FreeRTOS/trcKernelPort.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/streamports/RingBuffer/trcStreamPort.c
SOURCE_FILES += (TRACERECORDER_DIR)/trcAssert.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcCounter.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcDependency.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcDiagnostics.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcEntryTable.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcError.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcEvent.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcEventBuffer.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcExtension.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcHardwarePort.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcHeap.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcInternalEventBuffer.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcInterval.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcISR.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcMultiCoreEventBuffer.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcObject.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcPrint.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcRunnable.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcSnapshotRecorder.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcStackMonitor.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcStateMachine.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcStaticBuffer.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcStreamingRecorder.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcString.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcTask.c
SOURCE_FILES +=	(TRACERECORDER_DIR)/trcTimestamp.c

#Create a list of object files with the desired output directory path.
OBJS = $(SOURCE_FILES:%.c=%.o)
OBJS_NO_PATH = $(notdir $(OBJS))
OBJS_OUTPUT = $(OBJS_NO_PATH:%.o=$(OUTPUT_DIR)/%.o)

#Create a list of dependency files with the desired output directory path.
DEP_FILES := $(SOURCE_FILES:%.c=$(OUTPUT_DIR)/%.d)
DEP_FILES_NO_PATH = $(notdir $(DEP_FILES))
DEP_OUTPUT = $(DEP_FILES_NO_PATH:%.d=$(OUTPUT_DIR)/%.d)

all: $(IMAGE)

%.o : %.c
$(OUTPUT_DIR)/%.o : %.c $(OUTPUT_DIR)/%.d Makefile
	$(CC) $(CFLAGS) -c $< -o $@

$(IMAGE): ./mps2_m3.ld $(OBJS_OUTPUT) Makefile
	@echo ""
	@echo ""
	@echo "--- Final linking ---"
	@echo ""
	$(LD) $(CFLAGS) $(LDFLAGS) $(OBJS_OUTPUT) -o $(IMAGE)
	$(SIZE) $(IMAGE)

$(DEP_OUTPUT):
include $(wildcard $(DEP_OUTPUT))

clean:
	rm -f $(IMAGE) $(OUTPUT_DIR)/RTOSDemo.map $(OUTPUT_DIR)/*.o $(OUTPUT_DIR)/*.d

#use "make print-[VARIABLE_NAME] to print the value of a variable generated by
#this makefile.
print-%  : ; @echo $* = $($*)

.PHONY: all clean


