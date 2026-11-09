"""PX4/NuttX adapter ABIs supported by the Courbet C bridge."""
from __future__ import annotations

from execution_configuration.validation import validate_abi


# Offsets shared by the NuttX UART and uORB adapters in ardupilot.c.
# GPS is selected explicitly by the bridge_px4_uorb argument below because its
# representation changed between the supported PX4 release lines.
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
    'actuator_outputs_s': (80, {'noutputs': 8, 'output': 12}),
}

GPS_ABIS = {
    'gps_i32_v13': (112, {
        'lat': 20, 'lon': 24, 'alt': 28, 'alt_ellipsoid': 32,
        'vel_m_s': 68, 'vel_n_m_s': 72, 'vel_e_m_s': 76, 'vel_d_m_s': 80,
        'cog_rad': 84, 'heading': 92,
    }),
    'gps_i32_v14': (128, {
        'lat': 28, 'lon': 32, 'alt': 36, 'alt_ellipsoid': 40,
        'vel_m_s': 76, 'vel_n_m_s': 80, 'vel_e_m_s': 84, 'vel_d_m_s': 88,
        'cog_rad': 92, 'heading': 100,
    }),
    'gps_f64_v15': (144, {
        'latitude_deg': 16, 'longitude_deg': 24,
        'altitude_msl_m': 32, 'altitude_ellipsoid_m': 40,
        'vel_m_s': 92, 'vel_n_m_s': 96, 'vel_e_m_s': 100, 'vel_d_m_s': 104,
        'cog_rad': 108, 'heading': 116,
    }),
    'gps_f64_v17': (152, {
        'latitude_deg': 16, 'longitude_deg': 24,
        'altitude_msl_m': 32, 'altitude_ellipsoid_m': 40,
        'vel_m_s': 92, 'vel_n_m_s': 96, 'vel_e_m_s': 100, 'vel_d_m_s': 104,
        'cog_rad': 108, 'heading': 120,
    }),
}


def validate(elf):
    validate_abi(elf.path, ABI)


def resolve_argument(callback, argument, elf):
    if callback == 'bridge_px4_uorb':
        if argument not in GPS_ABIS:
            supported = ', '.join(sorted(GPS_ABIS))
            raise ValueError(f'bridge_px4_uorb requires a GPS ABI argument: {supported}')
        validate_abi(elf.path, {'sensor_gps_s': GPS_ABIS[argument]})
        return argument
    if callback == 'bridge_console_poll':
        if argument is None:
            raise ValueError(
                'bridge_console_poll requires *console_device,receive_function,telemetry_device')
        symbols = argument.removeprefix('*').split(',')
        if len(symbols) != 3 or not all(symbols):
            raise ValueError(
                'bridge_console_poll requires *console_device,receive_function,telemetry_device')
        # This callback redirects PC; the legacy plugin requires the leading *.
        return '*' + ','.join(hex(elf.resolve(symbol)) for symbol in symbols)
    return argument
