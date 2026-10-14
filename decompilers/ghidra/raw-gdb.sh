#!/usr/bin/env bash
## ###
#  IP: GHIDRA
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
#  
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
##
#@title raw gdb
#@no-image
#@desc <html><body width="300px">
#@desc   <h3>Start <tt>gdb</tt></h3>
#@desc   <p>
#@desc     This will start <tt>gdb</tt> and connect to it.
#@desc     It will not launch a target, so you can (must) set up your target manually.
#@desc     For setup instructions, press <b>F1</b>.
#@desc   </p>
#@desc </body></html>
#@menu-group raw
#@icon icon.debugger
#@help TraceRmiLauncherServicePlugin#gdb_raw
#@env OPT_GDB_PATH:file="gdb" "gdb command" "The path to gdb. Omit the full path to resolve using the system PATH."
#@env OPT_ARCH:str="i386:x86-64" "Architecture" "Target architecture"

if [ -d ${GHIDRA_HOME}/ghidra/.git ]
then
  export PYTHONPATH=$GHIDRA_HOME/ghidra/Ghidra/Debug/Debugger-agent-gdb/build/pypkg/src:$PYTHONPATH
  export PYTHONPATH=$GHIDRA_HOME/ghidra/Ghidra/Debug/Debugger-rmi-trace/build/pypkg/src:$PYTHONPATH
elif [ -d ${GHIDRA_HOME}/.git ]
then 
  export PYTHONPATH=$GHIDRA_HOME/Ghidra/Debug/Debugger-agent-gdb/build/pypkg/src:$PYTHONPATH
  export PYTHONPATH=$GHIDRA_HOME/Ghidra/Debug/Debugger-rmi-trace/build/pypkg/src:$PYTHONPATH
else
  export PYTHONPATH=$GHIDRA_HOME/Ghidra/Debug/Debugger-agent-gdb/pypkg/src:$PYTHONPATH
  export PYTHONPATH=$GHIDRA_HOME/Ghidra/Debug/Debugger-rmi-trace/pypkg/src:$PYTHONPATH
fi

# TODO: Need to update before running on your system!!
QEMU=${QEMU:?set QEMU to your QEMU checkout}
DYNFAST=${DYNFAST:?set DYNFAST to your FastDyn checkout}
#for Firmware just use absolute path
FIRMWARE=${FIRMWARE:?set FIRMWARE to the firmware ELF (absolute path)}
VECTOR=0x00000000

# Our changes to hook dynfast execution
source $QEMU/ws/banner.sh

# Navigate to DynFast build directory
cd "$QEMU/build" || {
    echo "Failed to cd into $QEMU/build"
    exit 1
}

./qemu-system-arm --plugin $DYNFAST/build/libfastdyn.so,dev=classic:,monitor=../ws/monitor.elf,logger=../ws/log_config.txt,virtual=../ws/virtuals.txt,detour=../ws/detours.txt,modifier=../ws/modifiers.txt -d in_asm,op -D qemu.log -machine cortexm,memory-backend=ram0 -monitor telnet:127.0.0.1:5555,server,nowait -semihosting --semihosting-config enable=on,target=native -qmp unix:/tmp/qmp-sock,server,nowait -kernel $FIRMWARE -serial stdio -nographic -object memory-backend-file,id=ram0,mem-path=/dev/shm/my_m4_ram3,size=512M,share=on -object memory-backend-file,id=ram1,mem-path=/dev/shm/my_m4_ram,size=512K,share=on -global cortexm-soc.shram_backend=ram1  -global cortexm-soc.ram_baseaddr=0x20000000  -global cortexm-soc.shram_baseaddr=0x30000000 -qmp unix:/tmp/qmp.sock,server=on,wait=off -chardev socket,id=char0,path=/tmp/usart1.sock,server=on,wait=off -device stm32f2xx-usart,id=usart1,chardev=char0,addr=0x40011000 -cpu cortex-m4 -global armv7m.init-nsvtor=$VECTOR  -S -s &

cd -

"$OPT_GDB_PATH" \
  -q \
  -ex "set pagination off" \
  -ex "set confirm off" \
  -ex "show version" \
  -ex "python import ghidragdb" \
  -ex "set architecture $OPT_ARCH" \
  -ex "ghidra trace connect \"$GHIDRA_TRACE_RMI_ADDR\"" \
  -ex "ghidra trace start" \
  -ex "ghidra trace sync-enable" \
  -ex "set confirm on" \
  -ex "set pagination on" \
  -ex "target remote :1234" \
  -ex "source $QEMU/ws/scripts/mcall.py" \
  -ex "set confirm off" \
  -ex "file $FIRMWARE" \
  -ex "source $QEMU/ws/scripts/reginfo.py" \
