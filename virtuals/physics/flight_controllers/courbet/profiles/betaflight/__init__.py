"""Betaflight (bare-metal STM32) adapter ABI for the bf_* bridge in betaflight.c."""
from execution_configuration.rules import resolve_operand
from execution_configuration.validation import validate_abi
from execution_configuration.elf import is_number
import json
import hashlib
from pathlib import Path


# Offsets consumed by virtuals/physics/flight_controllers/betaflight/betaflight.c.
# gdb `ptype/o` on the FASTDYNF405 ELF; regenerate if Betaflight changes these.
ABI = {
    'serialPort_s': (60, {'identifier': 56}),
    'gyroDev_s': (348, {'scale': 168, 'gyroADCRaw': 208, 'dataReady': 293}),
    'accDev_s': (68, {'acc_1G': 12, 'ADCRaw': 14, 'dataReady': 23}),
    'motorDevice_s': (12, {'count': 4}),
    'gpsSolutionData_s': (72, {'llh': 0, 'dop': 12, 'acc': 20, 'velned': 36, 'speed3d': 42,
                               'groundSpeed': 44, 'groundCourse': 46, 'numSat': 48,
                               'numSatInView': 49, 'time': 52}),
    'gpsDilution_s': (6, {'pdop': 0, 'hdop': 2, 'vdop': 4}),
    'gpsAccuracy_s': (16, {'hAcc': 0, 'vAcc': 4, 'sAcc': 8, 'headAcc': 12}),
    'gpsVelned_s': (6, {'velN': 0, 'velE': 2, 'velD': 4}),
}


def validate(elf):
    sweep = Path(__file__).resolve().parents[2] / 'betaflight' / 'version_sweep'
    for manifest in sweep.glob('*/abi.json'):
        record = json.loads(manifest.read_text())
        if record['elf_sha256'] == elf.sha256:
            parameters = manifest.parent / 'parameters.txt'
            if hashlib.sha256(parameters.read_bytes()).hexdigest() != record['parameters_sha256']:
                raise ValueError('sweep parameter file changed; review and regenerate bindings')
            validate_abi(elf.path, record['expected'])
            return
    validate_abi(elf.path, ABI)
    if 'mpu6000SpiDetect' in elf.symbols:
        validate_abi(elf.path, {
            'featureConfig_s': (4, {'enabledFeatures': 0}),
            'busSegment_s': (20, {'u': 0, 'len': 8, 'negateCS': 12, 'callback': 16}),
            'extDevice_s': (152, {'bus': 0, 'busType_u': 4, 'txBuf': 140, 'rxBuf': 144}),
            'busDevice_s': (40, {'busType': 0, 'busType_u': 4, 'curSegment': 32}),
            'rxConfig_s': (52, {'serialrx_provider': 8, 'rx_uart': 50}),
            'motorConfig_s': (38, {'dev': 0}),
            'motorDevConfig_s': (28, {'motorProtocol': 2, 'useContinuousUpdate': 4}),
            'mspConfig_s': (7, {'msp_uart': 1}),
            'gpsConfig_s': (80, {'provider': 0, 'autoConfig': 2, 'autoBaud': 3,
                                'gps_update_rate_hz': 6, 'gps_uart': 78}),
            'modeActivationCondition_s': (6, {'modeId': 0, 'auxChannelIndex': 1, 'range': 2}),
            'sdcardConfig_s': (5, {'mode': 4}),
            'blackboxConfig_s': (12, {'device': 5}),
            'batteryConfig_s': (14, {'voltageMeterSource': 3, 'currentMeterSource': 4}),
            'pidConfig_s': (6, {'pid_process_denom': 0}),
        })


def resolve_argument(callback, argument, elf):
    """Resolve symbols inside callback arguments.

    `*` alone keeps the control-flow flag. `*a,b,c` resolves every symbolic
    token to hex (bf_cycle_counter_init). A bare operand (bf_motor_write_all's
    motorDevice) resolves through the shared operand rules; literal tags for
    bf_trace stay opaque.
    """
    if argument is None:
        return None
    if callback == 'bf_hw_parameters':
        path = Path(__file__).resolve().parents[2] / 'betaflight' / 'version_sweep' / argument / 'parameters.txt'
        if not path.is_file() or len(str(path)) > 300:
            raise ValueError('invalid sweep parameter file')
        return str(path)
    if callback == 'bf_trace':
        return argument
    if argument == '*':
        return argument
    star = argument.startswith('*')
    tokens = argument[1:].split(',') if star else argument.split(',')
    if not all(tokens):
        raise ValueError(f'{callback}: empty token in argument {argument!r}')
    if callback == 'bf_hw_config':
        if star or len(tokens) != 10:
            raise ValueError('bf_hw_config requires ten configuration symbols and an observation-only hook')
        return ','.join(hex(elf.resolve(token)) for token in tokens)
    if callback == 'bf_motor_write_all' and len(tokens) == 2 and is_number(tokens[1]):
        return f'{hex(elf.resolve(tokens[0]))},{tokens[1]}'
    if all(is_number(token) for token in tokens):
        # Built-in virtuals take literal lists, e.g. raise_periodic_irq 15,1000000.
        return argument
    if star:
        return '*' + ','.join(token if is_number(token) else hex(elf.resolve(token))
                              for token in tokens)
    return resolve_operand(argument, elf)
