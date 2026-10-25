"""PX4/NuttX adapter ABI shared by targets using the current C bridge."""
from execution_configuration.validation import validate_abi


# Offsets consumed by the NuttX UART and uORB adapters in ardupilot.c.
# A new board/version may reuse this adapter only while this ABI still matches.
ABI = {
    'file': (None, {'f_inode': 8}),
    'inode': (None, {'i_private': 20}),
    'up_dev_s': (None, {'dev': 0}),
    'uart_dev_s': (156, {'open_count': 0, 'isconsole': 4}),
    'orb_metadata': (None, {'o_name': 0}),
    'sensor_accel_s': (48, {'x': 20, 'y': 24, 'z': 28}),
    'sensor_gyro_s': (48, {'x': 20, 'y': 24, 'z': 28}),
    'sensor_mag_s': (40, {'x': 20, 'y': 24, 'z': 28}),
    'sensor_baro_s': (32, {'pressure': 20, 'temperature': 24}),
    'sensor_gps_s': (152, {'latitude_deg': 16, 'longitude_deg': 24,
        'altitude_msl_m': 32, 'altitude_ellipsoid_m': 40,
        'vel_m_s': 92, 'vel_n_m_s': 96, 'vel_e_m_s': 100, 'vel_d_m_s': 104,
        'cog_rad': 108, 'heading': 120}),
    'actuator_outputs_s': (80, {'noutputs': 8, 'output': 12}),
}


def validate(elf):
    validate_abi(elf.path, ABI)


def resolve_argument(callback, argument, elf):
    if callback == 'bridge_console_poll':
        if argument is None:
            raise ValueError('bridge_console_poll requires *console_device,receive_function,telemetry_device')
        symbols = argument.removeprefix('*').split(',')
        if len(symbols) != 3 or not all(symbols):
            raise ValueError('bridge_console_poll requires *console_device,receive_function,telemetry_device')
        # This callback redirects PC; the legacy plugin requires the leading *.
        return '*' + ','.join(hex(elf.resolve(symbol)) for symbol in symbols)
    return argument
