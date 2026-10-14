CC=clang
BIN := idlefirmware.axf
CMSIS_PATH ?= ./CMSIS_5/
PIECES ?= /data/Pieces/
#Possible values KLEE, ETSAN, EC, CRTC, ECRTC
PROJECT ?= CRTC
#Possible values Flash, StreamBuffer, RECM, QSET, CR, INT
#Possible Baremetal demos include LwIP, Animation, SD
DEMO := Flash
BUILD_DIR := build

GCC_INSTALL ?= $(error set GCC_INSTALL to the unpacked arm-gnu-toolchain-13.3.rel1 directory, ending in /)
GCC_INSTALL_INCLUDE = $(GCC_INSTALL)arm-none-eabi/include/
CHECKED_C_INSTALL_INCLUDE ?=/data/checkedc/include
GCC_INSTALL_LIB  ?= $(GCC_INSTALL)arm-none-eabi/lib/thumb/v7e-m+fp/hard/
GCC_INSTALL_GLIB ?= $(GCC_INSTALL)lib/gcc/arm-none-eabi/13.3.1/thumb/v7e-m+fp/hard/
HAL_INSTALL ?= ./Drivers/STM32H7xx_HAL_Driver/
BSP_INSTALL ?= ./Drivers/BSP/STM32H7xx_Nucleo/




SOURCE_FILES += main.c interrupt_server.c

#Finalize configuration variables based on project/demo
CFLAGS += ${CFLAGS_${PROJECT}_${DEMO}} -O0 -nostdlib -DSTM32H743xx
#LDFLAGS = ${LDFLAGS_${PROJECT}_${DEMO}} //Pretty much every configuration uses the same ld flags, can extend in future.
LDFLAGS = -v -T ./LinkerScript.ld -specs=nano.specs --specs=rdimon.specs #-lc -lrdimon
LDFLAGS += -Xlinker -Map=${BUILD_DIR}/output.map

LDFLAGS +=  -L$(GCC_INSTALL_GLIB) -L$(GCC_INSTALL_LIB) -L./ -v
#LDFLAGS +=  -L./ -v
INCLUDE_DIRS = ${INCLUDE_DIRS_${PROJECT}_${DEMO}} -I $(PIECES)/partitioner/monitor/include/ -I $(GCC_INSTALL_INCLUDE) -I $(CHECKED_C_INSTALL_INCLUDE)
#Device Path
INCLUDE_DIRS += -I$(CMSIS_PATH)/Device/ST/STM32H7xx/Include/
INCLUDE_DIRS += -I$(CMSIS_PATH)/CMSIS/Core/Include/
INCLUDE_DIRS += -I$(BSP_INSTALL)/
INCLUDE_DIRS += -I$(HAL_INSTALL)/Inc/






#Prepare variables for build rules
OBJ_FILES := $(SOURCE_FILES:%.c=$(BUILD_DIR)/%.o)
CFLAGS += $(INCLUDE_DIRS)

CFLAGS += -flto=thin --target=arm-none-eabi -mthumb -mcpu=cortex-m7 -mfloat-abi=hard -mfpu=fpv5-sp-d16 -O0 -g

CFLAGS += $(CFLAG_EXTRA)




.PHONY: clean

$(BUILD_DIR)/$(BIN) : $(OBJ_FILES) $(BUILD_DIR)/startup.o
	$(CC) -ffunction-sections -fdata-sections $(CFLAGS) $(LDFLAGS) $+ -o $(@)

%.d: %.c
	@set -e; rm -f $@; \
    $(CC) -M $(CPPFLAGS) $< > $@.$$$$; \
    sed 's,\($*\)\.o[ :]*,\1.o $@ : ,g' < $@.$$$$ > $@; \
    rm -f $@.$$$$

INCLUDES := $(SOURCE_FILES:%.c=$(BUILD_DIR)/%.d)
-include $(INCLUDES)

${BUILD_DIR}/%.o : %.c Makefile
	-mkdir -p $(@D)
	$(CC) $(CFLAGS) $(CPPFLAGS) -MMD -c $< -o $@
	echo $(CFLAGS) > $(BUILD_DIR)/.cflags

$(BUILD_DIR)/startup.o : startup.s
	$(CC) -c -ffunction-sections -fdata-sections $(CFLAGS)  $+ -o $(@)

clean:
	-rm -rf build

bc: $(OBJ_FILES)
	$(warning Files are $(OBJ_FILES))
	llvm-link  -v $+ -o $(BUILD_DIR)/$(BIN).bc
	llvm-dis $(BUILD_DIR)/$(BIN).bc

exec: build/idlefirmware.axf.bc $(BUILD_DIR)/startup.o #$(CC) -c -ffunction-sections -fdata-sections $(CFLAGS)  $+ -o $(@)
	llc -filetype=obj build/idlefirmware.axf.bc
	$(CC)  --target=arm-none-eabi -mthumb -mcpu=cortex-m7 $(LDFLAGS)  -Xlinker -Map=./build/rtmk.map build/idlefirmware.axf.bc $(BUILD_DIR)/startup.o -o ./build/idlefirmware.axf

run:
	./build_files/qemu-system-arm -M stm32-p103 -semihosting -nographic -kernel $(BUILD_DIR)/idlefirmware.axf -s -S

rop:
	python3 ./genROPCount.py -b ./build/idlefirmware.axf -i ./sizeinfo

bcet: $(OBJ_FILES)
	llvm-link  -v $+ -o $(BUILD_DIR)/$(BIN).bc
	llvm-dis $(BUILD_DIR)/$(BIN).bc
	sed -i 's/optnone//g' $(BUILD_DIR)/$(BIN).ll
	opt -mem2reg -ipsccp -debug-pass=Arguments ./build/idlefirmware.axf.ll -o $(BUILD_DIR)/$(BIN).bc
	llvm-dis $(BUILD_DIR)/$(BIN).bc

bc3: $(OBJ_FILES)
	llvm-link  -v $+ -o $(BUILD_DIR)/$(BIN).bc
	llvm-dis $(BUILD_DIR)/$(BIN).bc
	sed -i 's/optnone//g' $(BUILD_DIR)/$(BIN).ll
	opt -O3 ./build/idlefirmware.axf.ll -o $(BUILD_DIR)/$(BIN).bc
	llvm-dis $(BUILD_DIR)/$(BIN).bc

itm:
	./extern/itm-tools/target/debug/itm-decode ~/etsan/itm.fifo
