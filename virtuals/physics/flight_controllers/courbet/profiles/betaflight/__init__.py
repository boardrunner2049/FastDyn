"""Betaflight (bare-metal STM32) adapter ABI for the bf_* bridge in betaflight.c."""
from execution_configuration.rules import resolve_operand
from execution_configuration.validation import validate_abi
from execution_configuration.elf import is_number
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


# Hardware release layouts are checked here, as in the PX4 adapter. Only fields
# consumed by the bridge and parameter settings are listed; images stay pinned
# by profile.toml. Most layouts are shared across releases.
SWEEP_ABI = {
    'serialPort_s': (60, {'identifier': 56, 'rxCallback': 44, 'rxCallbackData': 48}),
    'motorDevice_s': (60, {'count': 52}),
    'serialConfig_s': (112, {'portConfigs': 0}),
    'serialPortConfig_s': (12, {'functionMask': 0}),
    'modeActivationCondition_s': (6, {'modeId': 0, 'auxChannelIndex': 1, 'range': 2,
                                      'modeLogic': 4, 'linkedTo': 5}),
    'featureConfig_s': (4, {'enabledFeatures': 0}),
    'motorDevConfig_s': (28, {'motorPwmProtocol': 2, 'useUnsyncedPwm': 4}),
    'motorConfig_s': (40, {'dev': 0}),
    'gpsConfig_s': (77, {'provider': 0, 'autoConfig': 2, 'autoBaud': 3, 'gps_update_rate_hz': 6}),
    'sdcardConfig_s': (5, {'mode': 4}),
    'blackboxConfig_s': (8, {'device': 5}),
    'batteryConfig_s': (28, {'voltageMeterSource': 9, 'currentMeterSource': 10}),
    'pidConfig_s': (6, {'pid_process_denom': 0}),
    'controlRateConfig_s': (30, {'rates_type': 2, 'rcRates': 3, 'rcExpo': 6, 'rates': 9}),
}
SPI_ABI = {
    'busSegment_s': (20, {'u': 0, 'len': 8, 'negateCS': 12, 'callback': 16}),
    'extDevice_s': (152, {'bus': 0, 'busType_u': 4}),
    'busDevice_s': (40, {'busType_u': 4, 'curSegment': 32}),
}
MODERN_ABI = {
    'motorDevice_s': (12, {'count': 4}),
    'serialConfig_s': (88, {'portConfigs': 0}),
    'rxConfig_s': (52, {'serialrx_provider': 8}),
    'motorDevConfig_s': (28, {'motorProtocol': 2, 'useContinuousUpdate': 4}),
    'motorConfig_s': (38, {'dev': 0}),
    'controlRateConfig_s': (32, {'rates_type': 2, 'rcRates': 3, 'rcExpo': 6, 'rates': 9}),
}
SWEEP_ABIS = {
    '2026.6.2': {
        **MODERN_ABI,
        'gpsConfig_s': (78, {'provider': 0, 'autoConfig': 2, 'autoBaud': 3, 'gps_update_rate_hz': 6}),
        'batteryConfig_s': (14, {'voltageMeterSource': 3, 'currentMeterSource': 4}),
    },
    '2025.12.5': MODERN_ABI,
    '4.5.5': {},
    '4.4.3': {
        'busDevice_s': (44, {'busType_u': 4, 'curSegment': 32}),
        'motorDevConfig_s': (28, {'motorPwmProtocol': 2, 'useUnsyncedPwm': 4, 'ioTags': 8}),
        'motorConfig_s': (38, {'dev': 0}),
        'gpsConfig_s': (9, {'provider': 0, 'autoConfig': 2, 'autoBaud': 3}),
        'gyroDeviceConfig_s': (14, {'csnTag': 3, 'extiTag': 6, 'busType': 1, 'spiBus': 2,
                                   'alignment': 7, 'customAlignment': 8}),
        'barometerConfig_s': (8, {'baro_busType': 0, 'baro_i2c_device': 3, 'baro_hardware': 5}),
        'serialPinConfig_s': (36, {'ioTagTx': 0, 'ioTagRx': 12}),
        'i2cConfig_s': (6, {'ioTagScl': 0, 'ioTagSda': 1}),
        'spiPinConfig_s': (5, {'ioTagSck': 0, 'ioTagMiso': 1, 'ioTagMosi': 2}),
        'timerIOConfig_s': (3, {'ioTag': 0, 'index': 1, 'dmaopt': 2}),
        'controlRateConfig_s': (32, {'rates_type': 2, 'rcRates': 3, 'rcExpo': 6, 'rates': 9}),
    },
    '4.3.2': {
        'motorDevice_s': (52, {'count': 44}),
        'serialConfig_s': (100, {'portConfigs': 0}),
        'motorDevConfig_s': (26, {'motorPwmProtocol': 2, 'useUnsyncedPwm': 4}),
        'motorConfig_s': (36, {'dev': 0}),
        'gpsConfig_s': (9, {'provider': 0, 'autoConfig': 2, 'autoBaud': 3}),
        'blackboxConfig_s': (12, {'device': 1}),
        'batteryConfig_s': (26, {'voltageMeterSource': 9, 'currentMeterSource': 10}),
        'controlRateConfig_s': (38, {'rates_type': 2, 'rcRates': 3, 'rcExpo': 6, 'rates': 9}),
    },
    '4.2.11': {
        'motorDevice_s': (52, {'count': 44}),
        'serialConfig_s': (100, {'portConfigs': 0}),
        'motorDevConfig_s': (18, {'motorPwmProtocol': 2, 'useUnsyncedPwm': 4}),
        'motorConfig_s': (28, {'dev': 0}),
        'gpsConfig_s': (9, {'provider': 0, 'autoConfig': 2, 'autoBaud': 3}),
        'sdcardConfig_s': (6, {'mode': 5}),
        'blackboxConfig_s': (6, {'device': 2}),
        'batteryConfig_s': (26, {'voltageMeterSource': 9, 'currentMeterSource': 10}),
        'controlRateConfig_s': (34, {'rates_type': 2, 'rcRates': 3, 'rcExpo': 6, 'rates': 9}),
    },
}
# ELF hash -> (release, checksum of its absolute-address parameter writes).
SWEEP_IMAGES = {
    'd571ed03e1ae69ee2677d85722e85ad22cbb3d517f1b1bb2a8273ffcebbff590':
        ('2026.6.2', '90f65ada459dc4e04d60ee371a2b34e314e429adde376eeccc0bb7f2062edd86'),
    '57246a0e2cc2a746bf8fc49dc9339759ffb20e0cc3dbab905c35230490d9695c':
        ('2025.12.5', '7f956639b2d5628226459adbe4ef38bb515e0ee3209148ff46e8fc3859295749'),
    '46cb118a04a64d789a7fa21bbb46cd15869f0bb214cfc54acb6192cffbb44c09':
        ('4.5.5', '88c0d4d13abad5bc052c3351adff4089ab03315e89e69209ca9b40c11360bde6'),
    'd9763c6fc99cdabc710590fbc8240978aa35322c0b92c3fa2e153c6e450c1d00':
        ('4.4.3', '12802546397523748f99409eb63d32edd65955234cacd976e4d05e9bc8b1fe16'),
    '0a9e9b368a0ac625a6b48fd01721bdbcc6b3942a8da8202946b50913201de870':
        ('4.3.2', '2a09d1fc8b515d562c253004c69461fea0e9ed38254f77648930ac1a0a8e1056'),
    '0506924e3d51dd1d3ad3c4933319f82022b2239dbd23543ae904cf23f52b87e1':
        ('4.2.11', 'fe43333f857ddb6067bd9dae5524927d7e20ad2e0c4fc01c967e8b2782df0591'),
}


def validate(elf):
    image = SWEEP_IMAGES.get(elf.sha256)
    if image is not None:
        version, parameter_hash = image
        parameters = (Path(__file__).resolve().parents[2] / 'betaflight' /
                      'version_sweep' / version / 'parameters.txt')
        if hashlib.sha256(parameters.read_bytes()).hexdigest() != parameter_hash:
            raise ValueError('sweep parameter file changed; review and regenerate bindings')
        expected = {**SWEEP_ABI, **({} if version == '4.2.11' else SPI_ABI),
                    **SWEEP_ABIS[version]}
        validate_abi(elf.path, expected)
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
