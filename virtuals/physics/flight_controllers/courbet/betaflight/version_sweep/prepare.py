#!/usr/bin/env python3
"""Audit a sweep ELF and generate symbolic, hash-pinned bindings.

Review generated ABI and parameters before accepting a rebuilt image. This is
not an automatic claim that a new compiler output has been runtime validated.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
COURBET = HERE.parents[1]
ROOT = COURBET.parents[3]
sys.path.insert(0, str(COURBET))
from execution_configuration.elf import FirmwareELF


def audit(path):
    names = ['serialPort_s', 'motorDevice_s', 'busSegment_s', 'extDevice_s',
             'busDevice_s', 'featureConfig_s', 'rxConfig_s', 'motorConfig_s',
             'motorDevConfig_s', 'serialConfig_s', 'serialPortConfig_s',
             'gpsConfig_s', 'modeActivationCondition_s', 'sdcardConfig_s',
             'blackboxConfig_s', 'batteryConfig_s', 'pidConfig_s', 'controlRateConfig_s',
             'gyroDeviceConfig_s', 'barometerConfig_s', 'spiPinConfig_s', 'i2cConfig_s',
             'serialPinConfig_s', 'sensorAlignment_s', 'timerIOConfig_s']
    constants = ['SERIAL_PORT_USART1', 'SERIAL_PORT_USART2', 'SERIAL_PORT_USART3',
                 'FEATURE_RX_SERIAL', 'FEATURE_RX_MSP', 'FEATURE_GPS', 'FEATURE_TELEMETRY',
                 'FUNCTION_RX_SERIAL', 'FUNCTION_MSP', 'FUNCTION_GPS',
                 'FUNCTION_TELEMETRY_MAVLINK', 'GPS_UBLOX', 'SERIALRX_MAVLINK',
                 'RATES_TYPE_ACTUAL']
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / 'audit.json'
        commands = Path(tmp) / 'audit.gdb'
        commands.write_text('python\nimport gdb,json\nresult={"types":{},"constants":{}}\n'
            f'for name in {names!r}:\n'
            ' try:\n'
            '  typ=gdb.lookup_type("struct "+name)\n'
            '  result["types"][name]=[typ.sizeof,{f.name:f.bitpos//8 for f in typ.fields() if f.name and hasattr(f,"bitpos")}]\n'
            ' except gdb.error: pass\n'
            f'for name in {constants!r}:\n'
            ' try: result["constants"][name]=int(gdb.parse_and_eval(name))\n'
            ' except gdb.error: pass\n'
            'try:\n'
            ' result["gyro_custom_yaw_offset"]=int(gdb.parse_and_eval("(unsigned)&((gyroDeviceConfig_t*)0)->customAlignment.yaw"))\n'
            ' table=gdb.parse_and_eval("fullTimerHardware")\n'
            ' lo,hi=table.type.range()\n'
            ' result["timer_hardware"]=[{"tag":int(table[i]["tag"]),"af":int(table[i]["alternateFunction"])} for i in range(lo,hi+1)]\n'
            'except (gdb.error,TypeError): pass\n'
            f'open({str(output)!r},"w").write(json.dumps(result,indent=2))\nend\n')
        subprocess.run(['gdb-multiarch', '--nx', '--batch', '-iex', 'set auto-load off',
                        str(path), '-x', str(commands)], check=True, capture_output=True)
        return json.loads(output.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('--elf', type=Path, required=True)
    args = parser.parse_args()
    version = next(v for v in json.loads((HERE/'plan.json').read_text())['versions']
                   if v['tag'] == args.version)
    target = HERE / args.version
    target.mkdir(exist_ok=True)
    (target/'bin').mkdir(exist_ok=True)
    binary = target/'bin'/args.elf.name
    elf = FirmwareELF(args.elf)
    # These upstream globals are const char pointers in every selected release.
    def guest_string(symbol):
        address = int.from_bytes(elf.read_at(elf.resolve(symbol),4),'little')
        return elf.read_at(address,32).split(b'\0',1)[0].decode('ascii')
    revision = guest_string('shortGitRevision')
    target_name = guest_string('targetName')
    if len(revision) < 7 or not version['commit'].startswith(revision):
        raise ValueError(f'ELF revision {revision!r} does not match pinned {version["commit"]}')
    expected_target = 'MATEKF405' if args.version in ['4.3.2','4.2.11'] else 'STM32F405'
    if target_name != expected_target:
        raise ValueError(f'ELF target {target_name!r} does not match {expected_target}')
    data = audit(elf.path)
    types, constants = data['types'], data['constants']
    # These are the native bus and UART ABI assumptions, not inferred defaults.
    bus_abi = {
        'serialPort_s': {'identifier':56, 'rxCallback':44, 'rxCallbackData':48},
        'busSegment_s': {'u':0, 'len':8, 'negateCS':12, 'callback':16},
        'extDevice_s': {'bus':0, 'busType_u':4},
        'busDevice_s': {'busType_u':4, 'curSegment':32},
    }
    legacy = 'extDevice_s' not in types
    if legacy:
        assert types['busDevice_s'] == [12, {'bustype':0, 'busdev_u':4}]
        bus_abi = {'serialPort_s':bus_abi['serialPort_s']}
    for name, fields in bus_abi.items():
        for field, offset in fields.items():
            assert types[name][1][field] == offset, (name, field, types[name], offset)
    writes = []
    def put(symbol, typ, field, width, value, index=0):
        offset = types[typ][1][field] + index
        assert offset + width <= types[typ][0]
        writes.append(f'{symbol}+{offset}/{width}/{value}')
    mavrx = 'SERIALRX_MAVLINK' in constants
    features = constants['FEATURE_GPS'] | constants['FEATURE_TELEMETRY'] | constants['FEATURE_RX_SERIAL' if mavrx else 'FEATURE_RX_MSP']
    put('featureConfig_System','featureConfig_s','enabledFeatures',4,features)
    if mavrx:
        put('rxConfig_System','rxConfig_s','serialrx_provider',1,constants['SERIALRX_MAVLINK'])
    motor_fields = types['motorDevConfig_s'][1]
    protocol = 'motorProtocol' if 'motorProtocol' in motor_fields else 'motorPwmProtocol'
    continuous = 'useContinuousUpdate' if 'useContinuousUpdate' in motor_fields else 'useUnsyncedPwm'
    put('motorConfig_System','motorConfig_s','dev',1,0,index=motor_fields[protocol])
    put('motorConfig_System','motorConfig_s','dev',1,1,index=motor_fields[continuous])
    # Explicit serial functions: USB MSP, UART1 telemetry/receiver, UART2 MSP,
    # UART3 UBX. Preserve all board pin and peripheral configuration.
    port_size = types['serialPortConfig_s'][0]
    for slot, mask in enumerate([1,576 if mavrx else 512,1,2,0,0,0]):
        writes.append(f'serialConfig_System+{slot*port_size+types["serialPortConfig_s"][1]["functionMask"]}/4/{mask}')
    put('gpsConfig_System','gpsConfig_s','provider',1,constants['GPS_UBLOX'])
    for field in ['autoConfig','autoBaud']:
        put('gpsConfig_System','gpsConfig_s',field,1,0)
    if 'gps_update_rate_hz' in types['gpsConfig_s'][1]:
        put('gpsConfig_System','gpsConfig_s','gps_update_rate_hz',1,10)
    modes = [0,0,32,48,0,0, 1,1,32,48,0,0]
    assert types['modeActivationCondition_s'][0] == 6
    for offset, value in enumerate(modes):
        writes.append(f'modeActivationConditions_SystemArray+{offset}/1/{value}')
    for symbol, typ, field in [('sdcardConfig_System','sdcardConfig_s','mode'),
                             ('blackboxConfig_System','blackboxConfig_s','device'),
                             ('batteryConfig_System','batteryConfig_s','voltageMeterSource'),
                             ('batteryConfig_System','batteryConfig_s','currentMeterSource')]:
        put(symbol,typ,field,1,0)
    put('pidConfig_System','pidConfig_s','pid_process_denom',1,8)
    if args.version == '4.4.3':
        # The 4.4 board is an official unified-target configuration, rather than
        # a compiled board header. Apply its bus/pin parameters before device init.
        def tag(pin):
            return ((ord(pin[0])-ord('A')+1)<<4) | int(pin[1:])
        resources = {}
        for line in (target/'MTKS-MATEKF405CTR.config').read_text().splitlines():
            tokens = line.split()
            if tokens[:1] == ['resource']:
                resources[tokens[1],int(tokens[2])] = tag(tokens[3])
        for name,field in [('GYRO_CS','csnTag'),('GYRO_EXTI','extiTag')]:
            put('gyroDeviceConfig_SystemArray','gyroDeviceConfig_s',field,1,resources[name,1])
        for field,value in [('busType',2),('spiBus',1),('alignment',4)]:
            put('gyroDeviceConfig_SystemArray','gyroDeviceConfig_s',field,1,value)
        offset = data['gyro_custom_yaw_offset']
        assert offset == types['gyroDeviceConfig_s'][1]['customAlignment'] + 4
        writes.append(f'gyroDeviceConfig_SystemArray+{offset}/2/2700')
        for field,value in [('baro_busType',1),('baro_i2c_device',1),('baro_hardware',4)]:
            put('barometerConfig_System','barometerConfig_s',field,1,value)
        for (resource,index),value in resources.items():
            if resource in ['SPI_SCK','SPI_MISO','SPI_MOSI']:
                field={'SPI_SCK':'ioTagSck','SPI_MISO':'ioTagMiso','SPI_MOSI':'ioTagMosi'}[resource]
                offset=(index-1)*types['spiPinConfig_s'][0]+types['spiPinConfig_s'][1][field]
                writes.append(f'spiPinConfig_SystemArray+{offset}/1/{value}')
            if resource in ['I2C_SCL','I2C_SDA']:
                field='ioTagScl' if resource == 'I2C_SCL' else 'ioTagSda'
                offset=(index-1)*types['i2cConfig_s'][0]+types['i2cConfig_s'][1][field]
                writes.append(f'i2cConfig_SystemArray+{offset}/1/{value}')
            if resource in ['SERIAL_TX','SERIAL_RX']:
                put('serialPinConfig_System','serialPinConfig_s',
                    'ioTagTx' if resource == 'SERIAL_TX' else 'ioTagRx',1,value,index=index-1)
            if resource == 'MOTOR':
                put('motorConfig_System','motorConfig_s','dev',1,value,
                    index=types['motorDevConfig_s'][1]['ioTags']+index-1)
        timer_slot = 0
        for line in (target/'MTKS-MATEKF405CTR.config').read_text().splitlines():
            tokens=line.split()
            if tokens[:1] != ['timer']:
                continue
            io=tag(tokens[1]); af=int(tokens[2][2:])
            matches=[item for item in data['timer_hardware'] if item['tag']==io]
            index=next(i+1 for i,item in enumerate(matches) if item['af']==af)
            for field,value in [('ioTag',io),('index',index),('dmaopt',255)]:
                offset=timer_slot*types['timerIOConfig_s'][0]+types['timerIOConfig_s'][1][field]
                writes.append(f'timerIOConfig_SystemArray+{offset}/1/{value}')
            timer_slot += 1
    if 'RATES_TYPE_ACTUAL' in constants:
        put('controlRateProfiles_SystemArray','controlRateConfig_s','rates_type',1,constants['RATES_TYPE_ACTUAL'])
        for axis in range(3):
            for field,value in [('rcRates',7),('rcExpo',0),('rates',67)]:
                put('controlRateProfiles_SystemArray','controlRateConfig_s',field,1,value,index=axis)
    base = COURBET/'betaflight/matekf405ctr/labeled_conf'
    labeled = target/'labeled_conf'
    labeled.mkdir(exist_ok=True)
    rules = (base/'virtuals.txt').read_text().splitlines()
    rules = [line for line in rules if not line.startswith('featureInit ')]
    rules = [line if not line.startswith('motorWriteAll ') else
             f'motorWriteAll bf_motor_write_all motorDevice,{types["motorDevice_s"][1]["count"]}'
             for line in rules]
    if 'systemFaultAction' not in elf.symbols:
        rules = [line if not line.startswith('systemFaultAction ') else
                 'HardFault_Handler bf_trace hw-hardfault' for line in rules]
    if 'configWriteWord' not in elf.symbols:
        rules = [line if not line.startswith('configWriteWord ') else
                 'FLASH_ProgramWord bf_hw_flash_word *' for line in rules]
    # Old UART vtables expose only per-byte write; no buffer/endWrite entry exists.
    rules = [line for line in rules if not
             (line.startswith(('uartWriteBuf ', 'uartEndWrite ')) and line.split()[0] not in elf.symbols)]
    if 'usTicksInv' not in elf.symbols:
        rules = [line if not line.startswith('cycleCounterInit ') else
                 'cycleCounterInit bf_cycle_counter_init *usTicks,0,0' for line in rules]
    if legacy:
        rules = [line for line in rules if not line.startswith('spiSequence ')]
        rules = [line.replace('bf_hw_i2c_read','bf_hw_i2c_legacy_read')
                      .replace('bf_hw_i2c_write','bf_hw_i2c_legacy_write')
                      .replace('mpuAccReadSPI ', 'mpuAccRead ') for line in rules]
        rules += ['spiBusRawReadRegisterBuffer bf_hw_spi_legacy_read *',
                  'spiBusRawReadRegister bf_hw_spi_legacy_byte *',
                  'spiBusWriteRegister bf_hw_spi_legacy_write *',
                  'spiBusTransfer bf_hw_spi_legacy_transfer *']
    parameters = ''
    for entry in writes:
        symbol, width, value = entry.split('/')
        parameters += f'{elf.resolve(symbol):#x}/{width}/{value}\n'
    rules.insert(0, 'featureInit bf_hw_parameters ' + args.version)
    # Reject missing hook symbols here rather than silently omitting boundaries.
    for line in rules:
        if line and not line.startswith('#'):
            elf.resolve(line.split()[0])
    modifiers = (base/'modifiers.txt').read_text()
    if legacy:
        modifiers = '\n'.join(line for line in modifiers.splitlines() if not line.startswith('spiUseDMA '))+'\n'
    if 'configWriteWord' not in elf.symbols:
        modifiers += '\nFLASH_EraseSector r0 9\nFLASH_EraseSector r15 r14\n'
    manifest = dict(version=version, elf_sha256=elf.sha256, expected=types,
                    image_revision=revision, image_target=target_name,
                    constants=constants, receiver='MAVLink' if mavrx else 'MSP',
                    parameter_writes=writes,
                    parameters_sha256=hashlib.sha256(parameters.encode()).hexdigest())
    if args.version == '4.4.3':
        manifest['unified_config_commit']='7f1abbf7844d3299ecf2ed4cca4680dc95a941b5'
        manifest['unified_config_sha256']=hashlib.sha256((target/'MTKS-MATEKF405CTR.config').read_bytes()).hexdigest()
        manifest['timer_hardware']=data['timer_hardware']
        manifest['gyro_custom_yaw_offset']=data['gyro_custom_yaw_offset']
    profile = (f'schema_version = 1\nfirmware = "betaflight"\nelf = "bin/{binary.name}"\n'
               'labeled_conf = "labeled_conf"\noutput_dir = "unlabeled_conf"\n\n'
               f'[validation]\nelf_sha256 = "{elf.sha256}"\n\n[validation.symbol_addresses]\n')
    for name in ['__config_start','isr_vector_table_flash_base','isr_vector_table_base','_estack']:
        profile += f'{name} = {elf.resolve(name):#x}\n'
    for name, size in [('isr_vector_table_base',8),('run',2),('run+2',4)]:
        profile += f'\n[[validation.instructions]]\nat = "{name}"\nbytes = "{elf.read_at(elf.resolve(name),size).hex()}"\n'
    # Complete all source/ABI/symbol/instruction checks before replacing files.
    if args.elf.resolve() != binary.resolve():
        shutil.copy2(args.elf,binary)
    (target/'parameters.txt').write_text(parameters)
    (labeled/'virtuals.txt').write_text('\n'.join(rules)+'\n')
    (labeled/'modifiers.txt').write_text(modifiers)
    (target/'abi.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (target/'profile.toml').write_text(profile)
    print(json.dumps(dict(version=args.version,elf_sha256=elf.sha256,receiver=manifest['receiver']),indent=2))


if __name__ == '__main__':
    main()
