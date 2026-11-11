"""Exercise native sensor bus responses with mocked guest memory and plant data."""
import ctypes
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / 'virtuals/physics/flight_controllers/betaflight/hardware.c'


@pytest.fixture(scope='module')
def model(tmp_path_factory):
    compiler = shutil.which('cc')
    if not compiler:
        pytest.skip('C compiler unavailable')
    folder = tmp_path_factory.mktemp('bf-hardware-model')
    (folder / 'virtuals.h').write_text('''
#include <stddef.h>
#include <stdint.h>
#define ARM_V7M_R0 0
#define ARM_V7M_R1 1
#define ARM_V7M_R2 2
#define ARM_V7M_R3 3
#define ARM_V7M_LR 14
#define ARM_V7M_PC 15
uint32_t qemu_get_register(int);
void qemu_set_register(uint32_t, int);
int64_t qemu_plugin_get_virtual_timer(void);
int qemu_plugin_read_memory(uint64_t, void *, int);
int qemu_plugin_write_memory(uint64_t, uint8_t *, int);
void virtual_register(const char *, void (*)(unsigned int, void *));
''')
    harness = r'''
#include <stdint.h>
#include <string.h>
#include "phy.h"
static uint8_t memory[65536];
static uint32_t registers[16];
static imu_batch_t plant_imu;
static barometer_t plant_baro;
static gps_data_t plant_gps;
static int64_t tick;
uint32_t qemu_get_register(int r) { return registers[r]; }
void qemu_set_register(uint32_t value, int r) { registers[r] = value; }
int64_t qemu_plugin_get_virtual_timer(void) { return tick; }
int qemu_plugin_read_memory(uint64_t a, void *out, int n) {
    if (a + n > sizeof(memory)) return -1;
    memcpy(out, memory + a, n); return 0;
}
int qemu_plugin_write_memory(uint64_t a, uint8_t *in, int n) {
    if (a + n > sizeof(memory)) return -1;
    memcpy(memory + a, in, n); return 0;
}
void virtual_register(const char *name, void (*cb)(unsigned int, void *)) { (void)name; (void)cb; }
int phy_get_imu_batch(imu_batch_t *out) { *out = plant_imu; return 1; }
int phy_get_barometer_reading(barometer_t *out) { *out = plant_baro; return 1; }
int phy_get_navsat_reading(gps_data_t *out) { *out = plant_gps; return 1; }
'''
    harness += f'\n#include "{NATIVE}"\n'
    harness += r'''
void model_reset(void) {
    memset(memory, 0, sizeof(memory)); memset(registers, 0, sizeof(registers));
    memset(&plant_imu, 0, sizeof(plant_imu)); init_mpu();
    put32(0x100, 0x200); put32(0x204, 0x40013000);
    tick = 0;
}
void model_imu(float ax, float ay, float az, float gx, float gy, float gz) {
    plant_imu.imu[16] = (imu_t){ .accel_body = {ax,ay,az}, .gyro = {gx,gy,gz} };
}
void model_baro(double p, double t) { plant_baro = (barometer_t){p,t}; }
void model_spi(const uint8_t *tx, uint8_t *rx, int n, int split) {
    memcpy(memory + 0x400, tx, n); memset(memory + 0x300, 0, 60);
    put32(0x300, 0x400);
    if (split) {
        put32(0x308, 1);
        put32(0x314, 0x401); put32(0x318, 0x801); put32(0x31c, n-1); put8(0x320, 1);
    } else {
        put32(0x304, 0x800); put32(0x308, n); put8(0x30c, 1);
    }
    registers[0] = 0x100; registers[1] = 0x300; hw_spi_sequence(0, NULL);
    memcpy(rx, memory + 0x800, n);
}
int model_i2c(int address, int reg, int n, uint8_t *out) {
    put32(0x100, 0x200); put32(0x204, 0); put8(0x104, address);
    registers[0] = 0x100; registers[1] = reg; registers[2] = 0x800; registers[3] = n;
    hw_i2c_read(0, NULL); memcpy(out, memory + 0x800, n); return registers[0];
}
int model_gps(uint8_t *out, int64_t time) {
    tick = time; plant_gps = (gps_data_t){.lat=40.414929,.lon=-86.932387,.alt=164.1,.vel_n=2,.vel_e=-1,.vel_d=-0.5};
    return betaflight_hardware_gps_bytes(out, 512);
}
int model_legacy_i2c(int address, int reg, int n, uint8_t *out) {
    put8(0x104, 0); put8(0x105, address);
    registers[0]=0x100; registers[1]=reg; registers[2]=0x800; registers[3]=n;
    hw_i2c_legacy_read(0,NULL); memcpy(out,memory+0x800,n); return registers[0];
}
void model_legacy_spi(const uint8_t *tx, uint8_t *rx, int n) {
    put32(0x104,0x40013000); memcpy(memory+0x400,tx,n);
    registers[0]=0x100; registers[1]=0x400; registers[2]=0x800; registers[3]=n;
    hw_spi_legacy_transfer(0,NULL); memcpy(rx,memory+0x800,n);
}
int model_flash(uint32_t value) {
    registers[0]=0x900; registers[1]=value; hw_flash_word(0,NULL); return registers[0];
}
uint32_t model_word(uint32_t address) { return u32(address); }
void model_parameters(const char *path) { hw_parameters(0,(void *)path); }
'''
    source = folder / 'probe.c'
    source.write_text(harness)
    binary = folder / 'probe.so'
    subprocess.run([compiler, '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                    '-I', str(folder), '-I', str(ROOT / 'virtuals/physics'),
                    str(source), '-lm', '-o', str(binary)], check=True, capture_output=True)
    lib = ctypes.CDLL(str(binary))
    lib.model_imu.argtypes = [ctypes.c_float] * 6
    lib.model_baro.argtypes = [ctypes.c_double, ctypes.c_double]
    lib.model_gps.argtypes = [ctypes.c_void_p, ctypes.c_int64]
    lib.model_parameters.argtypes = [ctypes.c_char_p]
    lib.model_word.restype = ctypes.c_uint32
    return lib


@pytest.fixture
def sensor(model):
    model.model_reset()
    return model


def spi(sensor, tx, split=False):
    src = (ctypes.c_uint8 * len(tx))(*tx)
    out = (ctypes.c_uint8 * len(tx))()
    sensor.model_spi(src, out, len(tx), split)
    return bytes(out)


def test_mpu_identity_reset_and_split_chip_select(sensor):
    assert spi(sensor, [0xf5, 0])[1] == 0x68
    assert spi(sensor, [0x8c, 0], split=True)[1] == 0x58
    spi(sensor, [0x1b, 0x18], split=True)
    assert spi(sensor, [0x9b, 0])[1] == 0x18
    spi(sensor, [0x6b, 0x80])
    assert spi(sensor, [0x9b, 0])[1] == 0


def test_mpu_raw_axes_follow_range_and_inverse_board_alignment(sensor):
    # After the real driver's CW270 rotation, this produces body (1,-2,3) m/s^2.
    sensor.model_imu(1, 2, -3, 0.1, 0.2, -0.3)
    spi(sensor, [0x1b, 0x18])  # +/-2000 dps
    spi(sensor, [0x1c, 0x18])  # +/-16g
    sample = spi(sensor, [0xbb] + [0] * 14)[1:]
    ax, ay, az, temp, gx, gy, gz = struct.unpack('>7h', sample)
    assert (ax, ay, az) == tuple(round(v / 9.80665 * 2048) for v in (-2, -1, 3))
    assert (gx, gy, gz) == tuple(round(v * 180 / 3.141592653589793 * 16.384) for v in (-0.2, -0.1, 0.3))
    assert temp == round((25 - 36.53) * 340)
    spi(sensor, [0x1c, 0x10])
    assert struct.unpack('>h', spi(sensor, [0xbb, 0, 0])[1:])[0] == round(-2 / 9.80665 * 4096)


def read_i2c(sensor, reg, size, address=0x76):
    out = (ctypes.c_uint8 * size)()
    ok = sensor.model_i2c(address, reg, size, out)
    return ok, bytes(out)


@pytest.mark.parametrize('pressure,temperature', [(99546, 14.03), (80000, -10), (101325, 25), (110000, 40)])
def test_bmp_raw_samples_compensate_back_to_plant_values(sensor, pressure, temperature):
    sensor.model_baro(pressure, temperature)
    assert read_i2c(sensor, 0xd0, 1) == (1, b'\x58')
    ok, coefficients = read_i2c(sensor, 0x88, 24)
    assert ok
    t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9 = struct.unpack('<HhhHhhhhhhhh', coefficients)
    ok, raw = read_i2c(sensor, 0xf7, 6)
    assert ok
    adc_p = int.from_bytes(raw[:3], 'big') >> 4
    adc_t = int.from_bytes(raw[3:], 'big') >> 4
    fine = ((((adc_t >> 3) - (t1 << 1)) * t2) >> 11) + (((((adc_t >> 4) - t1) ** 2 >> 12) * t3) >> 14)
    compensated_t = ((fine * 5 + 128) >> 8) / 100
    # Integer Q24.8 algorithm used by the actual ARM driver.
    v1 = fine - 128000
    v2 = v1 * v1 * p6 + ((v1 * p5) << 17) + (p4 << 35)
    v1 = (((v1 * v1 * p3) >> 8) + ((v1 * p2) << 12))
    v1 = (((1 << 47) + v1) * p1) >> 33
    p = (((1048576 - adc_p) << 31) - v2) * 3125 // v1
    p = ((p + ((p9 * (p >> 13) ** 2) >> 25) + ((p8 * p) >> 19)) >> 8) + (p7 << 4)
    assert abs(p / 256 - pressure) < 0.3
    assert abs(compensated_t - temperature) <= 0.02


def test_i2c_nacks_absent_devices(sensor):
    assert read_i2c(sensor, 0xd0, 1, address=0x77)[0] == 0


def test_ubx_stream_has_valid_checksum_and_physical_navigation_units(sensor):
    out = (ctypes.c_uint8 * 512)()
    n = sensor.model_gps(out, 1000000000)
    wire = bytes(out[:n])
    assert len(wire) == 148
    pvt = wire[:100]
    assert pvt[:6] == b'\xb5\x62\x01\x07\x5c\x00'
    a = b = 0
    for value in pvt[2:-2]:
        a = (a + value) & 255
        b = (b + a) & 255
    assert pvt[-2:] == bytes((a, b))
    assert struct.unpack_from('<ii', pvt, 6 + 24) == (-869323870, 404149290)
    assert struct.unpack_from('<iii', pvt, 6 + 48) == (2000, -1000, -500)
    assert sensor.model_gps(out, 1050000000) == 0


def test_hardware_profile_validates_pinned_elf_and_preserves_driver_functions(tmp_path):
    profile = ROOT / 'virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr'
    cli = ROOT / 'virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py'
    subprocess.run([sys.executable, str(cli), '--profile', str(profile), '--output-dir', str(tmp_path)],
                   check=True, capture_output=True, text=True)
    rules = (tmp_path / 'virtuals.txt').read_text()
    assert 'bf_hw_spi_sequence *' in rules
    assert 'bf_hw_i2c_read *' in rules
    assert 'bf_trace hw-mpu-gyro-decode' in rules
    assert 'bf_trace hw-bmp-compensate' in rules
    assert 'bf_gyro_read' not in rules and 'bf_baro_calculate' not in rules and 'bf_gps_fill' not in rules
    assert (tmp_path / 'firmware.sha256').read_text().startswith('235afa1124b43784bebed4aa14afb883c7cb16e4fece853d771015888df5ecb7')
    modifiers = (profile / 'labeled_conf/modifiers.txt').read_text()
    assert 'accHasBeenCalibrated' not in modifiers
    assert 'compassInit' not in modifiers
    assert 'arming' not in modifiers.lower()


def test_hardware_profile_rejects_wrong_elf_without_overwriting_bindings(tmp_path):
    profile = ROOT / 'virtuals/physics/flight_controllers/courbet/betaflight/matekf405ctr'
    cli = ROOT / 'virtuals/physics/flight_controllers/courbet/prepare_execution_configuration.py'
    other_release = ROOT / 'virtuals/physics/flight_controllers/courbet/betaflight/version_sweep/4.5.5/bin/betaflight_STM32F405_MATEKF405CTR.elf'
    (tmp_path / 'virtuals.txt').write_text('retain existing rules\n')
    result = subprocess.run([sys.executable, str(cli), '--profile', str(profile), '--elf', str(other_release),
                             '--output-dir', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0 and 'SHA-256 mismatch' in result.stderr
    assert (tmp_path / 'virtuals.txt').read_text() == 'retain existing rules\n'


def test_default_f405_run_uses_physical_hardware_profile():
    hardware = tomllib.loads((ROOT / 'configs/betaflight_f405.toml').read_text())
    cpu = hardware['CPU']['cpu0'][0]
    assert cpu['binary'].endswith('betaflight_STM32F405_MATEKF405CTR.elf')
    assert cpu['existing_config_path'].endswith('betaflight/matekf405ctr/unlabeled_conf')
    assert hardware['Machine']['icount']['shift'] == 3
    assert all(p['enabled'] for p in hardware['Run']['processes'].values())


def test_legacy_spi_and_i2c_preserve_raw_sensor_registers(sensor):
    sensor.model_imu(0, 0, -9.80665, 0, 0, 0)
    tx=(ctypes.c_uint8*7)(0x80|0x3b,0,0,0,0,0,0)
    out=(ctypes.c_uint8*7)()
    sensor.model_legacy_spi(tx,out,7)
    assert struct.unpack('>hhh',bytes(out)[1:]) == (0,0,16384)
    identity=(ctypes.c_uint8*1)()
    assert sensor.model_legacy_i2c(0x76,0xd0,1,identity) == 1
    assert identity[0] == 0x58
    assert sensor.model_legacy_i2c(0x77,0xd0,1,identity) == 0


def test_spl_flash_word_uses_value_instead_of_dereferencing_it(sensor):
    assert sensor.model_flash(0x12345678) == 9
    assert sensor.model_word(0x900) == 0x12345678


def test_parameter_file_preserves_neighboring_fields(sensor,tmp_path):
    path=tmp_path/'parameters.txt'
    path.write_text('0x900/4/305419896\n0x901/1/255\n0x904/2/43981\n')
    sensor.model_parameters(str(path).encode())
    assert sensor.model_word(0x900) == 0x1234ff78
    assert sensor.model_word(0x904) == 0xabcd


def test_legacy_ubx_has_solution_position_and_velocity_frames(sensor,monkeypatch):
    monkeypatch.setenv('FASTDYN_BF_GPS_LEGACY','1')
    data=(ctypes.c_uint8*512)()
    length=sensor.model_gps(data,10_000_000_000)
    frames=[];offset=0
    while offset<length:
        size=int.from_bytes(bytes(data[offset+4:offset+6]),'little')
        frame=bytes(data[offset:offset+size+8]);a=b=0
        for value in frame[2:-2]:a=(a+value)&255;b=(b+a)&255
        assert frame[-2:]==bytes([a,b])
        frames.append((frame[3],frame[6:-2]));offset+=size+8
    assert [f[0] for f in frames] == [6,2,0x12]
    assert frames[0][1][10:12] == bytes([3,13])
    assert struct.unpack_from('<iii',frames[2][1],4) == (200,-100,-50)
